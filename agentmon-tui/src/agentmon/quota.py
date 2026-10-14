"""Subscription quota lookup shared by the dashboard and other features.

`QuotaService.report()` is the internal API for reading quota state: it
returns the cached snapshot when it is fresh enough and only calls the
provider usage endpoints when the cache has expired, so callers may invoke
it freely without spamming the APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
CODEX_ORIGINATOR = "codex_cli_rs"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTIGRAVITY_BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
ANTIGRAVITY_LOAD_URL = f"{ANTIGRAVITY_BASE_URL}:loadCodeAssist"
ANTIGRAVITY_USAGE_URL = f"{ANTIGRAVITY_BASE_URL}:retrieveUserQuotaSummary"
ANTIGRAVITY_CLI_VERSION = "1.1.10"
DEFAULT_TTL_SECONDS = 300.0
CLAUDE_SESSION_SECONDS = 5 * 3600.0
CLAUDE_WEEKLY_SECONDS = 7 * 86400.0

Fetcher = Callable[..., dict]


class QuotaError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuotaWindow:
    provider: str
    label: str
    used_percent: float
    resets_at: datetime | None = None
    window_seconds: float | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    def pace_percent(self, now: datetime) -> float | None:
        """Percentage of the window that has elapsed, or None if unknown.

        Providers report when a window resets but not when it started, so the
        start is derived by subtracting the window length from the reset time.
        Comparing this against `used_percent` says whether spending is ahead of
        or behind an even burn across the window.
        """
        if self.resets_at is None or not self.window_seconds:
            return None
        remaining = (self.resets_at - now).total_seconds()
        elapsed = self.window_seconds - remaining
        return max(0.0, min(100.0, elapsed / self.window_seconds * 100.0))


@dataclass(frozen=True)
class QuotaReport:
    fetched_at: datetime
    quotas: tuple[QuotaWindow, ...]
    errors: tuple[str, ...] = ()


def default_cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "agentmon" / "quota.json"


def _http_json(url: str, headers: dict[str, str], body: dict | None = None) -> dict:
    """GET `url`, or POST `body` to it as JSON when one is given."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise QuotaError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QuotaError(f"{url}: {exc}") from exc
    except ValueError as exc:
        raise QuotaError(f"{url}: invalid JSON response") from exc
    if not isinstance(payload, dict):
        raise QuotaError(f"{url}: unexpected response shape")
    return payload


def _window_span(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "window"
    if seconds >= 7 * 86400 - 3600:
        weeks = round(seconds / (7 * 86400))
        return "weekly" if weeks == 1 else f"{weeks}w"
    if seconds >= 86400:
        return f"{round(seconds / 86400)}d"
    return f"{round(seconds / 3600)}h"


def _window_seconds(value: object) -> float | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _epoch_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    # Go-written timestamps carry nanoseconds, which datetime cannot hold and
    # older Pythons refuse to parse at all.
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_codex_usage(payload: dict) -> list[QuotaWindow]:
    """Extract the weekly and (when applied) 5h windows from Codex usage."""
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise QuotaError("Codex usage response has no rate_limit data")
    quotas: list[QuotaWindow] = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        span = _window_span(window.get("limit_window_seconds"))
        quotas.append(
            QuotaWindow(
                provider="codex",
                label=f"Codex {span}",
                used_percent=float(window.get("used_percent") or 0.0),
                resets_at=_epoch_datetime(window.get("reset_at")),
                window_seconds=_window_seconds(window.get("limit_window_seconds")),
            )
        )
    return quotas


def _claude_limit_label(limit: dict) -> str:
    kind = limit.get("kind")
    if kind == "session":
        return "Claude 5h"
    if kind == "weekly_all":
        return "Claude weekly"
    if kind == "weekly_scoped":
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        return f"{name} weekly" if name else "Claude weekly (scoped)"
    return f"Claude {kind}" if isinstance(kind, str) else "Claude"


def _claude_limit_seconds(limit: dict) -> float | None:
    """Window length for a Claude limit, which the API states only by kind."""
    kind = limit.get("kind")
    if kind == "session":
        return CLAUDE_SESSION_SECONDS
    if kind in ("weekly_all", "weekly_scoped"):
        return CLAUDE_WEEKLY_SECONDS
    return None


def parse_claude_usage(payload: dict) -> list[QuotaWindow]:
    """Extract session/weekly windows from the Claude OAuth usage response."""
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        quotas = []
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            quotas.append(
                QuotaWindow(
                    provider="claude",
                    label=_claude_limit_label(limit),
                    used_percent=float(limit.get("percent") or 0.0),
                    resets_at=_iso_datetime(limit.get("resets_at")),
                    window_seconds=_claude_limit_seconds(limit),
                )
            )
        if quotas:
            return quotas

    quotas = []
    for key, label, seconds in (
        ("five_hour", "Claude 5h", CLAUDE_SESSION_SECONDS),
        ("seven_day", "Claude weekly", CLAUDE_WEEKLY_SECONDS),
    ):
        window = payload.get(key)
        if isinstance(window, dict):
            quotas.append(
                QuotaWindow(
                    provider="claude",
                    label=label,
                    used_percent=float(window.get("utilization") or 0.0),
                    resets_at=_iso_datetime(window.get("resets_at")),
                    window_seconds=seconds,
                )
            )
    if not quotas:
        raise QuotaError("Claude usage response has no limit data")
    return quotas


def _antigravity_window_seconds(window: object) -> float | None:
    """Length of an Antigravity bucket window, named rather than measured."""
    if window == "weekly":
        return CLAUDE_WEEKLY_SECONDS
    if isinstance(window, str) and window.endswith("h"):
        try:
            return float(window[:-1]) * 3600.0
        except ValueError:
            return None
    return None


def _is_gemini_group(group: dict) -> bool:
    """Whether a quota group covers the Gemini models `agy` runs by default.

    Antigravity also reports a group for the Claude and GPT models it can
    proxy, but those windows are spent by a different set of models; folding
    them in would let a spent third-party window pace a Gemini loop.
    """
    buckets = group.get("buckets")
    if isinstance(buckets, list) and any(
        isinstance(bucket, dict)
        and str(bucket.get("bucketId", "")).startswith("gemini")
        for bucket in buckets
    ):
        return True
    name = group.get("displayName")
    return isinstance(name, str) and "gemini" in name.lower()


def parse_antigravity_usage(payload: dict) -> list[QuotaWindow]:
    """Extract the Gemini 5h and weekly windows from an Antigravity summary.

    The API reports what is left rather than what is spent, and only names the
    window ("5h", "weekly") instead of dating its start.
    """
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise QuotaError("Antigravity usage response has no quota groups")
    quotas: list[QuotaWindow] = []
    for group in groups:
        if not isinstance(group, dict) or not _is_gemini_group(group):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            remaining = bucket.get("remainingFraction")
            if not isinstance(remaining, (int, float)):
                # The other arm of the oneof counts requests, which says
                # nothing about the share of the window that is left.
                continue
            span = _window_span(_antigravity_window_seconds(bucket.get("window")))
            quotas.append(
                QuotaWindow(
                    provider="antigravity",
                    label=f"Gemini {span}",
                    used_percent=max(0.0, 100.0 - float(remaining) * 100.0),
                    resets_at=_iso_datetime(bucket.get("resetTime")),
                    window_seconds=_antigravity_window_seconds(bucket.get("window")),
                )
            )
    if not quotas:
        raise QuotaError("Antigravity usage response has no Gemini quota data")
    return quotas


def _antigravity_user_agent(auth_method: str) -> str:
    """Identify as the Antigravity CLI, which the usage endpoint requires.

    Callers that do not name themselves this way get HTTP 403 no matter how
    good their OAuth token is, the same way the Codex endpoint insists on its
    originator header.
    """
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}.get(
        machine, machine
    )
    os_type = "darwin" if sys.platform == "darwin" else sys.platform
    return (
        f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} (aidev_client; "
        f"os_type={os_type}; arch={arch}; auth_method={auth_method})"
    )


class QuotaService:
    """Fetch and cache subscription quotas for Codex, Claude and Antigravity."""

    def __init__(
        self,
        *,
        codex_auth_path: Path | None = None,
        claude_credentials_path: Path | None = None,
        antigravity_token_path: Path | None = None,
        cache_path: Path | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        fetch: Fetcher = _http_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.codex_auth_path = codex_auth_path or Path.home() / ".codex" / "auth.json"
        self.claude_credentials_path = (
            claude_credentials_path or Path.home() / ".claude" / ".credentials.json"
        )
        self.antigravity_token_path = (
            antigravity_token_path
            or Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        )
        self.cache_path = cache_path or default_cache_path()
        self.ttl_seconds = ttl_seconds
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: dict[str | None, QuotaReport] = {}
        self._antigravity_project: str | None = None

    def report(self, *, service: str | None = None, force: bool = False) -> QuotaReport:
        """Read all quotas, or only one service, with independent cached results."""
        if service is not None and service not in ("codex", "claude", "antigravity"):
            raise ValueError(f"Unsupported quota service: {service}")
        with self._lock:
            if not force:
                cached = self._cached.get(service)
                if cached is None or self._expired(cached):
                    on_disk = self._load_disk_cache(service)
                    if on_disk is not None and (
                        cached is None or on_disk.fetched_at > cached.fetched_at
                    ):
                        cached = on_disk
                if cached is not None and not self._expired(cached):
                    self._cached[service] = cached
                    return cached
            fresh = self._fetch_report() if service is None else self._fetch_report(service)
            self._cached[service] = fresh
            self._store_disk_cache(fresh, service)
            return fresh

    def _expired(self, report: QuotaReport) -> bool:
        age = self._clock() - report.fetched_at.timestamp()
        return age < 0 or age >= self.ttl_seconds

    def _fetch_report(self, service: str | None = None) -> QuotaReport:
        quotas: list[QuotaWindow] = []
        errors: list[str] = []
        for name, fetch_provider in (
            ("codex", self._fetch_codex),
            ("claude", self._fetch_claude),
            ("antigravity", self._fetch_antigravity),
        ):
            if service is not None and name != service:
                continue
            try:
                quotas.extend(fetch_provider())
            except QuotaError as exc:
                errors.append(str(exc))
        return QuotaReport(
            fetched_at=datetime.fromtimestamp(self._clock(), tz=timezone.utc),
            quotas=tuple(quotas),
            errors=tuple(errors),
        )

    def _cache_file(self, service: str | None) -> Path:
        if service is None:
            return self.cache_path
        credential = {
            "codex": self.codex_auth_path,
            "claude": self.claude_credentials_path,
            "antigravity": self.antigravity_token_path,
        }[service]
        profile = hashlib.sha256(str(credential.resolve()).encode()).hexdigest()[:16]
        return self.cache_path.with_name(f"{self.cache_path.stem}-{service}-{profile}.json")

    def _fetch_codex(self) -> list[QuotaWindow]:
        try:
            auth = json.loads(self.codex_auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QuotaError(f"Codex auth unavailable: {exc}") from exc
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not access_token:
            raise QuotaError(
                f"Codex auth has no OAuth access token: {self.codex_auth_path}"
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "agentmon",
            # The usage endpoint rejects callers that do not declare the Codex
            # CLI originator with HTTP 403, regardless of the OAuth token.
            "originator": CODEX_ORIGINATOR,
        }
        account_id = tokens.get("account_id")
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return parse_codex_usage(self._fetch(CODEX_USAGE_URL, headers))

    def _fetch_claude(self) -> list[QuotaWindow]:
        try:
            credentials = json.loads(
                self.claude_credentials_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise QuotaError(f"Claude credentials unavailable: {exc}") from exc
        oauth = (
            credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
        )
        access_token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not access_token:
            raise QuotaError(
                f"Claude credentials have no OAuth access token: "
                f"{self.claude_credentials_path}"
            )
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at / 1000 <= self._clock():
            raise QuotaError(
                "Claude OAuth token is expired; run claude to refresh it"
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "agentmon",
        }
        return parse_claude_usage(self._fetch(CLAUDE_USAGE_URL, headers))

    def _fetch_antigravity(self) -> list[QuotaWindow]:
        try:
            credentials = json.loads(
                self.antigravity_token_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise QuotaError(f"Antigravity credentials unavailable: {exc}") from exc
        token = credentials.get("token") if isinstance(credentials, dict) else None
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not access_token:
            raise QuotaError(
                f"Antigravity credentials have no OAuth access token: "
                f"{self.antigravity_token_path}"
            )
        expiry = _iso_datetime(token.get("expiry"))
        if expiry is not None and expiry.timestamp() <= self._clock():
            # Only the Antigravity CLI refreshes this token, so agentmon can
            # do nothing but wait for the next `agy` run.
            raise QuotaError(
                "Antigravity OAuth token is expired; run agy to refresh it"
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": _antigravity_user_agent(
                str(credentials.get("auth_method") or "consumer")
            ),
        }
        project = self._antigravity_project or self._load_antigravity_project(headers)
        self._antigravity_project = project
        return parse_antigravity_usage(
            self._fetch(ANTIGRAVITY_USAGE_URL, headers, {"project": project})
        )

    def _load_antigravity_project(self, headers: dict[str, str]) -> str:
        """Look up the companion project the quota summary is keyed by.

        It is not stored anywhere on disk, so the only way to learn it is to
        ask the same endpoint the CLI asks when it starts up.
        """
        payload = self._fetch(
            ANTIGRAVITY_LOAD_URL, headers, {"metadata": {"ideType": "ANTIGRAVITY"}}
        )
        project = payload.get("cloudaicompanionProject")
        if not isinstance(project, str) or not project:
            raise QuotaError("Antigravity did not report a companion project")
        return project

    def _load_disk_cache(self, service: str | None = None) -> QuotaReport | None:
        try:
            raw = json.loads(self._cache_file(service).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return QuotaReport(
                fetched_at=datetime.fromtimestamp(
                    float(raw["fetched_at"]), tz=timezone.utc
                ),
                quotas=tuple(
                    QuotaWindow(
                        provider=str(quota["provider"]),
                        label=str(quota["label"]),
                        used_percent=float(quota["used_percent"]),
                        resets_at=_epoch_datetime(quota.get("resets_at")),
                        window_seconds=_window_seconds(quota.get("window_seconds")),
                    )
                    for quota in raw["quotas"]
                ),
                errors=tuple(str(error) for error in raw.get("errors", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _store_disk_cache(self, report: QuotaReport, service: str | None = None) -> None:
        payload = {
            "fetched_at": report.fetched_at.timestamp(),
            "quotas": [
                {
                    "provider": quota.provider,
                    "label": quota.label,
                    "used_percent": quota.used_percent,
                    "resets_at": (
                        quota.resets_at.timestamp() if quota.resets_at else None
                    ),
                    "window_seconds": quota.window_seconds,
                }
                for quota in report.quotas
            ],
            "errors": list(report.errors),
        }
        try:
            cache = self._cache_file(service)
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(cache)
        except OSError:
            # The cache is an optimization; quota reporting still works
            # without persisting it across restarts.
            pass

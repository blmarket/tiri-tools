from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentmon.quota import (
    ANTIGRAVITY_LOAD_URL,
    ANTIGRAVITY_USAGE_URL,
    CLAUDE_USAGE_URL,
    CODEX_USAGE_URL,
    QuotaError,
    QuotaReport,
    QuotaService,
    QuotaWindow,
    parse_antigravity_usage,
    parse_claude_usage,
    parse_codex_usage,
)


CODEX_PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 37,
            "limit_window_seconds": 604800,
            "reset_at": 1786240868,
        },
        "secondary_window": None,
    },
}

CLAUDE_PAYLOAD = {
    "five_hour": {"utilization": 41.0, "resets_at": "2026-08-02T05:00:00+00:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2026-08-07T12:00:00+00:00"},
    "limits": [
        {
            "kind": "session",
            "percent": 41,
            "resets_at": "2026-08-02T05:00:00+00:00",
        },
        {
            "kind": "weekly_all",
            "percent": 9,
            "resets_at": "2026-08-07T12:00:00+00:00",
        },
        {
            "kind": "weekly_scoped",
            "percent": 14,
            "resets_at": "2026-08-07T12:00:00+00:00",
            "scope": {"model": {"display_name": "Fable"}},
        },
    ],
}


ANTIGRAVITY_PAYLOAD = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "resetTime": "2026-08-15T07:57:05Z",
                    "remainingFraction": 0.8,
                },
                {
                    "bucketId": "gemini-5h",
                    "displayName": "Five Hour Limit Remaining",
                    "window": "5h",
                    "resetTime": "2026-08-09T11:11:53Z",
                    "remainingFraction": 1,
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {
                    "bucketId": "3p-weekly",
                    "window": "weekly",
                    "resetTime": "2026-08-16T06:39:05Z",
                    "remainingFraction": 0.1,
                }
            ],
        },
    ]
}


def test_parses_codex_weekly_window() -> None:
    quotas = parse_codex_usage(CODEX_PAYLOAD)
    assert [quota.label for quota in quotas] == ["Codex weekly"]
    assert quotas[0].used_percent == 37.0
    assert quotas[0].resets_at == datetime.fromtimestamp(
        1786240868, tz=timezone.utc
    )


def test_parses_codex_secondary_window_when_applied() -> None:
    payload = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 5,
                "limit_window_seconds": 604800,
            },
            "secondary_window": {
                "used_percent": 62,
                "limit_window_seconds": 18000,
            },
        }
    }
    quotas = parse_codex_usage(payload)
    assert [quota.label for quota in quotas] == ["Codex weekly", "Codex 5h"]
    assert quotas[1].used_percent == 62.0


def test_codex_payload_without_rate_limit_is_an_error() -> None:
    with pytest.raises(QuotaError):
        parse_codex_usage({"plan_type": "plus"})


def test_parses_claude_limits_including_fable() -> None:
    quotas = parse_claude_usage(CLAUDE_PAYLOAD)
    assert [quota.label for quota in quotas] == [
        "Claude 5h",
        "Claude weekly",
        "Fable weekly",
    ]
    assert [quota.used_percent for quota in quotas] == [41.0, 9.0, 14.0]
    assert quotas[0].resets_at == datetime(2026, 8, 2, 5, tzinfo=timezone.utc)


def test_parses_claude_legacy_fields_without_limits() -> None:
    payload = {key: CLAUDE_PAYLOAD[key] for key in ("five_hour", "seven_day")}
    quotas = parse_claude_usage(payload)
    assert [quota.label for quota in quotas] == ["Claude 5h", "Claude weekly"]


def test_parsed_windows_carry_their_length_for_pacing() -> None:
    codex = parse_codex_usage(CODEX_PAYLOAD)
    assert codex[0].window_seconds == 604800.0
    claude = parse_claude_usage(CLAUDE_PAYLOAD)
    assert [quota.window_seconds for quota in claude] == [18000.0, 604800.0, 604800.0]
    legacy = parse_claude_usage(
        {key: CLAUDE_PAYLOAD[key] for key in ("five_hour", "seven_day")}
    )
    assert [quota.window_seconds for quota in legacy] == [18000.0, 604800.0]


def test_unknown_claude_limit_kind_has_no_window_length() -> None:
    quotas = parse_claude_usage({"limits": [{"kind": "monthly", "percent": 3}]})
    assert quotas[0].window_seconds is None
    assert quotas[0].pace_percent(datetime.now(timezone.utc)) is None


def test_pace_percent_tracks_elapsed_share_of_the_window() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    quota = QuotaWindow(
        "claude",
        "Claude 5h",
        20.0,
        resets_at=now + timedelta(hours=1),
        window_seconds=5 * 3600.0,
    )
    assert quota.pace_percent(now) == pytest.approx(80.0)


def test_pace_percent_is_unavailable_without_a_reset_time() -> None:
    quota = QuotaWindow("codex", "Codex weekly", 20.0, window_seconds=604800.0)
    assert quota.pace_percent(datetime.now(timezone.utc)) is None


def test_pace_percent_clamps_outside_the_window() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    window = 5 * 3600.0
    stale = QuotaWindow("claude", "Claude 5h", 20.0, now - timedelta(hours=2), window)
    assert stale.pace_percent(now) == 100.0
    early = QuotaWindow("claude", "Claude 5h", 20.0, now + timedelta(hours=9), window)
    assert early.pace_percent(now) == 0.0


def test_claude_payload_without_data_is_an_error() -> None:
    with pytest.raises(QuotaError):
        parse_claude_usage({"limits": []})


def test_parses_antigravity_gemini_windows_as_usage() -> None:
    quotas = parse_antigravity_usage(ANTIGRAVITY_PAYLOAD)
    assert [quota.label for quota in quotas] == ["Gemini weekly", "Gemini 5h"]
    # The API reports what is left; the dialog and pacing both want what is spent.
    assert [quota.used_percent for quota in quotas] == pytest.approx([20.0, 0.0])
    assert [quota.window_seconds for quota in quotas] == [604800.0, 18000.0]
    assert quotas[1].resets_at == datetime(2026, 8, 9, 11, 11, 53, tzinfo=timezone.utc)
    assert {quota.provider for quota in quotas} == {"antigravity"}


def test_antigravity_third_party_group_does_not_pace_gemini_runs() -> None:
    """The Claude/GPT group is spent by other models, so it stays out."""
    quotas = parse_antigravity_usage(ANTIGRAVITY_PAYLOAD)
    assert all("Gemini" in quota.label for quota in quotas)


def test_antigravity_bucket_without_a_fraction_is_skipped() -> None:
    payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {"bucketId": "gemini-5h", "window": "5h", "remainingAmount": 3},
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.5,
                    },
                ],
            }
        ]
    }
    assert [quota.label for quota in parse_antigravity_usage(payload)] == [
        "Gemini weekly"
    ]


def test_antigravity_payload_without_gemini_data_is_an_error() -> None:
    with pytest.raises(QuotaError):
        parse_antigravity_usage({"groups": []})


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def credential_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    codex_auth = tmp_path / "codex-auth.json"
    codex_auth.write_text(
        json.dumps({"tokens": {"access_token": "codex-token", "account_id": "acct"}})
    )
    claude_credentials = tmp_path / "claude-credentials.json"
    claude_credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "claude-token"}})
    )
    antigravity_token = tmp_path / "antigravity-oauth-token"
    antigravity_token.write_text(
        json.dumps(
            {"token": {"access_token": "agy-token"}, "auth_method": "consumer"}
        )
    )
    return codex_auth, claude_credentials, antigravity_token


def make_service(
    tmp_path: Path,
    credential_paths: tuple[Path, Path, Path],
    clock: FakeClock,
    calls: list[str],
) -> QuotaService:
    def fetch(url: str, headers: dict[str, str], body: dict | None = None) -> dict:
        calls.append(url)
        if url == CODEX_USAGE_URL:
            assert headers["Authorization"] == "Bearer codex-token"
            assert headers["chatgpt-account-id"] == "acct"
            # Without the originator header the endpoint answers HTTP 403.
            assert headers["originator"] == "codex_cli_rs"
            return CODEX_PAYLOAD
        if url == CLAUDE_USAGE_URL:
            assert headers["Authorization"] == "Bearer claude-token"
            return CLAUDE_PAYLOAD
        if url == ANTIGRAVITY_LOAD_URL:
            assert body == {"metadata": {"ideType": "ANTIGRAVITY"}}
            return {"cloudaicompanionProject": "some-project"}
        if url == ANTIGRAVITY_USAGE_URL:
            assert headers["Authorization"] == "Bearer agy-token"
            # Like the Codex originator, this endpoint answers HTTP 403 unless
            # the caller names itself as the Antigravity CLI.
            assert headers["User-Agent"].startswith("antigravity/cli/")
            assert body == {"project": "some-project"}
            return ANTIGRAVITY_PAYLOAD
        raise AssertionError(f"unexpected URL: {url}")

    codex_auth, claude_credentials, antigravity_token = credential_paths
    return QuotaService(
        codex_auth_path=codex_auth,
        claude_credentials_path=claude_credentials,
        antigravity_token_path=antigravity_token,
        cache_path=tmp_path / "cache" / "quota.json",
        ttl_seconds=300,
        fetch=fetch,
        clock=clock,
    )


def test_report_fetches_every_provider(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    calls: list[str] = []
    service = make_service(tmp_path, credential_paths, FakeClock(1000.0), calls)
    report = service.report()
    assert [quota.label for quota in report.quotas] == [
        "Codex weekly",
        "Claude 5h",
        "Claude weekly",
        "Fable weekly",
        "Gemini weekly",
        "Gemini 5h",
    ]
    assert report.errors == ()
    assert calls == [
        CODEX_USAGE_URL,
        CLAUDE_USAGE_URL,
        ANTIGRAVITY_LOAD_URL,
        ANTIGRAVITY_USAGE_URL,
    ]


def test_report_looks_up_the_antigravity_project_once(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    """The companion project outlives a refresh, so only quota is refetched."""
    calls: list[str] = []
    service = make_service(tmp_path, credential_paths, FakeClock(1000.0), calls)
    service.report()
    service.report(force=True)
    assert calls.count(ANTIGRAVITY_LOAD_URL) == 1
    assert calls.count(ANTIGRAVITY_USAGE_URL) == 2


def test_report_serves_cached_data_within_ttl(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    calls: list[str] = []
    clock = FakeClock(1000.0)
    service = make_service(tmp_path, credential_paths, clock, calls)
    service.report()
    clock.now += 299
    service.report()
    assert calls == [
        CODEX_USAGE_URL,
        CLAUDE_USAGE_URL,
        ANTIGRAVITY_LOAD_URL,
        ANTIGRAVITY_USAGE_URL,
    ]


def test_report_refreshes_after_ttl(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    calls: list[str] = []
    clock = FakeClock(1000.0)
    service = make_service(tmp_path, credential_paths, clock, calls)
    service.report()
    clock.now += 301
    service.report()
    assert calls.count(CODEX_USAGE_URL) == 2


def test_report_force_bypasses_cache(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    calls: list[str] = []
    service = make_service(tmp_path, credential_paths, FakeClock(1000.0), calls)
    service.report()
    service.report(force=True)
    assert calls.count(CODEX_USAGE_URL) == 2


def test_report_reuses_disk_cache_across_instances(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    first_calls: list[str] = []
    service = make_service(tmp_path, credential_paths, FakeClock(1000.0), first_calls)
    service.report()

    second_calls: list[str] = []
    fresh = make_service(tmp_path, credential_paths, FakeClock(1100.0), second_calls)
    report = fresh.report()
    assert second_calls == []
    assert [quota.label for quota in report.quotas][0] == "Codex weekly"
    assert report.quotas[0].resets_at is not None
    assert report.quotas[0].window_seconds == 604800.0


def test_report_prefers_a_fresher_disk_cache_over_refetching(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    """A long-lived client must not refetch what a sibling already refreshed."""
    calls: list[str] = []
    clock = FakeClock(1000.0)
    service = make_service(tmp_path, credential_paths, clock, calls)
    service.report()

    # Another process refreshes the shared cache while this one waits out its TTL.
    other = make_service(tmp_path, credential_paths, FakeClock(1400.0), [])
    other.report(force=True)

    clock.now = 1500.0
    report = service.report()

    assert calls == [
        CODEX_USAGE_URL,
        CLAUDE_USAGE_URL,
        ANTIGRAVITY_LOAD_URL,
        ANTIGRAVITY_USAGE_URL,
    ]
    assert report.fetched_at.timestamp() == 1400.0


def test_report_refetches_when_the_disk_cache_is_stale_too(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    calls: list[str] = []
    clock = FakeClock(1000.0)
    service = make_service(tmp_path, credential_paths, clock, calls)
    service.report()

    clock.now = 2000.0
    service.report()

    assert calls.count(CODEX_USAGE_URL) == 2


def test_provider_failure_is_reported_not_raised(
    tmp_path: Path, credential_paths: tuple[Path, Path, Path]
) -> None:
    def fetch(url: str, headers: dict[str, str], body: dict | None = None) -> dict:
        if url == CODEX_USAGE_URL:
            raise QuotaError("codex is down")
        if url == ANTIGRAVITY_LOAD_URL:
            return {"cloudaicompanionProject": "some-project"}
        if url == ANTIGRAVITY_USAGE_URL:
            return ANTIGRAVITY_PAYLOAD
        return CLAUDE_PAYLOAD

    codex_auth, claude_credentials, antigravity_token = credential_paths
    service = QuotaService(
        codex_auth_path=codex_auth,
        claude_credentials_path=claude_credentials,
        antigravity_token_path=antigravity_token,
        cache_path=tmp_path / "quota.json",
        fetch=fetch,
        clock=FakeClock(1000.0),
    )
    report = service.report()
    assert report.errors == ("codex is down",)
    assert [quota.provider for quota in report.quotas] == ["claude"] * 3 + [
        "antigravity"
    ] * 2


def test_missing_credentials_become_errors(tmp_path: Path) -> None:
    service = QuotaService(
        codex_auth_path=tmp_path / "missing-codex.json",
        claude_credentials_path=tmp_path / "missing-claude.json",
        antigravity_token_path=tmp_path / "missing-agy-token",
        cache_path=tmp_path / "quota.json",
        fetch=lambda *args: pytest.fail("must not fetch without credentials"),
        clock=FakeClock(1000.0),
    )
    report = service.report()
    assert report.quotas == ()
    assert len(report.errors) == 3


def test_expired_claude_token_is_an_error(tmp_path: Path) -> None:
    claude_credentials = tmp_path / "claude.json"
    claude_credentials.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "token", "expiresAt": 900_000}}
        )
    )
    service = QuotaService(
        codex_auth_path=tmp_path / "missing-codex.json",
        claude_credentials_path=claude_credentials,
        antigravity_token_path=tmp_path / "missing-agy-token",
        cache_path=tmp_path / "quota.json",
        fetch=lambda *args: pytest.fail("must not fetch an expired token"),
        clock=FakeClock(1000.0),
    )
    report = service.report()
    assert any("expired" in error for error in report.errors)


def test_expired_antigravity_token_is_an_error(tmp_path: Path) -> None:
    """Only `agy` refreshes its token, so agentmon says so instead of retrying."""
    antigravity_token = tmp_path / "antigravity-oauth-token"
    antigravity_token.write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "agy-token",
                    "expiry": "1970-01-01T00:10:00Z",
                },
                "auth_method": "consumer",
            }
        )
    )
    service = QuotaService(
        codex_auth_path=tmp_path / "missing-codex.json",
        claude_credentials_path=tmp_path / "missing-claude.json",
        antigravity_token_path=antigravity_token,
        cache_path=tmp_path / "quota.json",
        fetch=lambda *args: pytest.fail("must not fetch an expired token"),
        clock=FakeClock(1000.0),
    )
    report = service.report()
    assert any("run agy" in error for error in report.errors)


def test_service_reports_fetch_and_expire_independently(tmp_path, credential_paths):
    calls = []
    clock = FakeClock(1000)
    service = make_service(tmp_path, credential_paths, clock, calls)
    report = service.report(service="codex")
    assert {q.provider for q in report.quotas} == {"codex"}
    assert report.errors == ()
    assert calls == [CODEX_USAGE_URL]
    clock.now += 200
    service.report(service="claude")
    clock.now += 101
    service.report(service="codex")
    service.report(service="claude")
    assert calls == [CODEX_USAGE_URL, CLAUDE_USAGE_URL, CODEX_USAGE_URL]
    service.report(service="claude", force=True)
    assert calls[-1] == CLAUDE_USAGE_URL


def test_service_disk_cache_is_shared_and_profile_scoped(tmp_path, credential_paths):
    first = make_service(tmp_path, credential_paths, FakeClock(1000), [])
    first.report(service="codex")
    calls = []
    second = make_service(tmp_path, credential_paths, FakeClock(1100), calls)
    assert second.report(service="codex").quotas
    assert calls == []
    alternate = tmp_path / "other-auth.json"
    alternate.write_text(credential_paths[0].read_text())
    third = make_service(tmp_path, (alternate, *credential_paths[1:]), FakeClock(1100), calls)
    third.report(service="codex")
    assert calls == [CODEX_USAGE_URL]


def test_unknown_quota_service_does_not_fetch(tmp_path, credential_paths):
    calls = []
    service = make_service(tmp_path, credential_paths, FakeClock(1000), calls)
    with pytest.raises(ValueError, match="Unsupported quota service"):
        service.report(service="unknown")
    assert calls == []

"""Fixtures for the webmon tests."""

from __future__ import annotations

import asyncio


def run_async(coro):
    """Run one coroutine to completion; the suite needs no asyncio plugin."""
    return asyncio.run(coro)

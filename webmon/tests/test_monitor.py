"""The monitor's history trimming."""

import asyncio
import json


from webmon.monitor import trim_history


async def until(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def test_history_is_trimmed_oldest_first_to_its_encoded_budget():
    screen = [b"screen"] * 3
    history = [b"\x1b[1m%04d" % index for index in range(100)]
    kept = trim_history(history + screen, rows=3, budget=200)
    assert kept[-3:] == screen
    assert kept[:-3] == history[len(history) - len(kept) + 3 :]
    assert 0 < len(kept) - 3 < 100
    assert sum(len(json.dumps(line.decode("latin-1"))) + 2 for line in kept[:-3]) <= 200
    assert trim_history(screen, rows=3, budget=0) == screen

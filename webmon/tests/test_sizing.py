import pytest

from webmon.sizing import dimensions


@pytest.mark.parametrize("value", [None, 0, -1, 1001, True, "40", 1.5])
def test_reject_invalid_dimensions(value):
    assert dimensions({"cols": 100, "rows": value}) is None
    assert dimensions({"cols": value, "rows": 40}) is None


import pytest

from webmon.formats import validate_template


@pytest.mark.parametrize("template", ["#(touch /tmp/no)", "#{E:@job}", "#{q;E:@job}", "#{E;q:@job}", "#{s|x|#{pane_id}|;E:@job}", "#{T:@job}", "bad\nline", "bad\0value", "é" * 2049])
def test_unsafe_or_oversized_templates_are_rejected(template):
    with pytest.raises(ValueError):
        validate_template(template)

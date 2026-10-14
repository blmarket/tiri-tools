"""The model: polls are authoritative, subscriptions patch what exists."""

from webmon.model import Model, rows_from_poll
from webmon.protocol import PANE_STATE_FIELDS, POLL_FIELDS


def poll_row(**overrides: str) -> str:
    values = {
        "session_id": "$0",
        "session_name": "work",
        "window_id": "@0",
        "window_index": "0",
        "window_name": "shell",
        "window_active": "1",
        "pane_index": "0",
        "pane_id": "%0",
        "pane_active": "1",
        "pane_width": "80",
        "pane_height": "24",
        "pane_current_command": "bash",
        "pane_current_path": "/home/x",
        "pane_title": "x",
        "pane_agent": "",
        "pane_agent_state": "",
        "pane_state_emoji": "💲",
        "pane_agent_model": "",
        "pane_agent_session_id": "",
        "alternate_on": "0",
    }
    values.update(overrides)
    return "\t".join(values[name] for name in POLL_FIELDS)


def state_row(**overrides: str) -> dict[str, str]:
    values = {
        "pane_id": "%0",
        "pane_active": "1",
        "pane_width": "80",
        "pane_height": "24",
        "pane_current_command": "bash",
        "pane_current_path": "/home/x",
        "pane_title": "x",
        "pane_agent": "",
        "pane_agent_state": "",
        "pane_state_emoji": "💲",
        "pane_agent_model": "",
        "pane_agent_session_id": "",
        "alternate_on": "0",
    }
    values.update(overrides)
    return {name: values[name] for name in PANE_STATE_FIELDS}


def test_a_first_poll_is_a_structure_change():
    model = Model()
    delta = model.apply_poll(rows_from_poll(poll_row()))
    assert delta.structure
    assert model.panes["%0"]["pane_width"] == 80
    assert model.panes["%0"]["pane_active"] is True


def test_an_unchanged_poll_produces_no_delta():
    model = Model()
    model.apply_poll(rows_from_poll(poll_row()))
    assert model.apply_poll(rows_from_poll(poll_row())).empty


def test_a_poll_reports_only_the_fields_that_moved():
    model = Model()
    model.apply_poll(rows_from_poll(poll_row()))
    delta = model.apply_poll(rows_from_poll(poll_row(pane_state_emoji="🔄")))
    assert delta.panes == {"%0": {"pane_state_emoji": "🔄"}}
    assert not delta.structure


def test_a_vanished_pane_is_removed_and_counts_as_structure():
    model = Model()
    model.apply_poll(rows_from_poll(poll_row() + "\n" + poll_row(pane_id="%1", pane_index="1")))
    delta = model.apply_poll(rows_from_poll(poll_row()))
    assert delta.removed == ["%1"]
    assert delta.structure
    assert "%1" not in model.panes


def test_a_subscription_row_patches_a_known_pane():
    model = Model()
    model.apply_poll(rows_from_poll(poll_row()))
    delta = model.apply_state_row(state_row(pane_state_emoji="🔄", pane_agent="claude"))
    assert delta.panes == {"%0": {"pane_state_emoji": "🔄", "pane_agent": "claude"}}
    assert model.panes["%0"]["session_name"] == "work"


def test_a_subscription_row_for_an_unknown_pane_asks_for_a_poll():
    model = Model()
    delta = model.apply_state_row(state_row(pane_id="%9"))
    assert delta.structure
    assert model.stale
    assert "%9" not in model.panes


def test_panes_group_into_sessions_and_windows_in_listing_order():
    model = Model()
    rows = "\n".join(
        [
            poll_row(),
            poll_row(pane_id="%1", pane_index="1"),
            poll_row(
                session_id="$1",
                session_name="other",
                window_id="@2",
                window_index="2",
                window_name="logs",
                pane_id="%2",
            ),
        ]
    )
    model.apply_poll(rows_from_poll(rows))
    sessions = model.sessions()
    assert [s["name"] for s in sessions] == ["work", "other"]
    assert sessions[0]["windows"][0]["panes"] == ["%0", "%1"]
    assert sessions[1]["windows"][0]["name"] == "logs"


def test_panes_are_addressed_by_session_id_not_name():
    model = Model()
    model.apply_poll(rows_from_poll(poll_row()))
    assert model.session_of("%0") == "$0"
    assert model.panes_in_session("$0") == ["%0"]
    assert model.panes_in_session("work") == []


def test_structure_changes_are_broadcast_as_a_whole_snapshot():
    model = Model()
    delta = model.apply_poll(rows_from_poll(poll_row()))
    assert model.delta_message(delta)["t"] == "model"
    delta = model.apply_poll(rows_from_poll(poll_row(pane_state_emoji="🔄")))
    assert model.delta_message(delta)["t"] == "panes"


def test_malformed_poll_lines_are_ignored():
    assert rows_from_poll("not\ta\trow\n" + poll_row()) == rows_from_poll(poll_row())

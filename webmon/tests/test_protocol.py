"""The control dialect: escaping, framing, and what each line means."""

from webmon import protocol


def test_unescape_matches_the_control_mode_fixtures():
    # The bytes tmux's own differential test asserts on the wire.
    assert protocol.unescape_output(rb"hello world\015\015\012") == b"hello world\r\r\n"
    assert protocol.unescape_output(rb"back\134slash\011end") == b"back\\slash\tend"
    assert protocol.unescape_output(rb"Z\033kTITLE\033\134VISIBLE") == (
        b"Z\x1bkTITLE\x1b\\VISIBLE"
    )


def test_del_is_not_escaped():
    assert protocol.unescape_output(b"end\x7fZ") == b"end\x7fZ"


def test_high_bytes_pass_through_unchanged():
    assert protocol.unescape_output(b"caf\xc3\xa9") == b"caf\xc3\xa9"


def test_a_backslash_tmux_could_not_have_written_is_preserved():
    assert protocol.unescape_output(rb"\9zz") == rb"\9zz"
    assert protocol.unescape_output(rb"tail\ ") == rb"tail\ "


def test_double_backslash_decodes_to_one():
    assert protocol.unescape_output(b"a\\\\b") == b"a\\b"


def test_parse_output_returns_raw_bytes():
    parsed = protocol.parse_output(rb"%output %3 a\015\012b")
    assert parsed is not None
    assert parsed.pane == "%3"
    assert parsed.data == b"a\r\nb"


def test_parse_output_accepts_an_empty_payload():
    parsed = protocol.parse_output(b"%output %3 ")
    assert parsed is not None and parsed.data == b""


def test_extended_output_carries_the_same_payload():
    # A client attached with pause-after is sent this spelling instead.
    parsed = protocol.parse_output(rb"%extended-output %7 0 : hi\015\012")
    assert parsed is not None
    assert parsed.pane == "%7"
    assert parsed.data == b"hi\r\n"


def test_utf8_split_across_two_output_lines_is_left_to_the_consumer():
    first = protocol.parse_output(b"%output %0 caf\xc3")
    second = protocol.parse_output(b"%output %0 \xa9!")
    assert first is not None and second is not None
    assert (first.data + second.data).decode("utf-8") == "café!"


def test_subscription_line_splits_into_target_and_value():
    event = protocol.parse_subscription(
        "%subscription-changed webmon $0 @1 3 %2 : %2\t1\t80"
    )
    assert event is not None
    assert (event.name, event.session, event.window, event.window_index, event.pane) == (
        "webmon",
        "$0",
        "@1",
        "3",
        "%2",
    )
    assert event.value == "%2\t1\t80"


def test_a_subscription_value_may_be_empty():
    event = protocol.parse_subscription("%subscription-changed webmon $0 @1 3 %2 : ")
    assert event is not None and event.value == ""


def test_begin_and_end_are_recognised_with_their_numbers():
    begin = protocol.parse_line("%begin 1788148238 286 0")
    end = protocol.parse_line("%end 1788148238 286 0")
    error = protocol.parse_line("%error 1788148238 286 1")
    assert isinstance(begin, protocol.Begin) and begin.number == "286"
    assert isinstance(end, protocol.End) and end.error is False
    assert isinstance(error, protocol.End) and error.error is True


def test_other_lines_are_notifications():
    event = protocol.parse_line("%window-add @4")
    assert isinstance(event, protocol.Notification)
    assert (event.name, event.arguments) == ("%window-add", "@4")


def test_the_notification_sets_cover_what_moves_structure():
    assert protocol.is_structure_notification("%window-close")
    assert protocol.is_structure_notification("%layout-change")
    assert not protocol.is_structure_notification("%output")
    assert protocol.is_session_set_notification("%sessions-changed")
    assert not protocol.is_session_set_notification("%window-add")


def test_subscription_command_targets_every_pane_of_the_session():
    command = protocol.subscription_command()
    assert command.startswith("refresh-client -B 'webmon:%*:")
    assert "#{pane_state_emoji}" in command
    assert "#{pane_agent_state}" in command


def test_state_rows_and_poll_rows_are_told_apart_by_width():
    state = "\t".join(["v"] * len(protocol.PANE_STATE_FIELDS))
    poll = "\t".join(["v"] * len(protocol.POLL_FIELDS))
    assert protocol.split_state_row(state) is not None
    assert protocol.split_state_row(poll) is not None
    assert protocol.split_state_row("too\tshort") is None

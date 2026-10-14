"""The hand-rolled RFC 6455 endpoint."""

import asyncio
import struct

import pytest
from conftest import run_async

from webmon import websocket as ws


def test_accept_key_matches_the_rfc_example():
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_needs_upgrade_connection_and_key():
    headers = {
        "upgrade": "websocket",
        "connection": "Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    response = ws.handshake_response(headers)
    assert response is not None
    assert b"101 Switching Protocols" in response
    assert b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in response


def test_a_browser_sending_keep_alive_and_upgrade_still_handshakes():
    # Firefox sends "keep-alive, Upgrade"; matching on the wrong token here
    # is the difference between a viewer and a 400.
    headers = {
        "upgrade": "websocket",
        "connection": "keep-alive, Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    assert ws.handshake_response(headers) is not None


def test_a_plain_request_is_not_a_handshake():
    assert ws.handshake_response({"connection": "keep-alive"}) is None
    assert ws.handshake_response({"upgrade": "websocket"}) is None


@pytest.mark.parametrize("size", [0, 5, 125, 126, 700, 70000])
def test_frames_round_trip_at_every_length_encoding(size: int):
    payload = b"x" * size

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(ws.client_mask_frame(ws.OP_BINARY, payload))
        reader.feed_eof()
        return await ws.read_frame(reader)

    frame = run_async(scenario())
    assert frame.opcode == ws.OP_BINARY
    assert frame.payload == payload


def test_server_frames_are_never_masked():
    encoded = ws.encode_frame(ws.OP_TEXT, b"hi")
    assert encoded[0] == 0x81
    assert encoded[1] == 2


def test_reading_past_the_end_of_the_stream_reports_a_close():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with pytest.raises(ws.WebSocketClosed):
            await ws.read_frame(reader)

    run_async(scenario())


def test_an_oversized_frame_is_refused_rather_than_buffered():
    async def scenario():
        reader = asyncio.StreamReader()
        header = bytes([0x82, 127]) + struct.pack("!Q", ws.MAX_MESSAGE_BYTES + 1)
        reader.feed_data(header)
        reader.feed_eof()
        with pytest.raises(ws.WebSocketError):
            await ws.read_frame(reader)

    run_async(scenario())


def test_unmask_is_its_own_inverse():
    key = b"\x01\x02\x03\x04"
    assert ws.unmask(ws.unmask(b"payload", key), key) == b"payload"

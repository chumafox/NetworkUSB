"""
Unit tests for networkusb.protocol.

Tests cover:
- build_frame / read_frame round-trip for all MsgType values
- Large DATA payload
- EOF mid-frame → IncompleteReadError
- Unknown msg_type → ValueError
- Payload exceeding MAX_PAYLOAD_SIZE → ValueError
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from networkusb.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD_SIZE,
    MsgType,
    build_frame,
    read_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_reader(data: bytes) -> asyncio.StreamReader:
    """Create an asyncio.StreamReader pre-loaded with *data* and EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# Round-trip tests for each MsgType
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_frame():
    frame = build_frame(MsgType.CONNECT, session_id=1)
    msg_type, sid, payload = await read_frame(make_reader(frame))
    assert msg_type == MsgType.CONNECT
    assert sid == 1
    assert payload == b""


@pytest.mark.asyncio
async def test_close_frame():
    frame = build_frame(MsgType.CLOSE, session_id=999)
    msg_type, sid, payload = await read_frame(make_reader(frame))
    assert msg_type == MsgType.CLOSE
    assert sid == 999
    assert payload == b""


@pytest.mark.asyncio
async def test_heartbeat_frame():
    frame = build_frame(MsgType.HEARTBEAT, session_id=0)
    msg_type, sid, payload = await read_frame(make_reader(frame))
    assert msg_type == MsgType.HEARTBEAT
    assert sid == 0
    assert payload == b""


@pytest.mark.asyncio
async def test_data_frame_small():
    body = b"hello from usbmuxd"
    frame = build_frame(MsgType.DATA, session_id=42, payload=body)
    msg_type, sid, payload = await read_frame(make_reader(frame))
    assert msg_type == MsgType.DATA
    assert sid == 42
    assert payload == body


@pytest.mark.asyncio
async def test_data_frame_binary():
    body = bytes(range(256)) * 64  # 16 KB of binary garbage
    frame = build_frame(MsgType.DATA, session_id=7, payload=body)
    msg_type, sid, payload = await read_frame(make_reader(frame))
    assert msg_type == MsgType.DATA
    assert sid == 7
    assert payload == body


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_session_id():
    """session_id is uint32 — test boundary value."""
    frame = build_frame(MsgType.CONNECT, session_id=0xFFFFFFFF)
    _, sid, _ = await read_frame(make_reader(frame))
    assert sid == 0xFFFFFFFF


@pytest.mark.asyncio
async def test_multiple_frames_sequential():
    """Multiple frames concatenated — each read_frame consumes exactly one."""
    f1 = build_frame(MsgType.CONNECT, 1)
    f2 = build_frame(MsgType.DATA, 1, b"data")
    f3 = build_frame(MsgType.CLOSE, 1)

    reader = asyncio.StreamReader()
    reader.feed_data(f1 + f2 + f3)
    reader.feed_eof()

    t1, s1, p1 = await read_frame(reader)
    t2, s2, p2 = await read_frame(reader)
    t3, s3, p3 = await read_frame(reader)

    assert (t1, s1, p1) == (MsgType.CONNECT, 1, b"")
    assert (t2, s2, p2) == (MsgType.DATA, 1, b"data")
    assert (t3, s3, p3) == (MsgType.CLOSE, 1, b"")


@pytest.mark.asyncio
async def test_eof_before_header():
    """Empty stream → IncompleteReadError."""
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(make_reader(b""))


@pytest.mark.asyncio
async def test_eof_mid_header():
    """Partial header → IncompleteReadError."""
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(make_reader(b"\x02\x00\x00"))


@pytest.mark.asyncio
async def test_eof_mid_payload():
    """Header valid but payload truncated → IncompleteReadError."""
    header = struct.pack(">BII", MsgType.DATA, 1, 100)
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(make_reader(header + b"\x00" * 50))  # only 50 of 100 bytes


@pytest.mark.asyncio
async def test_unknown_msg_type():
    """Unrecognised msg_type byte → ValueError."""
    bad = struct.pack(">BII", 0xFF, 1, 0)
    with pytest.raises(ValueError, match="Unknown msg_type"):
        await read_frame(make_reader(bad))


@pytest.mark.asyncio
async def test_payload_too_large():
    """payload_len exceeding MAX_PAYLOAD_SIZE → ValueError."""
    too_big = struct.pack(">BII", MsgType.DATA, 1, MAX_PAYLOAD_SIZE + 1)
    with pytest.raises(ValueError, match="Payload too large"):
        await read_frame(make_reader(too_big))


# ---------------------------------------------------------------------------
# Frame structure tests
# ---------------------------------------------------------------------------


def test_header_size_constant():
    frame = build_frame(MsgType.CONNECT, 0)
    assert len(frame) == HEADER_SIZE


def test_data_frame_size():
    body = b"x" * 1234
    frame = build_frame(MsgType.DATA, 0, body)
    assert len(frame) == HEADER_SIZE + len(body)


def test_frame_big_endian_encoding():
    """Verify the raw bytes match the expected big-endian layout."""
    frame = build_frame(MsgType.DATA, session_id=0x0000_0001, payload=b"AB")
    assert frame[0] == 0x02          # DATA
    assert frame[1:5] == b"\x00\x00\x00\x01"  # session_id = 1
    assert frame[5:9] == b"\x00\x00\x00\x02"  # payload_len = 2
    assert frame[9:] == b"AB"

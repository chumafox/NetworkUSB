"""
Binary multiplexing protocol for usbmuxd network tunnel.

Frame format (big-endian):
  [1 byte: msg_type] [4 bytes: session_id] [4 bytes: payload_len] [payload]

Total header size: 9 bytes.
"""

from __future__ import annotations

import asyncio
import struct
from enum import IntEnum

HEADER_SIZE = 9  # 1 (type) + 4 (session_id) + 4 (payload_len)
MAX_PAYLOAD_SIZE = 4 * 1024 * 1024  # 4 MB safety cap


class MsgType(IntEnum):
    """Protocol message types."""

    CONNECT = 0x01    # Bridge → Agent: open new usbmuxd session (payload_len = 0)
    DATA = 0x02       # Both directions: raw usbmuxd bytes
    CLOSE = 0x03      # Either direction: close session (payload_len = 0)
    HEARTBEAT = 0x04  # Bridge → Agent: keepalive; Agent replies with same (payload_len = 0)


async def read_frame(
    reader: asyncio.StreamReader,
) -> tuple[MsgType, int, bytes]:
    """
    Read exactly one frame from *reader*.

    Returns:
        (msg_type, session_id, payload)

    Raises:
        asyncio.IncompleteReadError: if connection is closed mid-frame.
        ValueError: if msg_type is unknown or payload exceeds MAX_PAYLOAD_SIZE.
    """
    header = await reader.readexactly(HEADER_SIZE)
    msg_type_byte, session_id, payload_len = struct.unpack(">BII", header)

    try:
        msg_type = MsgType(msg_type_byte)
    except ValueError:
        raise ValueError(f"Unknown msg_type: {msg_type_byte:#04x}")

    if payload_len > MAX_PAYLOAD_SIZE:
        raise ValueError(
            f"Payload too large: {payload_len} bytes (max {MAX_PAYLOAD_SIZE})"
        )

    payload = await reader.readexactly(payload_len) if payload_len else b""
    return msg_type, session_id, payload


def build_frame(
    msg_type: MsgType,
    session_id: int,
    payload: bytes = b"",
) -> bytes:
    """
    Encode a single protocol frame.

    Args:
        msg_type:   One of MsgType enum values.
        session_id: 32-bit unsigned session identifier.
        payload:    Raw bytes to send (empty for control frames).

    Returns:
        Bytes ready to be written to a transport.
    """
    return struct.pack(">BII", int(msg_type), session_id, len(payload)) + payload

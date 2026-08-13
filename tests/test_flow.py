"""Tests for SessionFlowControl and per-session byte-bounded queues."""

import pytest
import asyncio
from networkusb.flow import SessionFlowControl, MAX_CONCURRENT_SESSIONS


@pytest.mark.asyncio
async def test_flow_control_create_and_enqueue():
    flow = SessionFlowControl(max_sessions=5, max_queue_bytes=1024)
    q = flow.get_or_create_queue(1)
    assert flow.active_session_count == 1

    success = flow.enqueue(1, b"hello world")
    assert success is True

    payload = await flow.dequeue(1)
    assert payload == b"hello world"


@pytest.mark.asyncio
async def test_flow_control_max_sessions_limit():
    flow = SessionFlowControl(max_sessions=2, max_queue_bytes=1024)
    flow.get_or_create_queue(1)
    flow.get_or_create_queue(2)

    with pytest.raises(RuntimeError, match="Maximum concurrent sessions limit reached"):
        flow.get_or_create_queue(3)


@pytest.mark.asyncio
async def test_flow_control_byte_limit_overflow():
    flow = SessionFlowControl(max_sessions=5, max_queue_bytes=10)
    success1 = flow.enqueue(1, b"12345")
    assert success1 is True

    # Next enqueue exceeds 10 bytes limit
    success2 = flow.enqueue(1, b"123456")
    assert success2 is False

    flow.remove_session(1)
    assert flow.active_session_count == 0

"""
Per-session flow control and byte-bounded queue management.

Prevents Head-of-Line (HOL) blocking across parallel usbmuxd sessions
over WAN/Tailscale connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Final

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SESSIONS: Final[int] = 32
MAX_QUEUE_BYTES_PER_SESSION: Final[int] = 5 * 1024 * 1024  # 5 MB


class SessionFlowControl:
    """
    Manages per-session byte-bounded buffers for usbmuxd frame dispatching.
    """

    def __init__(
        self,
        max_sessions: int = MAX_CONCURRENT_SESSIONS,
        max_queue_bytes: int = MAX_QUEUE_BYTES_PER_SESSION,
    ) -> None:
        self.max_sessions = max_sessions
        self.max_queue_bytes = max_queue_bytes
        self._queues: dict[int, asyncio.Queue[bytes]] = {}
        self._queue_bytes: dict[int, int] = defaultdict(int)

    @property
    def active_session_count(self) -> int:
        return len(self._queues)

    def get_or_create_queue(self, session_id: int) -> asyncio.Queue[bytes]:
        """
        Return existing queue for session_id or create a new bounded queue.
        Raises RuntimeError if max_sessions limit is reached.
        """
        if session_id in self._queues:
            return self._queues[session_id]

        if len(self._queues) >= self.max_sessions:
            raise RuntimeError(
                f"Maximum concurrent sessions limit reached ({self.max_sessions})"
            )

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._queues[session_id] = q
        self._queue_bytes[session_id] = 0
        logger.debug("Created flow control queue for session_id=%d", session_id)
        return q

    def enqueue(self, session_id: int, payload: bytes) -> bool:
        """
        Enqueue payload for session_id if queue byte limit is respected.
        Return True if enqueued, False if dropped due to buffer overflow.
        """
        q = self.get_or_create_queue(session_id)
        current_bytes = self._queue_bytes[session_id]
        if current_bytes + len(payload) > self.max_queue_bytes:
            logger.warning(
                "Session %d queue overflow (%d > %d bytes). Dropping payload.",
                session_id,
                current_bytes + len(payload),
                self.max_queue_bytes,
            )
            return False

        try:
            q.put_nowait(payload)
            self._queue_bytes[session_id] += len(payload)
            return True
        except asyncio.QueueFull:
            logger.warning("Session %d queue full. Dropping payload.", session_id)
            return False

    async def dequeue(self, session_id: int) -> bytes:
        """
        Dequeue next payload for session_id.
        """
        if session_id not in self._queues:
            raise KeyError(f"Session {session_id} not registered")

        q = self._queues[session_id]
        payload = await q.get()
        self._queue_bytes[session_id] = max(0, self._queue_bytes[session_id] - len(payload))
        return payload

    def remove_session(self, session_id: int) -> None:
        """
        Remove queue and release memory for session_id.
        """
        if session_id in self._queues:
            del self._queues[session_id]
            self._queue_bytes.pop(session_id, None)
            logger.debug("Removed flow control queue for session_id=%d", session_id)

    def clear(self) -> None:
        """
        Clear all session queues.
        """
        self._queues.clear()
        self._queue_bytes.clear()

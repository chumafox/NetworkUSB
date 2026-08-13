"""
AgentServer — accepts TLS connections from bridge, multiplexes them
to the local usbmuxd UNIX socket.

Session lifecycle:
  1. Bridge sends AUTH <token>\\n
  2. Agent replies OK\\n (or FAIL\\n and closes)
  3. Bridge sends CONNECT frames to open usbmuxd sessions
  4. Both sides exchange DATA frames
  5. Either side sends CLOSE to tear down a session
  6. Bridge sends HEARTBEAT every 30 s; Agent echoes it back
  7. If 3 heartbeats are missed (90 s total), Agent disconnects bridge
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
import time
from dataclasses import dataclass, field

from networkusb.protocol import MsgType, build_frame, read_frame
from networkusb.utils import apply_tcp_keepalive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
AUTH_TIMEOUT = 10.0          # seconds to wait for AUTH line after connection
CHUNK_SIZE = 65_536          # bytes per read from usbmuxd
HEARTBEAT_INTERVAL = 30.0   # seconds between expected heartbeats from bridge
HEARTBEAT_MAX_MISSED = 3    # disconnect after this many missed intervals


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class UsbmuxdSession:
    """Represents one open connection to the local usbmuxd daemon."""

    session_id: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    task: asyncio.Task | None = field(default=None, repr=False)

    async def close(self) -> None:
        """Cancel the relay task and close the usbmuxd connection."""
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class AgentServer:
    """
    TCP+TLS server that bridges incoming connections to local usbmuxd.

    One AgentServer handles exactly one bridge connection at a time.
    If a second bridge connects while one is active, it is authenticated
    first and then both coexist independently with their own session maps.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        usbmuxd_path: str,
        ssl_context: ssl.SSLContext,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.usbmuxd_path = usbmuxd_path
        self.ssl_context = ssl_context
        self._server: asyncio.Server | None = None
        # Actual bound port (set in start()); read after stop() is NOT reliable
        # because the server's sockets are closed. Tests/CLI use this instead of
        # poking _server.sockets post-shutdown.
        self.bound_port: int | None = None
        # Active bridge connection handlers + writers. Tracked so a graceful
        # shutdown can force-close attached bridges instead of hanging in
        # server.wait_closed() (see note in start()).
        self._bridge_tasks: set[asyncio.Task] = set()
        self._bridge_writers: set[asyncio.StreamWriter] = set()
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the TCP+TLS listener and serve until stop() or cancellation.

        IMPORTANT: we do NOT drive shutdown through ``server.serve_forever()``
        cancellation. In CPython 3.14, ``serve_forever()`` catches
        ``CancelledError`` and internally awaits ``wait_closed()``, which blocks
        forever while any ``_handle_bridge`` coroutine is live — and that
        handler's own cancellation is triggered from *our* ``finally``, which
        never runs because ``serve_forever()`` never re-raises. The result is a
        deadlock (only a *second* cancel() from e.g. ``wait_for`` breaks it).

        Instead we wait on an explicit ``asyncio.Event`` and do the cleanup
        ourselves with timeouts, so both ``agent.stop()`` and ``task.cancel()``
        are safe.
        """
        self._server = await asyncio.start_server(
            self._handle_bridge,
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
        )
        self.bound_port = self._server.sockets[0].getsockname()[1]

        # Apply TCP keepalive to all listening sockets
        for sock in self._server.sockets:
            try:
                apply_tcp_keepalive(sock)
            except OSError:
                pass  # Some platforms may not support all options

        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info("Agent server listening on %s", addrs)

        self._stop_event.clear()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        """Request a graceful shutdown (safe to call from anywhere)."""
        self._stop_event.set()
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Force-close bridges, then close the listener — with timeouts."""
        # 1. Close transport writers so TLS close_notify handshakes can't block.
        for writer in list(self._bridge_writers):
            writer.close()
        # 2. Cancel + await the bridge handler tasks.
        for task in list(self._bridge_tasks):
            task.cancel()
        if self._bridge_tasks:
            await asyncio.gather(*self._bridge_tasks, return_exceptions=True)
            self._bridge_tasks.clear()
        # 3. Close the listener. wait_closed() still waits for pending TLS
        #    handshakes that never became handler tasks, so bound it.
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except Exception:
                logger.warning("Server wait_closed timed out during cleanup")
            self._server = None

    # ------------------------------------------------------------------
    # Bridge connection handler
    # ------------------------------------------------------------------

    async def _handle_bridge(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Top-level coroutine for each incoming bridge connection.

        Runs the AUTH handshake, then dispatches frames until the bridge
        disconnects or a heartbeat timeout occurs.
        """
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.info("Incoming bridge connection from %s", peer)

        # Track this handler + writer so a graceful shutdown can force-close it.
        current = asyncio.current_task()
        if current:
            self._bridge_tasks.add(current)
        self._bridge_writers.add(writer)
        try:
            await self._handle_bridge_inner(reader, writer, peer)
        except asyncio.CancelledError:
            raise
        finally:
            if current:
                self._bridge_tasks.discard(current)
            self._bridge_writers.discard(writer)
            writer.close()
            # Bound wait_closed: a broken/half-open peer must not block teardown.
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def _handle_bridge_inner(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer,
    ) -> None:
        """Authenticate + dispatch frames for one bridge connection."""

        # ---- AUTH handshake ----
        try:
            async with asyncio.timeout(AUTH_TIMEOUT):
                auth_line = await reader.readline()
        except TimeoutError:
            logger.warning("Auth timeout from %s — closing", peer)
            writer.close()
            return

        auth_str = auth_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not auth_str.startswith("AUTH ") or not secrets.compare_digest(auth_str[5:], self.token):
            logger.warning("Auth failed from %s", peer)
            try:
                writer.write(b"FAIL\n")
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return

        try:
            writer.write(b"OK\n")
            await writer.drain()
        except Exception as e:
            logger.error("Failed to send OK to %s: %s", peer, e)
            writer.close()
            return

        logger.info("Bridge %s authenticated successfully", peer)

        # ---- Per-connection state ----
        sessions: dict[int, UsbmuxdSession] = {}
        write_lock = asyncio.Lock()
        last_heartbeat_ts = time.monotonic()

        async def send_frame(
            msg_type: MsgType, session_id: int, payload: bytes = b""
        ) -> None:
            """Send a single frame to bridge (serialised via write_lock)."""
            frame = build_frame(msg_type, session_id, payload)
            async with write_lock:
                writer.write(frame)
            await writer.drain()

        async def close_session(session_id: int) -> None:
            session: UsbmuxdSession | None = sessions.pop(session_id, None)
            if session:
                await session.close()
                logger.debug("Closed usbmuxd session %d", session_id)

        async def relay_usbmuxd_to_bridge(session: UsbmuxdSession) -> None:
            """
            Background task: read bytes from usbmuxd and forward as DATA frames.
            Sends CLOSE when usbmuxd closes the connection.
            """
            try:
                while True:
                    data = await session.reader.read(CHUNK_SIZE)
                    if not data:
                        logger.debug(
                            "Session %d: usbmuxd closed connection", session.session_id
                        )
                        break
                    await send_frame(MsgType.DATA, session.session_id, data)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(
                    "Session %d relay error: %s", session.session_id, exc
                )
            finally:
                # Notify bridge that this session ended
                try:
                    await send_frame(MsgType.CLOSE, session.session_id)
                except Exception:
                    pass
                sessions.pop(session.session_id, None)
                try:
                    session.writer.close()
                except Exception:
                    pass

        async def heartbeat_watchdog() -> None:
            """Disconnect bridge if heartbeats stop arriving."""
            timeout = HEARTBEAT_INTERVAL * HEARTBEAT_MAX_MISSED
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                elapsed = time.monotonic() - last_heartbeat_ts
                if elapsed > timeout:
                    logger.warning(
                        "Bridge %s: heartbeat timeout (%.0f s) — disconnecting",
                        peer,
                        elapsed,
                    )
                    writer.close()
                    return

        watchdog_task = asyncio.create_task(heartbeat_watchdog())

        # ---- Frame dispatch loop ----
        try:
            while True:
                msg_type, session_id, payload = await read_frame(reader)

                if msg_type == MsgType.CONNECT:
                    logger.debug("CONNECT session %d from %s", session_id, peer)
                    try:
                        ux_reader, ux_writer = await asyncio.open_unix_connection(
                            self.usbmuxd_path
                        )
                    except OSError as exc:
                        logger.error(
                            "Cannot connect to usbmuxd at %s: %s",
                            self.usbmuxd_path,
                            exc,
                        )
                        await send_frame(MsgType.CLOSE, session_id)
                        continue

                    session = UsbmuxdSession(session_id, ux_reader, ux_writer)
                    sessions[session_id] = session
                    session.task = asyncio.create_task(
                        relay_usbmuxd_to_bridge(session),
                        name=f"relay-{session_id}",
                    )

                elif msg_type == MsgType.DATA:
                    sess = sessions.get(session_id)
                    if sess is None:
                        logger.debug(
                            "DATA for unknown session %d — sending CLOSE", session_id
                        )
                        await send_frame(MsgType.CLOSE, session_id)
                        continue
                    try:
                        sess.writer.write(payload)
                        await sess.writer.drain()
                    except Exception as exc:
                        logger.debug(
                            "Session %d write to usbmuxd failed: %s", session_id, exc
                        )
                        await close_session(session_id)
                        try:
                            await send_frame(MsgType.CLOSE, session_id)
                        except Exception:
                            pass

                elif msg_type == MsgType.CLOSE:
                    logger.debug("CLOSE session %d from %s", session_id, peer)
                    await close_session(session_id)

                elif msg_type == MsgType.HEARTBEAT:
                    last_heartbeat_ts = time.monotonic()
                    logger.debug("HEARTBEAT from %s", peer)
                    await send_frame(MsgType.HEARTBEAT, 0)

        except asyncio.IncompleteReadError:
            logger.info("Bridge %s disconnected (EOF)", peer)
        except ValueError as exc:
            logger.error("Protocol error from %s: %s", peer, exc)
        except Exception as exc:
            logger.error("Unexpected error from bridge %s: %s", peer, exc, exc_info=True)
        finally:
            watchdog_task.cancel()
            # Close all open usbmuxd sessions
            n = len(sessions)
            for sid in list(sessions.keys()):
                await close_session(sid)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(
                "Bridge %s cleaned up; closed %d usbmuxd session(s)", peer, n
            )

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

    async def start(self) -> None:
        """Start the TCP+TLS listener and serve indefinitely."""
        self._server = await asyncio.start_server(
            self._handle_bridge,
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
        )

        # Apply TCP keepalive to all listening sockets
        for sock in self._server.sockets:
            try:
                apply_tcp_keepalive(sock)
            except OSError:
                pass  # Some platforms may not support all options

        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info("Agent server listening on %s", addrs)

        async with self._server:
            await self._server.serve_forever()

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

        # ---- AUTH handshake ----
        try:
            async with asyncio.timeout(AUTH_TIMEOUT):
                auth_line = await reader.readline()
        except TimeoutError:
            logger.warning("Auth timeout from %s — closing", peer)
            writer.close()
            return

        auth_str = auth_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not auth_str.startswith("AUTH ") or auth_str[5:] != self.token:
            logger.warning("Auth failed from %s (got: %r)", peer, auth_str[:20])
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
            session = sessions.pop(session_id, None)
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
                    session = sessions.get(session_id)
                    if session is None:
                        logger.debug(
                            "DATA for unknown session %d — sending CLOSE", session_id
                        )
                        await send_frame(MsgType.CLOSE, session_id)
                        continue
                    try:
                        session.writer.write(payload)
                        await session.writer.drain()
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

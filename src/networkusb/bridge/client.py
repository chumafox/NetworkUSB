"""
BridgeClient — connects to the remote usbmuxd-agent over TCP+TLS,
exposes a local UNIX socket for libimobiledevice / pymobiledevice3.

Connection lifecycle:
  1. Open TCP+TLS connection to agent
  2. Verify certificate fingerprint (pin on first connect)
  3. Send AUTH <token>\\n, wait for OK\\n
  4. Create local UNIX socket at socket_path (chmod 0o777)
  5. Accept local clients; for each: send CONNECT → relay bytes → send CLOSE
  6. Send HEARTBEAT every 30 s
  7. On any disconnect: clean up all local clients + unix socket, reconnect
     with exponential backoff (1 s → 2 → 4 → … → 30 s max)

Session IDs are monotonically increasing uint32 starting at 1.
They are NOT reset between reconnect attempts.

Backpressure is handled by asyncio's transport-level flow control
(writer.write() + await writer.drain()) — no frame-counting semaphore.
A byte tunnel's traffic is not symmetric, so counting in-flight DATA
frames against reverse DATA would stall one-way transfers (AUDIT F-01).
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from pathlib import Path

from networkusb.protocol import MsgType, SessionIdAllocator, build_frame, read_frame
from networkusb.tls import (
    fingerprint_from_der,
    load_known_fingerprint,
    save_known_fingerprint,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL = 30.0   # seconds
CHUNK_SIZE = 65_536          # bytes per local-socket read
AUTH_TIMEOUT = 10.0          # seconds


class BridgeClient:
    """
    Manages the persistent connection to agent and local UNIX socket server.

    Designed to be run via ``asyncio.run(client.run())``.
    """

    def __init__(
        self,
        agent_host: str,
        agent_port: int,
        token: str,
        socket_path: str,
        ssl_context: ssl.SSLContext,
        expected_fingerprint: str | None = None,
        socket_mode: int = 0o700,
    ) -> None:
        self.agent_host = agent_host
        self.agent_port = agent_port
        self.token = token
        self.socket_path = socket_path
        self.ssl_context = ssl_context
        self.expected_fingerprint = expected_fingerprint
        self.socket_mode = socket_mode

        # Session ID allocator with uint32 wrap safety
        self._session_allocator = SessionIdAllocator(1)

        # Active local client writers keyed by session_id
        self._local_clients: dict[int, asyncio.StreamWriter] = {}

        # Agent TCP writer (None when disconnected)
        self._agent_writer: asyncio.StreamWriter | None = None

        # Serialise writes to agent
        self._write_lock = asyncio.Lock()

        # Local UNIX server handle
        self._unix_server: asyncio.Server | None = None

        self._running = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main reconnect loop. Runs until :py:meth:`stop` is called or the
        task is cancelled.
        """
        backoff = self._backoff_generator()
        while self._running:
            start_time = time.monotonic()
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as exc:
                logger.error("Agent connection error: %s", exc)

            if not self._running:
                break

            # If connection was sustained for > 5s, reset backoff delay
            if time.monotonic() - start_time > 5.0:
                backoff = self._backoff_generator()

            delay = next(backoff)
            logger.info("Reconnecting to agent in %.0f s...", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        await self._teardown()
        logger.info("Bridge stopped")

    def stop(self) -> None:
        """Signal the run loop to stop after current connection is closed."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal: connect + serve
    # ------------------------------------------------------------------

    async def _connect_and_serve(self) -> None:
        """
        Establish one full connection to the agent: auth, unix socket, relay.

        Raises on any failure so the caller can handle reconnect logic.
        """
        logger.info(
            "Connecting to agent at %s:%d", self.agent_host, self.agent_port
        )

        reader, writer = await asyncio.open_connection(
            self.agent_host,
            self.agent_port,
            ssl=self.ssl_context,
        )
        logger.debug("TCP+TLS handshake complete")

        # ---- Certificate pinning & Fingerprint Verification (BEFORE AUTH) ----
        ssl_obj = writer.get_extra_info("ssl_object")
        if ssl_obj:
            await self._verify_fingerprint(ssl_obj)

        # ---- AUTH ----
        writer.write(f"AUTH {self.token}\n".encode())
        await writer.drain()

        try:
            async with asyncio.timeout(AUTH_TIMEOUT):
                resp_line = await reader.readline()
        except TimeoutError:
            writer.close()
            raise ConnectionError("Auth response timeout from agent")

        resp = resp_line.decode("utf-8", errors="replace").strip()
        if resp != "OK":
            writer.close()
            raise ConnectionError(f"Agent rejected auth: {resp!r}")

        logger.info("Authenticated to agent %s:%d", self.agent_host, self.agent_port)
        self._agent_writer = writer

        # ---- Create local UNIX socket ----
        await self._start_unix_server()

        # ---- Start heartbeat sender ----
        hb_task = asyncio.create_task(
            self._heartbeat_loop(), name="bridge-heartbeat"
        )

        try:
            # Block here until agent disconnects
            await self._agent_reader_loop(reader)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._teardown()

    # ------------------------------------------------------------------
    # Certificate pinning
    # ------------------------------------------------------------------

    async def _verify_fingerprint(self, ssl_obj: ssl.SSLObject) -> None:
        """
        Verify agent certificate fingerprint BEFORE sending authentication token.

        If expected_fingerprint is set, validates against it.
        Otherwise checks known_hosts file (pinning on first connect).

        Raises ConnectionError if the fingerprint changed (possible MITM).
        """
        der = ssl_obj.getpeercert(binary_form=True)
        if not der:
            raise ConnectionError("Agent sent no TLS certificate")

        fp = fingerprint_from_der(der)
        self._last_fingerprint = fp
        fp_norm = fp.replace(":", "").upper()

        if self.expected_fingerprint:
            expected_norm = self.expected_fingerprint.replace(":", "").upper()
            if fp_norm != expected_norm:
                raise ConnectionError(
                    f"TLS fingerprint mismatch with --expected-fingerprint!\n"
                    f"  Expected: {self.expected_fingerprint}\n"
                    f"  Received: {fp}"
                )
            logger.info("Certificate fingerprint verified against --expected-fingerprint ✓")
            return

        known = load_known_fingerprint(self.agent_host, self.agent_port)

        if known is None:
            logger.warning(
                "First connection to %s:%d — pinning fingerprint:\n%s",
                self.agent_host,
                self.agent_port,
                fp,
            )
            save_known_fingerprint(self.agent_host, self.agent_port, fp)
        else:
            known_normalised = known.upper().replace(":", "")
            if known_normalised != fp_norm:
                raise ConnectionError(
                    f"TLS fingerprint MISMATCH for {self.agent_host}:{self.agent_port}!\n"
                    f"  Stored : {known}\n"
                    f"  Received: {fp}\n"
                    "This may indicate a man-in-the-middle attack.\n"
                    "If the agent certificate was regenerated intentionally, delete:\n"
                    f"  {Path.home()}/.config/usbmuxd-bridge/known_hosts"
                )
            logger.debug("Certificate fingerprint verified ✓")

    # ------------------------------------------------------------------
    # UNIX socket server
    # ------------------------------------------------------------------

    async def _start_unix_server(self) -> None:
        """Create (or recreate) the local UNIX socket for libimobiledevice."""
        # Remove stale socket file
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as exc:
                logger.warning("Could not remove stale socket %s: %s", self.socket_path, exc)

        self._unix_server = await asyncio.start_unix_server(
            self._handle_local_client,
            path=self.socket_path,
        )
        try:
            os.chmod(self.socket_path, self.socket_mode)
        except OSError:
            pass

        self._write_active_metadata()

        logger.info(
            "Local UNIX socket ready (mode %o): %s  →  export USBMUXD_SOCKET_ADDRESS=unix:%s",
            self.socket_mode,
            self.socket_path,
            self.socket_path,
        )

    def _write_active_metadata(self) -> None:
        """Write active.json metadata file for automatic discovery by iScan and status bars."""
        import json
        import tempfile
        try:
            from networkusb import __version__
            version = __version__
        except Exception:
            version = "0.1.0"

        active_file = Path.home() / ".cache" / "networkusb" / "active.json"
        active_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": os.getpid(),
            "socket": f"unix:{self.socket_path}",
            "agent_host": self.agent_host,
            "agent_port": self.agent_port,
            "fingerprint": self._last_fingerprint if hasattr(self, "_last_fingerprint") else "",
            "version": version,
        }
        try:
            with tempfile.NamedTemporaryFile("w", dir=active_file.parent, delete=False, encoding="utf-8") as tf:
                json.dump(data, tf)
                tmp_path = tf.name
            os.replace(tmp_path, active_file)
        except Exception as exc:
            logger.debug("Could not write active metadata: %s", exc)

    def _remove_active_metadata(self) -> None:
        """Remove active.json metadata file on bridge teardown."""
        active_file = Path.home() / ".cache" / "networkusb" / "active.json"
        if active_file.exists():
            try:
                active_file.unlink()
            except OSError:
                pass

    async def _handle_local_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle one libimobiledevice connection.

        Creates a new session on the agent and relays bytes bidirectionally.
        """
        session_id = self._session_allocator.next_id(set(self._local_clients.keys()))
        self._local_clients[session_id] = writer
        logger.debug("Local client → session %d", session_id)


        # Notify agent to open a usbmuxd connection
        try:
            await self._send_frame(MsgType.CONNECT, session_id)
        except Exception as exc:
            logger.error("Failed to send CONNECT for session %d: %s", session_id, exc)
            writer.close()
            self._local_clients.pop(session_id, None)
            return

        # Relay local client bytes → agent.
        # Backpressure is provided by writer.drain() (transport-level), not a
        # frame-counting semaphore — a byte tunnel's traffic is not symmetric,
        # so counting in-flight DATA frames against reverse DATA would stall
        # after 100 one-way chunks (see AUDIT F-01).
        try:
            while True:
                data = await reader.read(CHUNK_SIZE)
                if not data:
                    break  # local client closed connection

                await self._send_frame(MsgType.DATA, session_id, data)

        except asyncio.CancelledError:
            # Do NOT swallow cancellation: let it propagate so the task is
            # properly torn down (cleanup below still runs via finally).
            raise
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.debug("Session %d local read error: %s", session_id, exc)
        finally:
            # Tell agent this session is done
            try:
                await self._send_frame(MsgType.CLOSE, session_id)
            except Exception:
                pass
            self._local_clients.pop(session_id, None)
            try:
                writer.close()
            except Exception:
                pass
            logger.debug("Session %d local client closed", session_id)

    # ------------------------------------------------------------------
    # Agent reader loop
    # ------------------------------------------------------------------

    async def _agent_reader_loop(self, reader: asyncio.StreamReader) -> None:
        """
        Read frames from agent and dispatch to the appropriate local client.
        Returns when the agent closes the connection.
        """
        while True:
            msg_type, session_id, payload = await read_frame(reader)

            if msg_type == MsgType.DATA:
                local_writer = self._local_clients.get(session_id)
                if local_writer is None:
                    logger.debug(
                        "DATA for unknown local session %d — sending CLOSE", session_id
                    )
                    try:
                        await self._send_frame(MsgType.CLOSE, session_id)
                    except Exception:
                        pass
                    continue
                try:
                    local_writer.write(payload)
                    await local_writer.drain()
                except Exception as exc:
                    logger.debug(
                        "Session %d write to local client failed: %s", session_id, exc
                    )
                    self._local_clients.pop(session_id, None)

            elif msg_type == MsgType.CLOSE:
                logger.debug("Agent closed session %d", session_id)
                local_writer = self._local_clients.pop(session_id, None)
                if local_writer:
                    try:
                        local_writer.close()
                    except Exception:
                        pass

            elif msg_type == MsgType.HEARTBEAT:
                logger.debug("Heartbeat echo received from agent")

    # ------------------------------------------------------------------
    # Heartbeat sender
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send HEARTBEAT frames to the agent every HEARTBEAT_INTERVAL seconds."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send_frame(MsgType.HEARTBEAT, 0)
                logger.debug("Heartbeat sent to agent")
            except Exception as exc:
                logger.debug("Heartbeat send failed: %s", exc)
                break

    # ------------------------------------------------------------------
    # Shared frame sender
    # ------------------------------------------------------------------

    async def _send_frame(
        self,
        msg_type: MsgType,
        session_id: int,
        payload: bytes = b"",
    ) -> None:
        """Serialise and write one frame to the agent TCP connection."""
        if self._agent_writer is None or self._agent_writer.is_closing():
            raise ConnectionError("Not connected to agent")
        frame = build_frame(msg_type, session_id, payload)
        async with self._write_lock:
            self._agent_writer.write(frame)
            await self._agent_writer.drain()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _teardown(self) -> None:
        """
        Close all resources:
        - Stop local UNIX server
        - Remove socket file
        - Close all local client connections
        - Close agent TCP connection
        """
        # Stop accepting new local clients
        if self._unix_server:
            self._unix_server.close()
            try:
                await self._unix_server.wait_closed()
            except Exception:
                pass
            self._unix_server = None

        # Remove socket file
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self._remove_active_metadata()

        # Close all active local client connections
        n_clients = len(self._local_clients)
        for writer in list(self._local_clients.values()):
            try:
                writer.close()
            except Exception:
                pass
        self._local_clients.clear()
        if n_clients:
            logger.info("Closed %d local client(s)", n_clients)

        # Close agent connection
        if self._agent_writer:
            try:
                self._agent_writer.close()
            except Exception:
                pass
            self._agent_writer = None

    # ------------------------------------------------------------------
    # Backoff generator
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff_generator():
        """Yield 1, 2, 4, 8, 16, 30, 30, 30, ... (seconds)."""
        delay = 1.0
        while True:
            yield delay
            delay = min(delay * 2, 30.0)

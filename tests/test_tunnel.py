"""
Integration test for the full tunnel pipeline.

Topology:
    mock_usbmuxd (UNIX echo server)
        ↑
    AgentServer (TCP+TLS, random port)
        ↑  TCP+TLS
    BridgeClient (→ UNIX socket at bridge_sock)
        ↑
    test client (asyncio UNIX connection)

Flow:
    test sends b"PING" → bridge → agent → mock usbmuxd
    mock responds b"PONG" → agent → bridge → test
    Assert b"PONG" received.

Also tests:
    - Fingerprint is saved on first connect (TOFU)
    - Multiple sequential sessions over the same bridge connection
    - Bridge reconnects automatically after agent restart

Note on socket paths:
    macOS limits AF_UNIX paths to 104 characters. pytest's tmp_path lives
    under /private/var/folders/... which is too long. We use short /tmp paths
    with a unique suffix per test run instead.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from networkusb.agent.server import AgentServer
from networkusb.bridge.client import BridgeClient
from networkusb.tls import (
    generate_self_signed,
    get_fingerprint,
    make_client_ssl_context,
    make_server_ssl_context,
    save_known_fingerprint,
)

TEST_TOKEN = "test-integration-secret-xyz"
ECHO_PAYLOAD = b"hello-from-usbmuxd"
ECHO_RESPONSE = b"pong-" + ECHO_PAYLOAD


# ---------------------------------------------------------------------------
# Mock usbmuxd
# ---------------------------------------------------------------------------


async def start_mock_usbmuxd(path: str) -> asyncio.Server:
    """
    Mock usbmuxd: for every connection, reads up to 4096 bytes and
    echoes back ECHO_RESPONSE, then closes.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(4096)  # consume whatever the client sends
            writer.write(ECHO_RESPONSE)
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def certs(tmp_path):
    """Generate a fresh TLS keypair for the test (cert files, no 104-char limit)."""
    cert_dir = tmp_path / "certs"
    cert_path, key_path = generate_self_signed(cert_dir)
    return cert_path, key_path, cert_dir


@pytest.fixture()
def sock_dir():
    """
    Provide a short temporary directory under /tmp for UNIX socket files.

    macOS limits AF_UNIX socket paths to 104 characters. pytest's tmp_path
    expands to /private/var/folders/... which can easily exceed that.
    Using /tmp keeps paths short.
    """
    d = tempfile.mkdtemp(dir="/tmp", prefix="nusb_")
    yield d
    # Cleanup: remove any leftover socket files and the directory
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def patched_known_hosts(tmp_path, monkeypatch):
    import networkusb.tls as tls_module
    fake_path = tmp_path / "known_hosts"
    monkeypatch.setattr(tls_module, "KNOWN_HOSTS_PATH", fake_path)
    return fake_path


# ---------------------------------------------------------------------------
# Helper: build and start agent + bridge
# ---------------------------------------------------------------------------


async def build_agent(sock_dir: str, certs) -> tuple[AgentServer, asyncio.Task, asyncio.Server]:
    """Start mock usbmuxd + AgentServer using short /tmp socket paths."""
    cert_path, key_path, _ = certs
    usbmuxd_path = os.path.join(sock_dir, "usbmuxd.sock")

    mock_server = await start_mock_usbmuxd(usbmuxd_path)

    server_ssl = make_server_ssl_context(cert_path, key_path)
    agent = AgentServer(
        host="127.0.0.1",
        port=0,  # OS picks a free port
        token=TEST_TOKEN,
        usbmuxd_path=usbmuxd_path,
        ssl_context=server_ssl,
    )
    agent_task = asyncio.create_task(agent.start(), name="agent")
    # Give agent a moment to bind
    await asyncio.sleep(0.15)

    return agent, agent_task, mock_server


async def build_bridge(
    sock_dir: str,
    agent: AgentServer,
    certs,
    patched_known_hosts,
    token: str = TEST_TOKEN,
) -> tuple[BridgeClient, asyncio.Task, str]:
    cert_path, _, _ = certs
    agent_port = agent.bound_port
    assert agent_port is not None
    bridge_sock = os.path.join(sock_dir, "bridge.sock")

    # Pre-populate known_hosts so the bridge doesn't see a TOFU prompt
    fp = get_fingerprint(cert_path)
    save_known_fingerprint("127.0.0.1", agent_port, fp)

    client_ssl = make_client_ssl_context()
    bridge = BridgeClient(
        agent_host="127.0.0.1",
        agent_port=agent_port,
        token=token,
        socket_path=bridge_sock,
        ssl_context=client_ssl,
    )
    bridge_task = asyncio.create_task(bridge.run(), name="bridge")
    # Give bridge time to connect and create the UNIX socket
    await asyncio.sleep(0.3)

    return bridge, bridge_task, bridge_sock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_roundtrip(sock_dir, certs, patched_known_hosts):
    """Data sent by local client reaches mock usbmuxd and response comes back."""
    agent, agent_task, mock_server = await build_agent(sock_dir, certs)
    bridge, bridge_task, bridge_sock = await build_bridge(
        sock_dir, agent, certs, patched_known_hosts
    )

    try:
        reader, writer = await asyncio.open_unix_connection(bridge_sock)
        writer.write(ECHO_PAYLOAD)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        assert data == ECHO_RESPONSE
        writer.close()
    finally:
        bridge_task.cancel()
        agent_task.cancel()
        mock_server.close()
        await asyncio.gather(bridge_task, agent_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_multiple_sequential_sessions(sock_dir, certs, patched_known_hosts):
    """Multiple sequential local-client connections all get correct responses."""
    agent, agent_task, mock_server = await build_agent(sock_dir, certs)
    bridge, bridge_task, bridge_sock = await build_bridge(
        sock_dir, agent, certs, patched_known_hosts
    )

    try:
        for i in range(5):
            reader, writer = await asyncio.open_unix_connection(bridge_sock)
            writer.write(ECHO_PAYLOAD)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert data == ECHO_RESPONSE, f"Session {i} got wrong response"
            writer.close()
            await asyncio.sleep(0.05)
    finally:
        bridge_task.cancel()
        agent_task.cancel()
        mock_server.close()
        await asyncio.gather(bridge_task, agent_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fingerprint_saved_on_first_connect(
    sock_dir, certs, patched_known_hosts
):
    """Bridge saves agent fingerprint to known_hosts on first connection."""
    from networkusb.tls import load_known_fingerprint

    agent, agent_task, mock_server = await build_agent(sock_dir, certs)

    cert_path, _, _ = certs
    agent_port = agent.bound_port
    assert agent_port is not None

    # Do NOT pre-populate known_hosts — let the bridge do it
    client_ssl = make_client_ssl_context()
    bridge_sock = os.path.join(sock_dir, "bridge2.sock")
    bridge = BridgeClient(
        agent_host="127.0.0.1",
        agent_port=agent_port,
        token=TEST_TOKEN,
        socket_path=bridge_sock,
        ssl_context=client_ssl,
    )
    bridge_task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.3)

    fp_saved = load_known_fingerprint("127.0.0.1", agent_port)
    expected = get_fingerprint(cert_path)
    assert fp_saved is not None
    assert fp_saved.upper().replace(":", "") == expected.upper().replace(":", "")

    bridge_task.cancel()
    agent_task.cancel()
    mock_server.close()
    await asyncio.gather(bridge_task, agent_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_reconnects_after_agent_restart(
    sock_dir, certs, patched_known_hosts
):
    """
    Bridge reconnects automatically after agent restarts.
    Reduced wait times for CI — real reconnect delay is 1 s min.
    """
    agent, agent_task, mock_server = await build_agent(sock_dir, certs)
    bridge, bridge_task, bridge_sock = await build_bridge(
        sock_dir, agent, certs, patched_known_hosts
    )

    # Verify initial connection works
    r, w = await asyncio.open_unix_connection(bridge_sock)
    w.write(ECHO_PAYLOAD)
    await w.drain()
    data = await asyncio.wait_for(r.read(4096), timeout=5.0)
    assert data == ECHO_RESPONSE
    w.close()

    # Stop agent + mock usbmuxd. Use the explicit stop() (not task.cancel()):
    # cancelling serve_forever() deadlocks in Python 3.14 when a bridge is
    # attached (see PROBLEMS.md §3 / AUDIT F-07).
    await agent.stop()
    mock_server.close()
    await asyncio.gather(agent_task, return_exceptions=True)

    # Wait briefly for bridge to notice the disconnect
    await asyncio.sleep(0.3)
    assert not os.path.exists(bridge_sock), "Socket should be removed after disconnect"

    # Restart agent on the same port — bridge will reconnect.
    # bound_port was captured at bind time, BEFORE the server was closed.
    usbmuxd_path2 = os.path.join(sock_dir, "usbmuxd2.sock")
    mock_server2 = await start_mock_usbmuxd(usbmuxd_path2)
    cert_path, key_path, _ = certs
    server_ssl = make_server_ssl_context(cert_path, key_path)
    agent_port = agent.bound_port
    assert agent_port is not None

    agent2 = AgentServer(
        host="127.0.0.1",
        port=agent_port,
        token=TEST_TOKEN,
        usbmuxd_path=usbmuxd_path2,
        ssl_context=server_ssl,
    )
    agent2_task = asyncio.create_task(agent2.start())
    # Wait for bridge to reconnect (backoff starts at 1 s)
    await asyncio.sleep(2.5)

    assert os.path.exists(bridge_sock), "Socket should be recreated after reconnect"

    r2, w2 = await asyncio.open_unix_connection(bridge_sock)
    w2.write(ECHO_PAYLOAD)
    await w2.drain()
    data2 = await asyncio.wait_for(r2.read(4096), timeout=5.0)
    assert data2 == ECHO_RESPONSE

    w2.close()
    bridge_task.cancel()
    agent2_task.cancel()
    mock_server2.close()
    await asyncio.gather(bridge_task, agent2_task, return_exceptions=True)


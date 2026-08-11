"""Isolate test_bridge_reconnects_after_agent_restart hang with stack dump."""
import asyncio, os, tempfile, threading, sys
from pathlib import Path
from networkusb.agent.server import AgentServer
from networkusb.bridge.client import BridgeClient
from networkusb.tls import (generate_self_signed, make_server_ssl_context,
                            make_client_ssl_context, get_fingerprint,
                            save_known_fingerprint)
import networkusb.tls as tls_module

TOKEN = "test-integration-secret-xyz"
LOG = "/tmp/nusb_rec.log"
ECHO = b"hello-from-usbmuxd"
PONG = b"pong-" + ECHO

def log(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")

def dump():
    for tid, fr in sys._current_frames().items():
        if tid in (threading.get_ident(),):  # skip self
            continue
        log(f"=== thread {tid} ===")
        while fr:
            log(f"  {fr.f_code.co_filename}:{fr.f_lineno} in {fr.f_code.co_name}")
            fr = fr.f_back

def timer_fire():
    log("=== TIMER dump ===")
    dump()

async def mock_usbmuxd(path):
    async def handle(r, w):
        try:
            data = await r.read(4096)
            w.write(PONG)
            await w.drain()
        finally:
            w.close()
    return await asyncio.start_unix_server(handle, path=path)

async def main():
    threading.Timer(12.0, timer_fire).start()
    log("=== start ===")
    d = tempfile.mkdtemp(dir="/tmp", prefix="nusbrec_")
    cert_path, key_path = generate_self_signed(Path(d))
    fake_kh = Path(d) / "known_hosts"
    tls_module.KNOWN_HOSTS_PATH = fake_kh

    ux = os.path.join(d, "usbmuxd.sock")
    mock = await mock_usbmuxd(ux)
    ssl = make_server_ssl_context(cert_path, key_path)
    agent = AgentServer("127.0.0.1", 0, TOKEN, ux, ssl)
    at = asyncio.create_task(agent.start(), name="agent")
    await asyncio.sleep(0.3)
    port = agent._server.sockets[0].getsockname()[1]
    log(f"agent port={port}")

    fp = get_fingerprint(cert_path)
    save_known_fingerprint("127.0.0.1", port, fp)
    cssl = make_client_ssl_context()
    bsock = os.path.join(d, "bridge.sock")
    bridge = BridgeClient("127.0.0.1", port, TOKEN, bsock, cssl)
    bt = asyncio.create_task(bridge.run(), name="bridge")
    await asyncio.sleep(0.5)
    log("bridge up, socket=" + str(os.path.exists(bsock)))

    # first roundtrip
    r, w = await asyncio.open_unix_connection(bsock)
    w.write(ECHO); await w.drain()
    data = await asyncio.wait_for(r.read(4096), 5)
    log(f"roundtrip1={data!r}")
    w.close()
    log("closed local client w")

    # kill agent + mock
    at.cancel()
    mock.close()
    await asyncio.gather(at, return_exceptions=True)
    log("agent cancelled, mock closed")
    await asyncio.sleep(0.3)
    log("socket exists after kill=" + str(os.path.exists(bsock)))

    # restart agent on same port
    ux2 = os.path.join(d, "usbmuxd2.sock")
    mock2 = await mock_usbmuxd(ux2)
    agent2 = AgentServer("127.0.0.1", port, TOKEN, ux2, ssl)
    at2 = asyncio.create_task(agent2.start(), name="agent2")
    await asyncio.sleep(2.5)
    log("agent2 up, socket=" + str(os.path.exists(bsock)))

    r2, w2 = await asyncio.open_unix_connection(bsock)
    w2.write(ECHO); await w2.drain()
    data2 = await asyncio.wait_for(r2.read(4096), 5)
    log(f"roundtrip2={data2!r}")
    w2.close()
    log("=== SUCCESS ===")

asyncio.run(main())

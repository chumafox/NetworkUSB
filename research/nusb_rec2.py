"""Reconnect scenario; dump asyncio TASK stacks (not just thread) on hang."""
import asyncio, os, tempfile, threading
from pathlib import Path
from networkusb.agent.server import AgentServer
from networkusb.bridge.client import BridgeClient
from networkusb.tls import (generate_self_signed, make_server_ssl_context,
                            make_client_ssl_context, get_fingerprint,
                            save_known_fingerprint)
import networkusb.tls as tls_module

TOKEN = "test-integration-secret-xyz"
LOG = "/tmp/nusb_rec2.log"
ECHO = b"hello-from-usbmuxd"
PONG = b"pong-" + ECHO
_loop = None

def log(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")

def dump_via_loop():
    global _loop
    async def dump():
        for t in asyncio.all_tasks():
            log(f"--- task {t.get_name()} done={t.done()} cancelled={t.cancelled()}")
            for fr in t.get_stack():
                log(f"    {fr.f_code.co_filename}:{fr.f_lineno} in {fr.f_code.co_name}")
    fut = asyncio.run_coroutine_threadsafe(dump(), _loop)
    try:
        fut.result(timeout=5)
    except Exception as e:
        log(f"dump error: {e!r}")

def timer_fire():
    log("=== TIMER dump ===")
    dump_via_loop()

async def mock_usbmuxd(path):
    async def handle(r, w):
        try:
            await r.read(4096)
            w.write(PONG)
            await w.drain()
        finally:
            w.close()
    return await asyncio.start_unix_server(handle, path=path)

async def main():
    global _loop
    _loop = asyncio.get_running_loop()
    threading.Timer(10.0, timer_fire).start()
    log("=== start ===")
    d = tempfile.mkdtemp(dir="/tmp", prefix="nusbrec2_")
    cert_path, key_path = generate_self_signed(Path(d))
    tls_module.KNOWN_HOSTS_PATH = Path(d) / "known_hosts"
    ux = os.path.join(d, "usbmuxd.sock")
    mock = await mock_usbmuxd(ux)
    ssl = make_server_ssl_context(cert_path, key_path)
    agent = AgentServer("127.0.0.1", 0, TOKEN, ux, ssl)
    at = asyncio.create_task(agent.start(), name="agent")
    await asyncio.sleep(0.3)
    port = agent._server.sockets[0].getsockname()[1]
    fp = get_fingerprint(cert_path)
    save_known_fingerprint("127.0.0.1", port, fp)
    bsock = os.path.join(d, "bridge.sock")
    bridge = BridgeClient("127.0.0.1", port, TOKEN, bsock, make_client_ssl_context())
    bt = asyncio.create_task(bridge.run(), name="bridge")
    await asyncio.sleep(0.5)
    r, w = await asyncio.open_unix_connection(bsock)
    w.write(ECHO); await w.drain()
    data = await asyncio.wait_for(r.read(4096), 5)
    log(f"roundtrip1={data!r}")
    w.close()
    await asyncio.sleep(0.2)
    log("=== cancelling agent ===")
    at.cancel()
    mock.close()
    await asyncio.gather(at, return_exceptions=True)
    log("=== gather returned ===")

asyncio.run(main())

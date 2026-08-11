"""Instrumented reconnect scenario: does cancel reach agent finally? Where blocks?"""
import asyncio, logging, os, tempfile, threading
from pathlib import Path
from networkusb.agent.server import AgentServer
from networkusb.bridge.client import BridgeClient
from networkusb.tls import (generate_self_signed, make_server_ssl_context,
                            make_client_ssl_context, get_fingerprint,
                            save_known_fingerprint)
import networkusb.tls as tls_module

TOKEN = "t"
ECHO = b"hello"
PONG = b"pong-" + ECHO
_loop = None

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    filename="/tmp/rec3_debug.log", filemode="w")

def dump():
    async def d():
        for t in asyncio.all_tasks():
            if t.get_name() in ("agent", "bridge", "Task-1"):
                print(f"TASK {t.get_name()} done={t.done()} canc={t.cancelled()}", flush=True)
                for fr in t.get_stack():
                    print(f"    {fr.f_code.co_filename}:{fr.f_lineno} {fr.f_code.co_name}", flush=True)
    fut = asyncio.run_coroutine_threadsafe(d(), _loop)
    fut.result(timeout=5)

def timer():
    print("=== TIMER ===", flush=True)
    dump()

async def mock(path):
    async def h(r, w):
        try:
            await r.read(4096)
            w.write(PONG); await w.drain()
        finally:
            w.close()
    return await asyncio.start_unix_server(h, path=path)

async def main():
    global _loop
    _loop = asyncio.get_running_loop()
    threading.Timer(8.0, timer).start()
    print("start", flush=True)
    d = tempfile.mkdtemp(dir="/tmp", prefix="r3_")
    cert, key = generate_self_signed(Path(d))
    tls_module.KNOWN_HOSTS_PATH = Path(d) / "kh"
    ux = os.path.join(d, "u.sock")
    mock_srv = await mock(ux)
    agent = AgentServer("127.0.0.1", 0, TOKEN, ux, make_server_ssl_context(cert, key))
    at = asyncio.create_task(agent.start(), name="agent")
    await asyncio.sleep(0.3)
    port = agent._server.sockets[0].getsockname()[1]
    save_known_fingerprint("127.0.0.1", port, get_fingerprint(cert))
    bs = os.path.join(d, "b.sock")
    bridge = BridgeClient("127.0.0.1", port, TOKEN, bs, make_client_ssl_context())
    bt = asyncio.create_task(bridge.run(), name="bridge")
    await asyncio.sleep(0.5)
    r, w = await asyncio.open_unix_connection(bs)
    w.write(ECHO); await w.drain()
    got = await asyncio.wait_for(r.read(4096), 5)
    print("roundtrip", got, flush=True)
    w.close()
    await asyncio.sleep(0.2)
    print("cancelling agent; _bridge_tasks size=", len(agent._bridge_tasks), flush=True)
    at.cancel()
    mock_srv.close()
    try:
        await asyncio.wait_for(asyncio.gather(at, return_exceptions=True), 6)
        print("GATHER OK", flush=True)
    except asyncio.TimeoutError:
        print("GATHER TIMEOUT", flush=True)
        dump()

asyncio.run(main())

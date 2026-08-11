"""Does server.close() + wait_closed() stop cleanly with a pending TLS conn?"""
import asyncio, socket, tempfile
from pathlib import Path
from networkusb.tls import generate_self_signed, make_server_ssl_context

async def bridge_handler(reader, writer):
    try:
        await reader.read()
    except asyncio.CancelledError:
        raise
    finally:
        writer.close()

async def main():
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="sf3_")) / "certs"
    cert, key = generate_self_signed(d)
    srv = await asyncio.start_server(bridge_handler, "127.0.0.1", 0, ssl=make_server_ssl_context(cert, key))
    port = srv.sockets[0].getsockname()[1]
    sf = asyncio.create_task(srv.serve_forever(), name="sf")
    await asyncio.sleep(0.3)
    raw = socket.create_connection(("127.0.0.1", port))  # pending TLS handshake
    await asyncio.sleep(0.3)
    print("calling srv.close() + await wait_closed()...", flush=True)
    srv.close()
    try:
        await asyncio.wait_for(srv.wait_closed(), 3.0)
        print("wait_closed() returned (clean shutdown)", flush=True)
    except asyncio.TimeoutError:
        print(">>> wait_closed() TIMEOUT with pending conn", flush=True)
    # is serve_forever task now done?
    print("after close: sf.done()=", sf.done(), flush=True)
    raw.close()

asyncio.run(main())

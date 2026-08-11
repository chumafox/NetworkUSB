"""Does serve_forever cancel cleanly when an active handler is attached?
Simulates the real reconnect scenario: a TLS-ish server with a live bridge
handler blocked reading, then cancels the serve_forever task."""
import asyncio, socket, threading, time, sys
from pathlib import Path
from networkusb.tls import generate_self_signed, make_server_ssl_context

async def bridge_handler(reader, writer):
    print("  [handler] started, blocked in read...", flush=True)
    try:
        await reader.read()
    except asyncio.CancelledError:
        print("  [handler] got CancelledError", flush=True)
        raise
    finally:
        print("  [handler] finally: closing writer", flush=True)
        try:
            writer.close()
            await writer.wait_closed()
            print("  [handler] writer fully closed", flush=True)
        except Exception as e:
            print("  [handler] wait_closed raised:", repr(e), flush=True)

async def main():
    d = Path(tempfile()) / "certs"
    cert, key = generate_self_signed(d)
    ssl = make_server_ssl_context(cert, key)
    srv = await asyncio.start_server(bridge_handler, "127.0.0.1", 0, ssl=ssl)
    port = srv.sockets[0].getsockname()[1]
    print(f"server on {port}", flush=True)

    sf = asyncio.create_task(srv.serve_forever(), name="serve_forever")
    await asyncio.sleep(0.3)

    # open a raw connection to the TLS port (handler will block on read)
    raw = socket.create_connection(("127.0.0.1", port))
    print("raw connection opened", flush=True)
    await asyncio.sleep(0.5)
    print("cancelling serve_forever task...", flush=True)
    sf.cancel()
    try:
        await asyncio.wait_for(sf, 3.0)
        print("serve_forever completed on cancel", flush=True)
    except asyncio.CancelledError:
        print("serve_forever raised CancelledError", flush=True)
    except asyncio.TimeoutError:
        print(">>> TIMEOUT: serve_forever did NOT stop on cancel", flush=True)
    print("sf.cancelled()=", sf.cancelled(), "sf.done()=", sf.done(), flush=True)
    raw.close()
    srv.close()

def tempfile():
    import tempfile
    return tempfile.mkdtemp(dir="/tmp", prefix="sf2_")

asyncio.run(main())

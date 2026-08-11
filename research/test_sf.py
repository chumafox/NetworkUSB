"""Test whether asyncio.Server.serve_forever() swallows cancellation in this Python."""
import asyncio, sys

async def handler(r, w):
    try:
        await r.read()
    finally:
        w.close()

async def main():
    print(f"python {sys.version.split()[0]}")
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    t = asyncio.create_task(srv.serve_forever(), name="sf")
    await asyncio.sleep(0.3)
    print("cancelling serve_forever task...")
    t.cancel()
    try:
        await asyncio.wait_for(t, 2.0)
        print("serve_forever task completed normally (no exception)")
    except asyncio.CancelledError:
        print("serve_forever raised CancelledError as expected")
    except asyncio.TimeoutError:
        print("TIMEOUT: serve_forever did not stop on cancel (SWALLOWED)")
    print("task.cancelled() =", t.cancelled())
    print("task.done() =", t.done())
    srv.close()

asyncio.run(main())

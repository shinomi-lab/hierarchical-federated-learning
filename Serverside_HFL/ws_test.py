import asyncio, websockets
async def main():
    uri = 'ws://localhost:8001/ws/updates?token=secret-token'
    try:
        async with websockets.connect(uri) as ws:
            print("Connected:", uri)
            await ws.send("hello")
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                print("Received:", msg)
            except asyncio.TimeoutError:
                print("No message within 2s; connection OK")
    except Exception as e:
        print("WS error:", e)
asyncio.run(main())

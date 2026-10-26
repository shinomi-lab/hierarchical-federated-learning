import asyncio
import websockets
import json

async def run():
    uri = "ws://127.0.0.1:9001/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"device_id": "device-123", "endpoint": "http://device.local"}))
        while True:
            msg = await ws.recv()
            print("RECV:", msg)


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(run())

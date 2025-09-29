import json
from contextlib import asynccontextmanager
from typing import Literal, Optional
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from friendlywords.friendlywords import FriendlyWords
import redis.asyncio as redis
from starlette.websockets import WebSocketState

ROOM_EXPIRE = 5 * 60

r: redis.Redis | None = None
words = FriendlyWords("")


@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    global r
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    words.preload()
    yield
    if r is None: return
    await r.aclose()
    r = None

app = FastAPI(lifespan=lifespan)


# @app.get("/rooms/create")
# async def create_room():
#     while True:
#         room_id = words.generate(6, separator="-")
#         if await r.get(f"room:{room_id}") is None:
#             break
#
#     await r.set(f"room:{room_id}", "", ex=ROOM_EXPIRE)
#     return {"room_id": room_id, "ex": ROOM_EXPIRE}


async def safe_ws_close(ws: WebSocket, code: int = 1000):
    try:
        state = getattr(ws, "application_state", None) or getattr(ws, "client_state", None)
        if state == WebSocketState.CONNECTED:
            await ws.close(code=code)
    except RuntimeError:
        pass


async def pubsub_forward(ws: WebSocket, subscribe_channel: str, publish_channel: str, room_id: str):
    pubsub = r.pubsub()
    await pubsub.subscribe(subscribe_channel)

    async def reader():
        async for message in pubsub.listen():
            if message["type"] == "message":
                await ws.send_text(message["data"])
                print("received " + message["data"])

    read_task = asyncio.create_task(reader())

    try:
        while True:
            data = await ws.receive_text()
            await r.publish(publish_channel, data)
            print("published " + data)
    except Exception:
        pass
    finally:
        read_task.cancel()
        await pubsub.unsubscribe(subscribe_channel)
        await safe_ws_close(ws)

        try:
            keys = [f"{room_id}:server", f"{room_id}:client"]
            subscribers = [await r.pubsub_numsub(key) for key in keys]
            if all(count == 0 for _, count in subscribers):
                await r.delete(f"room:{room_id}")
        except Exception:
            pass


@app.websocket("/ws/rooms")
async def websocket_endpoint(ws: WebSocket, role: Literal["server", "client"] = Query(), room_id: Optional[str] = None):
    await ws.accept()

    if room_id is None:
        while True:
            room_id = words.generate(3, separator="-")
            exists = await r.get(f"room:{room_id}")
            if not exists:
                await r.set(f"room:{room_id}", "", ex=ROOM_EXPIRE)
                break
        await ws.send_text(json.dumps({"type": "room_id", "value": room_id}))
    else:
        try:
            exists = await r.get(f"room:{room_id}")
            if not exists is not None:
                await safe_ws_close(ws, code=1008)
                return
            else:
                await r.expire(f"room:{room_id}", ROOM_EXPIRE)
        except Exception:
            await safe_ws_close(ws, code=1011)
            return

    subscribe_channel = f"room:{room_id}:{role}"
    publish_channel = f"room:{room_id}:{'server' if role == 'client' else 'client'}"

    await pubsub_forward(ws, subscribe_channel, publish_channel, f"room:{room_id}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

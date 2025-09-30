import json
from contextlib import asynccontextmanager
from typing import Literal, Optional
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from friendlywords.friendlywords import FriendlyWords
import redis.asyncio as redis
from starlette.websockets import WebSocketState
import os

ROOM_EXPIRE = 5 * 60

r: redis.Redis | None = None
words = FriendlyWords("")


@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    global r
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    words.preload()
    yield
    if r is None: return
    await r.aclose()
    r = None

app = FastAPI(lifespan=lifespan)


async def safe_ws_close(ws: WebSocket, code: int = 1000):
    try:
        state = getattr(ws, "application_state", None) or getattr(ws, "client_state", None)
        if state == WebSocketState.CONNECTED:
            await ws.close(code=code)
    except Exception:
        pass


async def pubsub_forward(ws: WebSocket, subscribe_channel: str, publish_channel: str):
    if r is None:
        await safe_ws_close(ws, code=1011)
        return
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(subscribe_channel)

        async def reader():
            try:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    payload = message.get("data")
                    try:
                        if payload == "disconnect":
                            await safe_ws_close(ws)
                            break
                    except Exception:
                        # Fall through
                        pass
                    try:
                        await ws.send_text(payload)
                    except (WebSocketDisconnect, RuntimeError):
                        break
            except asyncio.CancelledError:
                # Expected on shutdown
                pass
            except Exception:
                # Reader errors
                pass

        read_task = asyncio.create_task(reader())

        try:
            while True:
                data = await ws.receive_text()
                try:
                    await r.publish(publish_channel, data)
                except Exception:
                    # If Redis is unavailable
                    break
        except (WebSocketDisconnect, RuntimeError):
            try:
                await r.publish(publish_channel, "disconnect")
            except Exception:
                pass
        finally:
            read_task.cancel()
            # Ensure the reader finishes
            await asyncio.gather(read_task, return_exceptions=True)
            try:
                await pubsub.unsubscribe(subscribe_channel)
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
            await safe_ws_close(ws)

    except Exception:
        # If subscribe failed
        try:
            await pubsub.aclose()
        except Exception:
            pass
        await safe_ws_close(ws, code=1011)


@app.websocket("/ws/rooms")
async def websocket_endpoint(ws: WebSocket, role: Literal["server", "client"] = Query(), room_id: Optional[str] = None):
    await ws.accept()

    if room_id is None:
        while True:
            room_id = words.generate(3, separator="-")
            try:
                # NX ensures no race
                claimed = await r.set(f"room:{room_id}", "", ex=ROOM_EXPIRE, nx=True)
            except Exception:
                await safe_ws_close(ws, code=1011)
                return
            if claimed:
                break
        await ws.send_text(json.dumps({"type": "room_id", "value": room_id}))
    else:
        try:
            exists = await r.get(f"room:{room_id}")
            if exists is None:
                await safe_ws_close(ws, code=1008)
                return
            else:
                await r.expire(f"room:{room_id}", ROOM_EXPIRE)
        except Exception:
            await safe_ws_close(ws, code=1011)
            return

    subscribe_channel = f"room:{room_id}:{role}"
    publish_channel = f"room:{room_id}:{'server' if role == 'client' else 'client'}"

    await pubsub_forward(ws, subscribe_channel, publish_channel)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

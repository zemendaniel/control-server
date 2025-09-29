from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
from friendlywords.friendlywords import FriendlyWords
import redis.asyncio as redis
from starlette.websockets import WebSocketState

ROOM_EXPIRE = 5 * 60

r: redis.Redis | None = None

@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    global r
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    yield
    if r is None: return
    await r.aclose()
    r = None

app = FastAPI(lifespan=lifespan)
words = FriendlyWords("")
words.preload()


class WebsocketHandler:
    websockets: dict[str, dict[str, WebSocket]] = {}

    @staticmethod
    async def send_to_other_in_room(room_id: str, message: str, own_role: Literal["server", "client"]):
        receiver_role = WebsocketHandler.other_role(own_role)
        ws = WebsocketHandler.websockets.get(room_id, {}).get(receiver_role)
        if not ws: return

        try:
            await ws.send_text(message)
        except (RuntimeError, WebSocketDisconnect):
            room = WebsocketHandler.websockets.get(room_id)
            if room and room.get(receiver_role) is ws:
                del room[receiver_role]
                if not room.get("client") and not room.get("server"):
                    try:
                        await r.delete(f"room:{room_id}")
                    finally:
                        WebsocketHandler.websockets.pop(room_id, None)

    @staticmethod
    def other_role(role: Literal["server", "client"]):
        return "server" if role == "client" else "client"


@app.get("/rooms/create")
async def create_room():
    while True:
        room_id = words.generate(6, separator="-")
        if await r.get(f"room:{room_id}") is None:
            break

    await r.set(f"room:{room_id}", "", ex=ROOM_EXPIRE)
    return {"room_id": room_id, "ex": ROOM_EXPIRE}


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, role: Literal["server", "client"]):
    if await r.get(f"room:{room_id}") is None:
        try:
            await websocket.close()
        except RuntimeError:
            pass
        return
    else:
        await r.expire(f"room:{room_id}", ROOM_EXPIRE)

    await websocket.accept()

    if room_id not in WebsocketHandler.websockets:
        WebsocketHandler.websockets[room_id] = {}
    WebsocketHandler.websockets[room_id][role] = websocket

    try:
        while True:
            try:
                data = await websocket.receive_text()
                print("received " + data)
                await WebsocketHandler.send_to_other_in_room(room_id, data, role)
            except WebSocketDisconnect:
                break
            except Exception:
                break

    finally:
        try:
            room = WebsocketHandler.websockets.get(room_id)
            if not room:
                pass
            elif role in room:
                del room[role]

                other_role = "client" if role == "server" else "server"
                other_ws = room.get(other_role)
                if other_ws:
                    try:
                        state = getattr(other_ws, "application_state", None) or getattr(other_ws, "client_state", None)
                        if state == WebSocketState.CONNECTED:
                            await other_ws.close()
                    except RuntimeError:
                        pass
                    finally:
                        room.pop(other_role, None)

                if not room.get("client") and not room.get("server"):
                    try:
                        await r.delete(f"room:{room_id}")
                    finally:
                        WebsocketHandler.websockets.pop(room_id, None)

        finally:
                try:
                    state = getattr(websocket, "application_state", None) or getattr(websocket, "client_state", None)
                    if state == WebSocketState.CONNECTED:
                        await websocket.close()
                except RuntimeError:
                    pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)



import json
from contextlib import asynccontextmanager
from utils import choose_peer_ip
from fastapi import FastAPI, HTTPException, WebSocket
import uvicorn
from models import NewRoom, JoinRoom
from friendlywords.friendlywords import FriendlyWords
import redis.asyncio as redis

ROOM_EXPIRE = 5 * 60

r: redis.Redis | None = None

@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    global r
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    yield
    await r.close()
    await r.connection_pool.disconnect()

app = FastAPI(lifespan=lifespan)
words = FriendlyWords("")
words.preload()


@app.post("/rooms/create")
async def create_room(data: NewRoom):
    while True:
        room_id = words.generate(6, separator="-")
        if await r.get(f"room:{room_id}") is None:
            break

    room = {
        "server": data.model_dump(mode="json"),
        "client": None,
    }

    await r.set(f"room:{room_id}", json.dumps(room), ex=ROOM_EXPIRE)
    return {"room_id": room_id, "ex": ROOM_EXPIRE}


@app.post("/rooms/join/{room_id}")
async def join_room(room_id: str, data: JoinRoom):
    room = await r.get(f"room:{room_id}")
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    room = json.loads(room)
    if room.get("client") is not None:
        raise HTTPException(status_code=400, detail="Room already joined")

    room["client"] = data.model_dump(mode="json")
    await r.set(f"room:{room_id}", json.dumps(room), ex=ROOM_EXPIRE)

    server = room["server"]
    client = room["client"]

    server_ip = choose_peer_ip(server, client)
    client_ip = choose_peer_ip(client, server)

    client_info_for_server = {
        "name": client["name"],
        "ip": client_ip,
        "port": client["port"]
    }
    server_info_for_client = {
        "name": server["name"],
        "ip": server_ip,
        "port": server["port"]
    }

    # publish client info for server
    await r.publish(f"room:{room_id}", json.dumps(client_info_for_server))

    # return server info for the client
    return server_info_for_client

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    room = await r.get(f"room:{room_id}")
    if room is None:
        await websocket.close()
        return

    await websocket.accept()
    pubsub = r.pubsub()
    await pubsub.subscribe(f"room:{room_id}")
    # todo test what happens if room expires while server is listening
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
                break
    finally:
        await pubsub.unsubscribe(f"room:{room_id}")
        await pubsub.close()
        await websocket.close()



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)



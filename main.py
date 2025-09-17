import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
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


@app.post("/rooms")
async def create_room(data: NewRoom):
    while True:
        room_id = words.generate(6, separator="-")
        if await r.get(f"room:{room_id}") is None:
            break

    room = {
        "server": data.model_dump(mode="json"),
    }

    await r.set(f"room:{room_id}", json.dumps(room), ex=ROOM_EXPIRE)
    return {"room_id": room_id, "ex": ROOM_EXPIRE}


@app.post("/rooms/{room_id}")
async def get_room(room_id: str, data: JoinRoom):
    room = await r.get(f"room:{room_id}")
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    room = json.loads(room)
    room["client"] = data.model_dump(mode="json")
    await r.set(f"room:{room_id}", json.dumps(room), ex=ROOM_EXPIRE)

    return None


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

    # TODO: websockets


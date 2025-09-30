# todo: slowapi, scaling
import json
from contextlib import asynccontextmanager
from typing import Literal, Optional
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from friendlywords.friendlywords import FriendlyWords
import redis.asyncio as redis
from starlette.websockets import WebSocketState
import os
import logging

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("control-server")


ROOM_EXPIRE = int(os.getenv("ROOM_EXPIRE", 300))

r: redis.Redis | None = None
words = FriendlyWords("")


@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    global r
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    try:
        r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        # Test connection
        await r.ping()
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise
    words.preload()
    try:
        yield
    finally:
        if r is not None:
            try:
                await r.aclose()
            except Exception:
                logger.warning("Error while closing Redis connection", exc_info=True)
        r = None

app = FastAPI(lifespan=lifespan)


async def safe_ws_close(ws: WebSocket, code: int = 1000, reason: str = ""):
    try:
        state = getattr(ws, "application_state", None) or getattr(ws, "client_state", None)
        if state == WebSocketState.CONNECTED:
            await ws.close(code=code, reason=reason)
    except Exception:
        pass


async def pubsub_forward(ws: WebSocket, subscribe_channel: str, publish_channel: str):
    """Forward messages between WebSocket and Redis pub/sub channels."""
    if r is None:
        logger.error("Redis unavailable in pubsub_forward")
        await safe_ws_close(ws, code=1011)
        return

    pubsub = r.pubsub()
    await pubsub.subscribe(subscribe_channel)

    close_code = 1000
    close_reason = ""

    async def reader():
        """Read from Redis and send to WebSocket."""
        nonlocal close_code, close_reason
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue

                payload = message.get("data")

                # Handle control messages
                if payload == "disconnect":
                    close_reason = "Peer disconnected"
                    break
                if payload == "timeout":
                    close_code = 1001
                    close_reason = "Room expired"
                    break

                # Forward regular messages
                await ws.send_text(payload)

        except (WebSocketDisconnect, RuntimeError):
            logger.debug("WebSocket closed during read")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Reader error", exc_info=True)

    async def writer():
        """Read from WebSocket and publish to Redis."""
        nonlocal close_code, close_reason
        try:
            while True:
                data = await asyncio.wait_for(ws.receive_text(), timeout=ROOM_EXPIRE)

                if len(data) > 64 * 1024:
                    close_code = 1008
                    close_reason = "Message too large"
                    break

                await r.publish(publish_channel, data)

        except asyncio.TimeoutError:
            close_code = 1001
            close_reason = "Room expired"
            await r.publish(publish_channel, "timeout")
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # Run reader and writer concurrently
    read_task = asyncio.create_task(reader())
    write_task = asyncio.create_task(writer())

    try:
        # Wait for either task to complete (one closing means both should close)
        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

    finally:
        # Notify peer we're disconnecting (if not already notified)
        if close_code != 1001:  # Don't send disconnect if we sent timeout
            await r.publish(publish_channel, "disconnect")

        # Cleanup
        await pubsub.unsubscribe(subscribe_channel)
        await pubsub.aclose()
        await safe_ws_close(ws, code=close_code, reason=close_reason)


@app.websocket("/ws/rooms")
async def websocket_endpoint(ws: WebSocket, role: Literal["server", "client"] = Query(), room_id: Optional[str] = None):
    await ws.accept()
    logger.info(f"WebSocket accepted: role={role}, room_id={room_id}")

    if r is None:
        logger.error("Redis unavailable in websocket_endpoint")
        await safe_ws_close(ws, code=1011)
        return

    if room_id is None and role == "client":
        await safe_ws_close(ws, code=1008, reason="Clients must provide room id")
        return

    if room_id is not None and role == "server":
        await safe_ws_close(ws, code=1008, reason="Servers cannot provide room id")
        return

    # Server creating a new room
    if role == "server":
        max_attempts = 1000
        for _ in range(max_attempts):
            room_id = words.generate(4, separator="-")
            try:
                claimed = await r.set(f"room:{room_id}:server", "connected", ex=ROOM_EXPIRE, nx=True)
                if claimed:
                    await ws.send_text(json.dumps({
                        "type": "room_info",
                        "value": {"id": room_id, "ex": ROOM_EXPIRE}
                    }))
                    break
            except Exception:
                await safe_ws_close(ws, code=1011)
                return
        else:
            logger.critical("Failed to claim room after max attempts")
            await safe_ws_close(ws, code=1011)
            return

    # Client joining existing room
    else:
        try:
            pipe = r.pipeline()
            await pipe.exists(f"room:{room_id}:server")
            await pipe.set(f"room:{room_id}:client", "connected", ex=ROOM_EXPIRE, nx=True)
            server_exists, client_claimed = await pipe.execute()

            if not server_exists:
                await safe_ws_close(ws, code=1008, reason="This room does not exist")
                return

            if not client_claimed:
                await safe_ws_close(ws, code=1008, reason="This room is already in use")
                return

        except Exception:
            await safe_ws_close(ws, code=1011)
            return

    subscribe_channel = f"room:{room_id}:{role}"
    publish_channel = f"room:{room_id}:{'server' if role == 'client' else 'client'}"

    try:
        await pubsub_forward(ws, subscribe_channel, publish_channel)
    # Cleanup
    finally:
        try:
            await r.delete(f"room:{room_id}:{role}")
        except Exception:
            logger.error("Failed to delete room", exc_info=True)


@app.get("/health")
async def health():
    if r is None:
        return {"status": "unhealthy", "redis": "disconnected"}, 503
    try:
        await r.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception:
        return {"status": "unhealthy", "redis": "error"}, 503


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

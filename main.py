import asyncio

import logging as log
import os
from pathlib import Path
import time
import traceback

from cfg import User
from cfg.Client import Client, Lobby, LobbyModel, Message, Room, get_main_lobby
from cfg.consts import SERVER_VERSION
from cfg.users import all_users
from cfg.db import db
log.basicConfig(level=log.DEBUG)

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

from beanie import Document, Indexed, init_beanie


HOST_NAME = os.environ.get("CGF_HOST_NAME", "0.0.0.0")
TCP_PORT = int(os.environ.get("CGF_PORT", "15277"))

loop = asyncio.new_event_loop()

clients: set[Client] = set()

async def main():
    await init_beanie(database=db.cgf_db, document_models=[
        User, Message, LobbyModel
        # Lobby, Room
    ])
    start_user_load = time.time()
    async for user in User.find_all():
        all_users[user.uid] = user
    log.info(f"Loaded {len(all_users)} users in {(time.time() - start_user_load) * 1000:.1f} ms")
    asyncio.create_task(get_main_lobby())  # init main lobby

    # start socket server and run forever
    server_coro = await asyncio.start_server(connection_cb, HOST_NAME, TCP_PORT)
    log.info(f"[version: {SERVER_VERSION}] Starting server: {HOST_NAME}:{TCP_PORT}")
    async with server_coro:
        await server_coro.serve_forever()

async def connection_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    c = Client(reader, writer, await get_main_lobby())
    clients.add(c)
    try:
        await c.main_loop()
    except Exception as e:
        if c.disconnected:
            return
        log.warn(f"Exception in client main loop: {e}\n{''.join(traceback.format_exception(e))}")

if __name__ == "__main__":
    asyncio.run(main())

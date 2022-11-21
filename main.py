import asyncio
import logging as log
import os
from pathlib import Path
import sys
import time
import traceback
import signal

from cfg import User
from cfg.Client import Client, ChatMessages, Lobby, LobbyModel, Message, Room, GameInstance, get_main_lobby, all_clients, populate_all_lobbies, all_lobbies, all_users
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


def cleanup_clients(*args):
    _clients = list(all_clients)
    for client in _clients:
        log.info(f"Disconnecting client: {client.client_ip}")
        client.disconnect()
    all_clients.clear()
    del _clients
    log.info(f"Disconnected all clients")
    sys.exit(0)


async def main():
    signal.signal(signal.SIGTERM, cleanup_clients)
    signal.signal(signal.SIGINT, cleanup_clients)

    await init_beanie(database=db.cgf_db, document_models=[
        User, Message, LobbyModel,
        ChatMessages,
        Room, GameInstance
        # Lobby, Room
    ])
    start_user_load = time.time()
    async for user in User.find_all():
        all_users[user.uid] = user
    log.info(f"Loaded {len(all_users)} users in {(time.time() - start_user_load) * 1000:.1f} ms")
    # asyncio.create_task(get_main_lobby())  # init main lobby
    start_lobby_load = time.time()
    await populate_all_lobbies()
    log.info(f"Loaded {len(all_lobbies)} lobbies in {(time.time() - start_lobby_load) * 1000:.1f} ms")


    # start socket server and run forever
    server_coro = await asyncio.start_server(connection_cb, HOST_NAME, TCP_PORT)
    log.info(f"[version: {SERVER_VERSION}] Starting server: {HOST_NAME}:{TCP_PORT}")
    async with server_coro:
        await server_coro.serve_forever()

async def connection_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    c = Client(reader, writer, await get_main_lobby())
    try:
        await c.main_loop()
    except Exception as e:
        if c.disconnected:
            return
        log.warn(f"Exception in client main loop: {e}\n{''.join(traceback.format_exception(e))}")

if __name__ == "__main__":
    asyncio.run(main())

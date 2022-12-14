import asyncio
import logging as log
import os
from pathlib import Path
import sys
import time
import traceback
import signal
import urllib3
import threading

from cgf.Client import Client, ChatMessages, Lobby, LobbyModel, Message, Room, GameSession, get_main_lobby, all_clients, populate_all_lobbies, all_lobbies
from cgf.NadeoApi import run_club_room_creation_test, run_nadeo_services_auth
import cgf.RandomMapCacher as RMC
from cgf.User import User
from cgf.models.MapPack import MapPack
from cgf.models.RandomMapQueue import RandomMapQueue
from cgf.users import all_users
from cgf.consts import LOCAL_DEV_MODE, SERVER_VERSION, SHUTDOWN, SHUTDOWN_EVT
from cgf.models.Map import Map
from cgf.users import all_users
from cgf.db import db
from cgf.utils import timeit_context
# log.basicConfig(level=log.DEBUG)
log.basicConfig(
    format='%(asctime)s [%(levelname)-8s] %(message)s',
    level=log.DEBUG,
    datefmt='%Y-%m-%d %H:%M:%S')

log.getLogger('boto').setLevel(log.WARNING)
log.getLogger('boto3').setLevel(log.WARNING)
log.getLogger('boto3.resources.factory').setLevel(log.WARNING)
log.getLogger('boto3.resources.action').setLevel(log.WARNING)
log.getLogger('botocore').setLevel(log.WARNING)
log.getLogger('urllib3.connectionpool').setLevel(log.WARNING)

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel

from beanie import Document, Indexed, init_beanie


urllib3.connectionpool

HOST_NAME = os.environ.get("CGF_HOST_NAME", "0.0.0.0")
TCP_PORT = int(os.environ.get("CGF_PORT", "15277"))

loop = asyncio.new_event_loop()

def cleanup_clients(*args):
    global SHUTDOWN, SHUTDOWN_EVT
    SHUTDOWN = True
    SHUTDOWN_EVT.set()
    _clients = list(all_clients)
    log.warning(f"Shutting down")
    for client in _clients:
        log.info(f"Disconnecting client: {client.client_ip}")
        client.disconnect()
    all_clients.clear()
    del _clients
    log.info(f"Disconnected all clients")
    sys.exit(0)


MAIN_INIT_DONE = False

MAIN_DB_NAME = os.environ.get("CGF_DB_NAME", "cgf_db")


async def main():
    global MAIN_INIT_DONE
    signal.signal(signal.SIGTERM, cleanup_clients)
    signal.signal(signal.SIGINT, cleanup_clients)

    log.info(f"[version: {SERVER_VERSION}] Starting server: {HOST_NAME}:{TCP_PORT}")
    asyncio.create_task(run_nadeo_services_auth())
    # if LOCAL_DEV_MODE:
    #     await run_club_room_creation_test()
    #     return

    with timeit_context("init_beanie"):
        await init_beanie(database=db[MAIN_DB_NAME], document_models=[
            User, Message, LobbyModel,
            ChatMessages,
            Room, GameSession,
            Map, MapPack,
            RandomMapQueue,
        ], allow_index_dropping=True)

    with timeit_context("Load cached fresh maps"):
        await RMC.init_fresh_maps_from_db()

    with timeit_context("Load all users"):
        async for user in User.find_all():
            all_users[user.uid] = user
    log.info(f"Loaded {len(all_users)} users")
    # asyncio.create_task(get_main_lobby())  # init main lobby
    with timeit_context("Load lobbies"):
        await populate_all_lobbies()
    log.info(f"Loaded {len(all_lobbies)} lobbies")

    count_rooms = 0
    with timeit_context("Load rooms"):
        for lobby in all_lobbies.values():
            # await lobby.initialized()
            await lobby.load_rooms_task
            count_rooms += len(lobby.rooms)

    count_games = 0
    with timeit_context("Load games"):
        for lobby in all_lobbies.values():
            for room in lobby.rooms.values():
                await room.initialized()
                count_games += 1 if room.game is not None else 0

    with timeit_context("Load known maps"):
        await RMC.init_known_maps()

    with timeit_context("Get latest maps from TMX"):
        log.info(f"Getting latest maps from TMX")
        await RMC.add_latest_maps()

    asyncio.create_task(RMC.maintain_random_maps())
    asyncio.create_task(RMC.maintain_totd_maps())

    for l in all_lobbies.values():
        await l.initialized()
        for r in l.rooms.values():
            await r.initialized()

    log.info(f"Loaded {count_rooms} total rooms with {count_games} total games")


    MAIN_INIT_DONE = True

    # start socket server and run forever
    server = await asyncio.start_server(connection_cb, HOST_NAME, TCP_PORT)
    async with server:
        await server.serve_forever()

    cleanup_clients()



async def connection_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    while not MAIN_INIT_DONE:
        await asyncio.sleep(0.05)
    c = Client(reader, writer)
    try:
        await c.main_loop()
    except Exception as e:
        if c.disconnected:
            return
        log.warn(f"Exception in client main loop: {e}\n{''.join(traceback.format_exception(e))}")

if __name__ == "__main__":
    log.info("Server starting...")
    asyncio.run(main())

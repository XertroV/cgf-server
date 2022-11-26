import asyncio
import logging
import queue
import random
import time
import botocore

import aiohttp
from beanie.operators import In

from cgf.consts import SERVER_VERSION
from cgf.http import get_session
from cgf.models.Map import LONG_MAP_SECS, Map, MapJustID
from cgf.db import s3, s3_bucket_name, s3_client

fresh_random_maps: list[Map] = list()
maps_to_cache: list[Map] = list()
known_maps: set[int] = set()

MAINTAIN_N_MAPS = 20   #200

async def init_known_maps():
    _maps = await Map.find_all(projection_model=MapJustID).to_list()
    known_maps.update([m.TrackID for m in _maps])
    logging.info(f"Known maps: {len(known_maps)}")
    asyncio.create_task(ensure_known_maps_cached())

async def ensure_known_maps_cached():
    _known_maps = set(known_maps)
    cached_maps = await asyncio.get_event_loop().run_in_executor(None, _get_bucket_keys)
    uncached = _known_maps - cached_maps
    logging.info(f"Getting {len(uncached)} uncached but known maps")
    for i, track_id in enumerate(uncached):
        await asyncio.sleep(0.300) # sleep 300 ms between checks
        asyncio.create_task(cache_map(track_id))

def _get_bucket_keys() -> set[int]:
    cached_maps = set()
    for k in s3_client.list_objects(Bucket=s3_bucket_name)['Contents']:
        try:
            cached_maps.add(int(k['Key'].split('.Map.Gbx')[0]))
        except Exception as e:
            print(f"Got exception getting key: {e}")
    return cached_maps

async def cache_map(track_id: int, force = False, delay_ms=0):
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    map_cached = await is_map_cached(track_id)
    if force or not map_cached:
        await _download_and_cache_map(track_id)

async def is_map_cached(track_id: int):
    return await asyncio.get_event_loop().run_in_executor(None, is_map_cached_blocking, track_id)

def is_map_cached_blocking(track_id: int):
    map_file = f"{track_id}.Map.Gbx"
    try:
        s3.Object(s3_bucket_name, map_file).load()
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "404":
            return False
        else: raise e
    # does exist
    return True

async def _download_and_cache_map(track_id: int):
    map_file = f"{track_id}.Map.Gbx"
    logging.info(f"Caching map: {map_file}")
    # does not exist
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/maps/download/{track_id}") as resp:
            if resp.status == 200:
                map_bs = await resp.content.read()
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: s3.Object(s3_bucket_name, map_file).put(
                        ACL='public-read',
                        Body=map_bs
                    )
                )
                logging.info(f"Uploaded map to s3 cache: {map_file}")


async def maintain_random_maps():
    while True:
        if len(fresh_random_maps) < MAINTAIN_N_MAPS:
            await add_more_random_maps(MAINTAIN_N_MAPS - len(fresh_random_maps))
        else:
            await asyncio.sleep(0.1)

async def add_more_random_maps(n: int):
    logging.info(f"Fetching {n} random maps")
    if n <= 0: return
    await asyncio.wait([_add_a_random_map() for _ in range(n)])
    logging.info(f"Fetched {n} random maps")

async def _add_a_random_map():
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/mapsearch2/search?api=on&random=1") as resp:
            if resp.status == 200:
                await _add_maps_from_json(await resp.json())
            else:
                logging.warning(f"Could not get random map: {resp.status} code")

async def _add_maps_from_json(j: dict):
    if 'results' not in j:
        logging.warning(f"Response didn't contain .results")
        return
    map_docs = list()
    added_c = 0
    maps_j = j['results']
    track_ids = list()
    for map_j in maps_j:
        track_id = map_j['TrackID']
        track_ids.append(track_id)
        _map = Map(**map_j)
        if not _map.Downloadable:
            continue
        fresh_random_maps.append(_map)
        maps_to_cache.append(_map)
        added_c += 1
        if track_id in known_maps:
            print(f'known track_id: {track_id}')
            continue
        map_docs.append(_map)
        known_maps.add(track_id)
    # logging.info(f"Added {len(maps_j)} fresh random maps")
    # logging.info(f"Inserting {len(map_docs)} maps to DB")
    if len(map_docs) == 0: return
    await Map.insert_many(map_docs)
    for i, tid in enumerate(track_ids):
        asyncio.create_task(cache_map(tid, delay_ms=i*100))

async def get_some_maps(n: int, min_secs: int = 0, max_secs: int = LONG_MAP_SECS):
    min_secs = max(0, min_secs)
    max_secs = min(LONG_MAP_SECS, max_secs)
    if min_secs >= max_secs: raise Exception(f"min secs >= max secs")
    if min_secs % 15 != 0: raise Exception(f"min_secs % 15 != 0")
    if max_secs % 15 != 0: raise Exception(f"max_secs % 15 != 0")
    sent = 0
    maps_checked = 0
    while sent < n:
        while len(fresh_random_maps) < n:
            await add_more_random_maps(n)
        m = fresh_random_maps.pop()
        maps_checked += 1
        if min_secs <= m.LengthSecs <= max_secs:
            yield m
            sent += 1
        if maps_checked > 100:
            break
    nb_required = n - sent
    if nb_required == 0: return
    extra_ids = await Map.find_many(Map.LengthSecs >= min_secs, Map.LengthSecs <= max_secs, projection_model=MapJustID).to_list()
    track_ids = random.choices(extra_ids, k=nb_required)
    for m in await Map.find_many(In(Map.TrackID, track_ids)).to_list():
        yield m

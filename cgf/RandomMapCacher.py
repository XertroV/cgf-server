import asyncio
import logging
import queue
import random
import time
import botocore

import aiohttp
from beanie.operators import In, Eq

from cgf.consts import LOCAL_DEV_MODE, SERVER_VERSION, SHUTDOWN, SHUTDOWN_EVT
from cgf.utils import chunk
from cgf.http import get_session
from cgf.models.Map import LONG_MAP_SECS, Map, MapJustID, difficulty_to_int
from cgf.db import s3, s3_bucket_name, s3_client

fresh_random_maps: list[Map] = list()
maps_to_cache: list[Map] = list()
known_maps: set[int] = set()
cached_maps: set[int] = set()

MAINTAIN_N_MAPS = 200 if not LOCAL_DEV_MODE else 20  #200

async def init_known_maps():
    _maps = await Map.find_all(projection_model=MapJustID).to_list()
    known_maps.update([m.TrackID for m in _maps])
    logging.info(f"Known maps: {len(known_maps)}")
    asyncio.create_task(ensure_known_maps_have_difficulty_int())
    if not LOCAL_DEV_MODE:
        asyncio.create_task(ensure_known_maps_cached())

async def ensure_known_maps_have_difficulty_int():
    maps = await Map.find(Map.DifficultyInt == None).to_list()
    for m in maps:
        if SHUTDOWN: break
        m.DifficultyInt = difficulty_to_int(m.DifficultyName)
        await m.save()
        # logging.info(f"Set map difficulty: {m.TrackID}: {m.DifficultyName} = {m.DifficultyInt}")
    logging.info(f"Set DifficultyInt on {len(maps)} maps")


async def ensure_known_maps_cached():
    log_s3_progress = True
    while not SHUTDOWN:
        _known_maps = set(known_maps)
        cached_maps.update(await asyncio.get_event_loop().run_in_executor(None, _get_bucket_keys, log_s3_progress))
        log_s3_progress = False  # don't log again on following loops
        uncached = _known_maps - cached_maps
        logging.info(f"Getting {len(uncached)} uncached but known maps")
        for i, track_id in enumerate(uncached):
            await asyncio.sleep(0.300) # sleep 300 ms between checks
            logging.info(f"Getting uncached: {track_id}")
            asyncio.create_task(cache_map(track_id))
        max_map_id = 81192 if len(_known_maps) == 0 else max(_known_maps)
        other_map_ids = list(set(range(0, max_map_id)) - cached_maps)
        # slowly get all the other maps proactively
        logging.info(f"Caching {len(other_map_ids)} uncached maps")
        for tids in chunk(other_map_ids, 3):
            await asyncio.wait(map(cache_map, tids))
        waiting_secs = 60 * 60  # an hour
        for _ in range(waiting_secs * 10):
            await asyncio.sleep(0.1)
            if SHUTDOWN: break
        # at end we want to get any new maps so we cache them
        await add_latest_maps()

def _get_bucket_keys(log_s3_progress = True) -> set[int]:
    cached_maps = set()
    avg_size = 0
    nb_in_avg = 0
    bucket = s3.Bucket(s3_bucket_name)
    for k in bucket.objects.all():
        if SHUTDOWN_EVT.is_set(): break
        try:
            cached_maps.add(int(k.key.split('.Map.Gbx')[0]))
            avg_size = (avg_size * nb_in_avg + k.size) / (nb_in_avg + 1)
            nb_in_avg += 1
        except Exception as e:
            print(f"Got exception getting key: {e}")
        if log_s3_progress and len(cached_maps) % 1000 == 0:
            logging.info(f"Loading cached maps... {len(cached_maps)} / ??? | avg size: {avg_size / 1024:.1f} kb")
        # does not work when running in executor
        # if not asyncio.get_event_loop().is_running():
        #     logging.info(f"Breaking get bucket keys b/c event looped stopped")
        #     break
    logging.info(f"Loaded cached maps: {len(cached_maps)} total | avg size: {avg_size / 1024:.1f} kb")
    return cached_maps

async def cache_map(track_id: int, force = False, delay_ms=0):
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    map_cached = await is_map_cached(track_id)
    if force or not map_cached:
        await _download_and_cache_map(track_id)

async def is_map_cached(track_id: int):
    if track_id in cached_maps: return True
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

async def add_map_to_db_via_id(track_id: int):
    if track_id not in known_maps:
        await _add_a_specific_map(track_id)
        known_maps.add(track_id)


async def _download_and_cache_map(track_id: int, retry_times=10):
    map_file = f"{track_id}.Map.Gbx"
    logging.info(f"Caching map: {map_file}")
    # does not exist
    try:
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
                    cached_maps.add(track_id)
                else:
                    logging.warn(f"Could not get map {track_id}, code: {resp.status}")
    except Exception as e:
        if retry_times <= 0:
            raise e
        logging.warn(f"Error caching map. Waiting 10s and retrying up to {retry_times} more times")
        await asyncio.sleep(10)
        await _download_and_cache_map(track_id, retry_times=retry_times - 1)


async def maintain_random_maps():
    asyncio.create_task(maintain_random_maps_slow())
    while True:
        if len(fresh_random_maps) < MAINTAIN_N_MAPS:
            await add_more_random_maps(10)
        else:
            await asyncio.sleep(0.1)

lastFRM = 0

async def maintain_random_maps_slow():
    global lastFRM
    while True:
        if len(fresh_random_maps) < MAINTAIN_N_MAPS * 10:
            await add_more_random_maps(1)
            currFRM = len(fresh_random_maps)
            if currFRM % 10 == 0:
                logging.info(f"Fresh random maps: {currFRM}")
        else:
            await asyncio.sleep(2)

async def add_more_random_maps(n: int):
    if (n > 100): raise Exception(f"too many maps requested: {n}")
    if (n > 1): logging.info(f"Fetching {n} random maps")
    if n <= 0: return
    await asyncio.wait([_add_a_random_map(delay = i * 0.1) for i in range(n)])
    if (n > 1): logging.info(f"Fetched {n} random maps")

async def _add_a_random_map(delay = 0):
    if delay > 0: await asyncio.sleep(delay)
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/mapsearch2/search?api=on&random=1", timeout=10.0) as resp:
            if resp.status == 200:
                await _add_maps_from_json(await resp.json())
            else:
                logging.warning(f"Could not get random map: {resp.status} code. TMX might be down. Adding some random maps from the DB.")
                maps = await Map.find_all(projection_model=MapJustID).to_list()
                new_maps = []
                for _ in range(20):
                    new_maps.append(random.choice(maps))
                maps = await Map.find_many(In(Map.TrackID, new_maps)).to_list()
                fresh_random_maps.extend(maps)

async def _add_a_specific_map(track_id: int):
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/api/maps/get_map_info/id/{track_id}") as resp:
            if resp.status == 200:
                await _add_maps_from_json(dict(results=[await resp.json()]))
            else:
                logging.warning(f"Could not get specific map (TID:{track_id}): {resp.status} code")

async def add_latest_maps():
    async with get_session() as session:
        async with session.get("https://trackmania.exchange/mapsearch2/search?api=on") as resp:
            if resp.status == 200:
                await _add_maps_from_json(await resp.json(), False)
            else:
                logging.warning(f"Could not get latest maps: {resp.status} code")


async def _add_maps_from_json(j: dict, add_to_random_maps = True):
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
        map_in_db = await Map.find_one(Eq(Map.TrackID, track_id))
        if map_in_db is not None:
            # todo: check for update?
            _map = map_in_db
        else:
            await _map.save()  # using insert_many later doesn't populate .id
        if add_to_random_maps:
            fresh_random_maps.append(_map)
        maps_to_cache.append(_map)
        added_c += 1
        if track_id in known_maps:
            continue
        map_docs.append(_map)
        known_maps.add(track_id)
    for i, tid in enumerate(track_ids):
        asyncio.create_task(cache_map(tid, delay_ms=i*100))

async def get_some_maps(n: int, min_secs: int = 0, max_secs: int = LONG_MAP_SECS, max_difficulty: int = 5):
    min_secs = max(15, min_secs)
    max_secs = min(LONG_MAP_SECS, max(15, max_secs))
    if min_secs > max_secs: raise Exception(f"min secs > max secs")
    if min_secs % 15 != 0: raise Exception(f"min_secs % 15 != 0: {min_secs}")
    if max_secs % 15 != 0: raise Exception(f"max_secs % 15 != 0: {max_secs}")
    if 0 > max_difficulty or max_difficulty > 5: raise Exception(f"invalid max difficulty: {max_difficulty}")
    sent = 0
    maps_checked = 0
    while sent < n:
        while len(fresh_random_maps) < n:
            await add_more_random_maps(25)
        m = fresh_random_maps.pop()
        maps_checked += 1
        length_ok = min_secs <= m.LengthSecs <= max_secs
        difficulty_ok = max_difficulty >= m.DifficultyInt
        if length_ok and difficulty_ok:
            yield m
            sent += 1
        if maps_checked > 100:
            break
    nb_required = n - sent
    if nb_required == 0: return
    extra_ids = await Map.find_many(Map.LengthSecs >= min_secs, Map.LengthSecs <= max_secs, Map.DifficultyInt <= max_difficulty, projection_model=MapJustID).to_list()
    track_ids = random.choices(extra_ids, k=nb_required)
    for m in await Map.find_many(In(Map.TrackID, track_ids)).to_list():
        yield m

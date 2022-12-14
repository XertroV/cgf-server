import asyncio
import logging
import queue
import random
import time
import botocore

import aiohttp
from beanie.operators import In, Eq
from cgf.NadeoApi import await_nadeo_services_initialized, get_totd_maps

from cgf.consts import LOCAL_DEV_MODE, SERVER_VERSION, SHUTDOWN, SHUTDOWN_EVT
from cgf.models.MapPack import MapPack
from cgf.models.RandomMapQueue import RandomMapQueue
from cgf.utils import chunk
from cgf.http import get_session
from cgf.models.Map import LONG_MAP_SECS, Map, MapJustID, difficulty_to_int
from cgf.db import s3, s3_bucket_name, s3_client

fresh_random_maps: list[Map] = list()
maps_to_cache: list[Map] = list()
known_maps: set[int] = set()
cached_maps: set[int] = set()
totd_tids: set[int] = set()

initialized_totds = False

MAINTAIN_N_MAPS = 200 if not LOCAL_DEV_MODE else 20  #200

async def load_random_map_queue():
    return await RandomMapQueue.find_one(RandomMapQueue.name == "main")

async def sync_random_map_queue():
    q = await load_random_map_queue()
    if q is None:
        q = RandomMapQueue(name="main", tracks=list())
    q.tracks = [m.TrackID for m in fresh_random_maps]
    await q.save()

async def init_fresh_maps_from_db():
    global fresh_random_maps
    cached_random_maps = await load_random_map_queue()
    if cached_random_maps is not None:
        fresh_random_maps = await Map.find(In(Map.TrackID, cached_random_maps.tracks)).to_list()
        random.shuffle(fresh_random_maps)
    logging.info(f"fresh random maps loaded from db: {len(fresh_random_maps)}")

class MapPackNotFound(Exception):
    def __init__(self, _id: int, status_code: int | None, message: str | None, *args: object) -> None:
        self.id = _id
        self.status_code = 0 if status_code is None else status_code
        self.message = 'Unknown' if message is None else message
        super().__init__(*args)

def rm_query_args():
    return [Map.Downloadable == True, Map.Unreleased == False, Map.Unlisted == False, Map.MapType == "TM_Race"]

async def init_known_maps():
    _maps = await Map.find_all(projection_model=MapJustID).to_list()
    known_maps.update([m.TrackID for m in _maps])
    logging.info(f"Known maps: {len(known_maps)}")
    asyncio.create_task(ensure_known_maps_have_difficulty_int())
    if not LOCAL_DEV_MODE:
        asyncio.create_task(ensure_known_maps_cached())
    asyncio.create_task(ensure_maps_have_map_type())

async def ensure_known_maps_have_difficulty_int():
    maps = await Map.find(Map.DifficultyInt == None).to_list()
    for m in maps:
        if SHUTDOWN: break
        m.DifficultyInt = difficulty_to_int(m.DifficultyName)
        await m.save()
        # logging.info(f"Set map difficulty: {m.TrackID}: {m.DifficultyName} = {m.DifficultyInt}")
    logging.info(f"Set DifficultyInt on {len(maps)} maps")


async def ensure_maps_have_map_type():
    tids_to_update: list[int] = []
    to_update_count = await Map.find(Map.MapType == None).count()
    logging.info(f"Updating map type on {to_update_count} maps")
    async for m in Map.find(Map.MapType == None):
        tids_to_update.append(m.TrackID)
        if len(tids_to_update) >= 25:
            await update_maps_from_tmx(tids_to_update)
            tids_to_update.clear()
            await asyncio.sleep(1)


async def update_maps_from_tmx(tids_or_uids: list[int | str]):
    tids_str = ','.join(map(str, tids_or_uids))
    async with get_session() as session:
        try:
            async with session.get(f"https://trackmania.exchange/api/maps/get_map_info/multi/{tids_str}", timeout=10.0) as resp:
                if resp.status == 200:
                    await _add_maps_from_json(dict(results=await resp.json()), False)
                else:
                    logging.warning(f"Could not get map infos: {resp.status} code.")
                    print(f"RETRY ME: {tids_str}")
                    return update_maps_from_tmx(tids_or_uids)
        except asyncio.TimeoutError as e:
            logging.warning(f"TMX timeout for get map infos")
            return update_maps_from_tmx(tids_or_uids)


async def ensure_known_maps_cached():
    log_s3_progress = True
    first_run = True
    while not SHUTDOWN:
        _known_maps = set(known_maps)
        if first_run:
            cached_maps.update(await asyncio.get_event_loop().run_in_executor(None, _get_bucket_keys_outer, log_s3_progress))
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
        first_run = False

def _get_bucket_keys_outer(log_s3_progress = True) -> set[int]:
    try:
        return _get_bucket_keys()
    except Exception as e:
        logging.warn(f"Exception getting bucket keys: {e}")
        logging.warn(f"Sleeping and trying to get bucket keys again")
    return _get_bucket_keys_outer(log_s3_progress)


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
            await sync_random_map_queue()
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
                await sync_random_map_queue()
        await asyncio.sleep(2)

async def add_more_random_maps(n: int):
    if (n > 100): raise Exception(f"too many maps requested: {n}")
    if (n > 1): logging.info(f"Fetching {n} random maps")
    if n <= 0: return
    try:
        await asyncio.wait([_add_a_random_map(delay = i * 0.1) for i in range(n)])
        if (n > 1): logging.info(f"Fetched {n} random maps")
    except Exception as e:
        logging.warn(f"Exception getting random maps (passing over): {e}")

RANDOM_MAP_TMX_PARAMS = {
    # exclude tags; https://trackmania.exchange/api/tags/gettags; kacky,royal,arena,flagrush,puzzle
    'etags': '23,37,40,46,47',
}

def mk_url_params(params: dict[str, str], prefix_ampersand=False):
    '''will only prefix ampersand if params is not empty'''
    return ('&' if prefix_ampersand and len(params) > 0 else '') + '&'.join([f"{k}={v}" for k,v in params.items()])

async def _add_a_random_map(delay = 0, extra_params=None):
    params = dict(**RANDOM_MAP_TMX_PARAMS)
    if extra_params is not None:
        params.update(extra_params)
    params_str = mk_url_params(params, True)

    if delay > 0: await asyncio.sleep(delay)
    async with get_session() as session:
        try:
            async with session.get(f"https://trackmania.exchange/mapsearch2/search?api=on&random=1{params_str}", timeout=10.0) as resp:
                if resp.status == 200:
                    await _add_maps_from_json(await resp.json())
                else:
                    logging.warning(f"Could not get random map: {resp.status} code. TMX might be down. Adding some random maps from the DB.")
                    await add_maps_from_db()
        except asyncio.TimeoutError as e:
            logging.warning(f"TMX timeout for random maps")
            await add_maps_from_db()


async def add_maps_from_db():
    maps = await Map.find(*rm_query_args(), projection_model=MapJustID).to_list()
    new_maps = []
    for _ in range(2):
        new_maps.append(random.choice(maps).TrackID)
    maps = await Map.find_many(In(Map.TrackID, new_maps)).to_list()
    fresh_random_maps.extend(maps)
    logging.info(f"Added {len(maps)} maps from DB to fresh_random_maps; {new_maps}")


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


async def _add_maps_from_json(j: dict, add_to_random_maps = True, log_replacement = True):
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
            _map.id = map_in_db.id
            await _map.replace()
            if log_replacement:
                logging.info(f"Replacing map in db: {_map.TrackID}")
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
    if nb_required == 0:
        await sync_random_map_queue()
        return
    extra_ids = await Map.find_many(Map.LengthSecs >= min_secs, Map.LengthSecs <= max_secs, Map.DifficultyInt <= max_difficulty, *rm_query_args(), projection_model=MapJustID).to_list()
    track_ids = random.choices(extra_ids, k=nb_required)
    logging.info(f"Selecting {len(track_ids)} extra maps from DB.")
    for m in await Map.find_many(In(Map.TrackID, track_ids)).to_list():
        yield m
    await sync_random_map_queue()

cached_map_packs: set[int] = set()

async def get_map_pack(id: int, count: int = 0) -> dict | None:
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/api/mappack/get_info/{id}") as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logging.warn(f"Request to get map pack failed with status: {resp.status} and body {resp._body}")
                if count > 10:
                    raise Exception(f'Cannot get map pack {id} -- too many retries')
                await asyncio.sleep(3.0)
                return await get_map_pack(id, count + 1)

async def get_map_pack_tracks(id: int, count: int = 0):
    async with get_session() as session:
        async with session.get(f"https://trackmania.exchange/api/mappack/get_mappack_tracks/{id}") as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logging.warn(f"Request to get map pack failed with status: {resp.status} and body {resp._body}")
                if count > 10:
                    raise Exception(f'Cannot get map pack tracks {id} -- too many retries')
                await asyncio.sleep(3.0)
                return await get_map_pack_tracks(id, count + 1)

async def get_maps_from_map_pack(maps_needed: int, id: int):
    mp = await MapPack.find_one(MapPack.ID == id)
    if id not in cached_map_packs:
        data = await get_map_pack(id)
        _mp = None
        # error if these keys exist
        if data is not None and 'StatusCode' not in data and 'Message' not in data:
            _mp = MapPack(**data)
        elif data is not None:
            raise MapPackNotFound(id, data.get('StatusCode', None), data.get('Message', None))
        else:
            raise MapPackNotFound(id, 500, 'Could not complete the request.')
        if mp is None and _mp is not None:
            await _mp.insert()
        else:
            _mp.id = mp.id
            await _mp.replace()
        mp = _mp
        if mp is not None:
            if mp.Tracks is None:
                mp.Tracks = list()
            tracks = await get_map_pack_tracks(id)
            await _add_maps_from_json(dict(results=tracks), False, False)
            for track in tracks:
                tid = track['TrackID']
                if tid not in mp.Tracks:
                    mp.Tracks.append(tid)
            await mp.save()
            cached_map_packs.add(id)
            asyncio.create_task(uncache_map_pack_in(id, 60 * 60 * 24))
            logging.info(f"Cached map pack {mp.ID} ({mp.Name}) with {len(mp.Tracks)} maps.")
    provided = 0
    if mp is not None:
        maps = await Map.find_many(In(Map.TrackID, mp.Tracks)).to_list()
        while provided < maps_needed:
            random.shuffle(maps)
            for m in maps:
                provided += 1
                yield m
                if provided >= maps_needed:
                    break
    else:
        async for m in get_some_maps(maps_needed, 0, 60, 2):
            yield m


# cache for 24 hrs
async def uncache_map_pack_in(id, delay = 60 * 60 * 24):
    await asyncio.sleep(delay)
    if id in cached_map_packs:
        cached_map_packs.remove(id)


async def maintain_totd_maps():
    existing_totds = await Map.find(Map.WasTOTD == True).to_list()
    totd_tids.update(m.TrackID for m in existing_totds)
    while not SHUTDOWN:
        try:
            resp = await get_totd_maps()
            if resp is None:
                await asyncio.sleep(5)
                continue
            await update_totds(resp)
            wait_time = resp['relativeNextRequest']
            await asyncio.sleep(wait_time)
        except Exception as e:
            logging.warn(f"Exception maintaining totd maps: {e}")
            await asyncio.sleep(5)

totds_not_on_tmx: set[str] = set()

async def update_totds(resp: dict):
    global initialized_totds
    map_uids = set()
    for month in resp.get('monthList', []):
        for day in month.get('days', []):
            map_uid = day.get('mapUid', None)
            if map_uid is not None and len(map_uid) > 15 and map_uid not in totds_not_on_tmx:
                map_uids.add(map_uid)
    map_uids_list = list(map_uids)

    logging.info(f"Updating TOTDs: {len(map_uids)} total")
    existing_maps = await Map.find(In(Map.TrackUID, map_uids_list)).to_list()
    existing_uids = set(m.TrackUID for m in existing_maps)
    to_get_uids = list(map_uids - existing_uids)
    totd_tids.update(m.TrackID for m in existing_maps)


    logging.info(f"Getting TOTDs from TMX: {len(to_get_uids)}")
    for i, uids in enumerate(chunk(to_get_uids, 5)):
        await update_maps_from_tmx(uids)
        logging.info(f"Updated TOTDs: [{i+1} / {len(to_get_uids) // 5 + 1}] {uids}")
    all_totd_maps = await Map.find(In(Map.TrackUID, map_uids_list)).to_list()
    totd_tids.update(m.TrackID for m in all_totd_maps)
    totds_not_on_tmx.update(map_uids - set(m.TrackUID for m in all_totd_maps))

    logging.info(f"All TOTDs updated: {len(totd_tids)} total")

    for m in all_totd_maps:
        if not m.WasTOTD:
            m.WasTOTD = True
            await m.save()

    logging.info(f"Ensured TOTD flag set")
    initialized_totds = True


async def await_initialized_totds():
    while not initialized_totds:
        await asyncio.sleep(0.05)


async def get_maps_from_totd_maps(maps_needed: int):
    await await_initialized_totds()
    sent = 0
    while sent < maps_needed:
        tids = random.sample(totd_tids, maps_needed)
        async for m in Map.find(In(Map.TrackID, tids)):
            yield m
            sent += 1

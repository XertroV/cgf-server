import asyncio
from dataclasses import dataclass
import logging
import time
from aiohttp import BasicAuth

import jwt
from cgf.consts import LOCAL_DEV_MODE
from cgf.users import gen_uid

from cgf.utils import read_config_file
from cgf.http import get_session


ubi_account_info = read_config_file('.ubisoft-acct', ['email', 'password'])


UBI_SESSIONS_URL = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
NADEO_AUDIENCE_REG_URL = "https://prod.trackmania.core.nadeo.online/v2/authentication/token/ubiservices"
NADEO_REFRESH_URL = "https://prod.trackmania.core.nadeo.online/v2/authentication/token/refresh"


TTG_CLUB_ID = 55829


@dataclass
class UbiAuthResp:
    platformType: str
    ticket: str
    twoFactorAuthenticationTicket: str
    profileId: str
    userId: str
    nameOnPlatform: str
    environment: str
    expiration: str
    spaceId: str
    clientIp: str
    clientIpCountry: str
    serverTime: str
    sessionId: str
    sessionKey: str
    rememberMeTicket: str


@dataclass
class NadeoToken:
    accessToken: str
    refreshToken: str

    @property
    def accessTokenJson(self):
        return jwt.decode(self.accessToken, options={"verify_signature": False})

    @property
    def refreshTokenJson(self):
        return jwt.decode(self.refreshToken, options={"verify_signature": False})


async def start_session():
    async with get_session() as session:
        async with await session.post(
            UBI_SESSIONS_URL,
            headers={'Content-Type': 'application/json', 'Ubi-AppId': '86263886-327a-4328-ac69-527f0d20a237'},
            auth=BasicAuth(ubi_account_info['email'], ubi_account_info['password']),
            json={}
        ) as resp:
            if not resp.ok:
                logging.warn(f"Error starting session to ubi; {resp.status}, {await resp.content.read()}")
                return
            data = await resp.json()
            logging.info(f"Got ubi session")
            return UbiAuthResp(**data)


async def get_token_for_audience(ubi: UbiAuthResp, audience: str):
    body = {'audience': audience}
    async with get_session() as session:
        async with await session.post(
            NADEO_AUDIENCE_REG_URL,
            headers={'Content-Type': 'application/json', 'Authorization': f'ubi_v1 t={ubi.ticket}'},
            json=body
        ) as resp:
            if not resp.ok:
                logging.warn(f"Error getting token for audience {audience}; {resp.status}, {await resp.content.read()}")
                return
            return NadeoToken(**(await resp.json()))


NadeoCoreToken: NadeoToken | None = None
NadeoLiveToken: NadeoToken | None = None


async def await_nadeo_services_initialized():
    while NadeoCoreToken is None or NadeoLiveToken is None:
        await asyncio.sleep(.05)


def all_tokens() -> list[NadeoToken | None]:
    return [NadeoCoreToken, NadeoLiveToken]


async def reacquire_all_tokens():
    global NadeoCoreToken, NadeoLiveToken
    logging.info(f"Starting get nadeo auth tokens...")
    ubi = await start_session()
    logging.info(f"Ubi session started: {ubi is not None}")

    NadeoCoreToken = await get_token_for_audience(ubi, 'NadeoServices')
    logging.warn(f"Got core token: {NadeoCoreToken is not None}")
    if LOCAL_DEV_MODE:
        logging.warn(f"Got core token: {NadeoCoreToken.accessToken}")

    NadeoLiveToken = await get_token_for_audience(ubi, 'NadeoLiveServices')
    logging.warn(f"Got live token: {NadeoLiveToken is not None}")
    if LOCAL_DEV_MODE:
        logging.warn(f"Got live token: {NadeoLiveToken.accessToken}")


async def run_nadeo_services_auth():
    await reacquire_all_tokens()
    while True:
        await asyncio.sleep(60)
        ts = all_tokens()
        for t in ts:
            if t is None: continue
            refreshAfter = t.accessTokenJson.get('rat')
            if time.time() > refreshAfter + 10:
                await reacquire_all_tokens()
                break


def get_token_for(audience):
    if audience == "NadeoServices" and NadeoCoreToken is not None:
        return NadeoCoreToken.accessToken
    if audience == "NadeoLiveServices" and NadeoLiveToken is not None:
        return NadeoLiveToken.accessToken
    raise Exception(f'cannot get token for audience: {audience}')

def get_nadeo_session(audience: str):
    session = get_session()
    session.headers['Authorization'] = f"nadeo_v1 t={get_token_for(audience)}"
    return session

def get_core_session():
    return get_nadeo_session('NadeoServices')

def get_live_session():
    return get_nadeo_session('NadeoLiveServices')


MAP_INFO_BY_UID_URL = "https://prod.trackmania.core.nadeo.online/maps/?mapUidList="

''' wait for up to 2 minutes for maps to be uploaded '''
async def await_maps_uploaded(mapUids: list[str]):
    await await_nadeo_services_initialized()
    mapsNotUploaded = set(mapUids)
    counter = 0
    logging.info(f"Awaiting map uploads: {len(mapsNotUploaded)} : {mapsNotUploaded}")
    while len(mapsNotUploaded) > 0 and counter <= 60:
        if counter > 0:
            await asyncio.sleep(2)
        counter += 1
        url = MAP_INFO_BY_UID_URL + ",".join(mapsNotUploaded)
        async with get_core_session() as session:
            async with await session.get(url) as resp:
                if not resp.ok:
                    logging.warn(f"Error getting maps for uids: {mapUids}; {resp.status}, {await resp.content.read()}")
                    return
                mapInfos = await resp.json()
                for mapInfo in mapInfos:
                    uid = mapInfo['mapUid']
                    if uid in mapsNotUploaded:
                        mapsNotUploaded.remove(uid)
                logging.info(f"Maps not yet uploaded; {mapsNotUploaded}")
    if len(mapsNotUploaded) > 0:
        logging.warn(f"Some maps are not yet uploaded! {mapsNotUploaded}")
    else:
        logging.info(f"All maps uploaded: {set(mapUids)}")


CREATE_ROOM_URL = f"https://live-services.trackmania.nadeo.live/api/token/club/{TTG_CLUB_ID}/room/create"
DELETE_ROOM_URL = lambda activityId: f"https://live-services.trackmania.nadeo.live/api/token/club/{TTG_CLUB_ID}/activity/{activityId}/delete"
GET_ROOM_URL = lambda activityId: f"https://live-services.trackmania.nadeo.live/api/token/club/{TTG_CLUB_ID}/room/{activityId}/"
GET_PASSWORD_URL = lambda activityId: f"https://live-services.trackmania.nadeo.live/api/token/club/{TTG_CLUB_ID}/room/{activityId}/get-password"
POST_JOIN_URL = lambda activityId: f"https://live-services.trackmania.nadeo.live/api/token/club/{TTG_CLUB_ID}/room/{activityId}/join"

''' example settings:

[{"key":"S_TimeLimit","value":"3600","type":"integer"},{"key":"S_WarmUpNb","value":"1","type":"integer"},{"key":"S_WarmUpDuration","value":"10","type":"integer"},{"key":"S_WarmUpTimeout","value":"10","type":"integer"}]

'''

async def create_club_room(name: str, mapUids=list[str], region: str = "eu-west", scalable=0, password=0, maxPlayers=64, script="TrackMania/TM_TimeAttack_Online.Script.txt", settings=None):
    await await_nadeo_services_initialized()
    valid_regions = ["eu-west", "ca-central"]
    if region not in valid_regions:
        region = valid_regions[0]
    assert scalable in [0, 1]
    assert password in [0, 1]
    data = {
        "name":name,
        "region":region,
        "maxPlayersPerServer":maxPlayers,
        "script":script,
        "settings":[] if settings is None else settings,
        "maps":mapUids,
        "scalable":scalable,
        "password":password
    }
    async with get_live_session() as session:
        async with session.post(CREATE_ROOM_URL, json=data) as resp:
            if not resp.ok:
                logging.warn(f"Error creating club room; {resp.status}, {await resp.content.read()}")
                return
            data = await resp.json()
            logging.info(f"Create room response: {data}")
            if password == 0:
                return data
        logging.info(f"Getting password for club room: {data['activityId']}")
        async with session.get(GET_PASSWORD_URL(data['activityId'])) as resp:
            if not resp.ok:
                logging.warn(f"Error getting pw for club room; {resp.status}, {await resp.content.read()}; {data}")
                return data
            pwData = await resp.json()
            data['password'] = pwData['password']
            return data

async def get_club_room(activityId: int):
    await await_nadeo_services_initialized()
    async with get_live_session() as session:
        async with session.get(GET_ROOM_URL(activityId)) as resp:
            if not resp.ok:
                logging.warn(f"Error getting club room {activityId}; {resp.status}, {await resp.content.read()}")
            else:
                data = await resp.json()
                async with session.get(GET_PASSWORD_URL(data['activityId'])) as resp:
                    if not resp.ok:
                        logging.warn(f"Error getting pw for club room; {resp.status}, {await resp.content.read()}; {data}")
                        return data
                    pwData = await resp.json()
                    data['password'] = pwData['password']
                    return data

async def delete_club_room(activityId: int):
    await await_nadeo_services_initialized()
    async with get_live_session() as session:
        async with session.post(DELETE_ROOM_URL(activityId)) as resp:
            if not resp.ok:
                logging.warn(f"Error deleting club room {activityId}; {resp.status}, {await resp.content.read()}")
            else:
                logging.info(f"Deleted activity: {activityId}")


async def join_club_room(activityId: int):
    await await_nadeo_services_initialized()
    async with get_live_session() as session:
        async with session.post(POST_JOIN_URL(activityId)) as resp:
            if not resp.ok:
                logging.warn(f"Error getting join info for {activityId}; {resp.status}, {await resp.content.read()}")
                if resp.status == 504: # timeout
                    await asyncio.sleep(1.0)
                    return await join_club_room(activityId)
            else:
                data: dict = await resp.json()
                # logging.debug(f"Join link data: {data}")
                return data

async def await_join_club_room(activityId: int):
        count = 0
        while count < 60:
            if count > 0:
                await asyncio.sleep(.75)
            count += 1
            join_resp: dict = await join_club_room(activityId)
            if not join_resp.get('starting', True):
                return join_resp['joinLink']
        logging.warn(f"Server was not started! checked 60 times sleeping .75s between.")


# works!
async def run_club_room_creation_test():
    if not LOCAL_DEV_MODE: return
    await await_nadeo_services_initialized()
    name = f"TTG-{gen_uid(6)}"
    logging.info(f"Creating room named {name}")
    map_list = ["RCvWrXBMxPl2RkLbbabPW_m_Cp2","RCeN91YppQKsVeaxljyNZrv08Ia","AIUiDusbordueNQMAfniaEeRpl2"]
    await await_maps_uploaded(map_list)
    logging.info(f"Ensured maps are uploaded")
    room_resp = await create_club_room(name, map_list, password=1)
    logging.info(f"Got room resp: {room_resp}")
    join_link = await await_join_club_room(room_resp['activityId'])
    join_link += f":{room_resp['password']}"
    logging.info(f"Got join link for room: {join_link}")
    exists_start = time.time()
    # while join_link is not None:
    logging.info(f"waiting 315 seconds, should still be active")
    await asyncio.sleep(315.)
    join_info = await join_club_room(room_resp['activityId'])
    logging.info(f"waiting 30 seconds, should not active")
    await asyncio.sleep(30.)
    join_info = await join_club_room(room_resp['activityId'])
        # if join_info.get('starting', False):
        #     break
    logging.info(f"Server active for {time.time() - exists_start} seconds")
    logging.info(f"deleting room")
    await delete_club_room(room_resp['activityId'])
    logging.info(f"Deleted room")






# @dataclass
# class ClubRoom:
#     id: int
#     clubId: int
#     clubName: str
#     nadeo: bool
#     roomId: int
#     activityId: int
#     campaignId: None | int
#     playerServerLogin: None | str
#     serverInfo: None | dict
#     mediaUrl: str
#     name: str




#     {"id":338354,"clubId":55829,"clubName":"$z\uf292$s$iTic Tac GO!","nadeo":true,"roomId":118859,"campaignId":null,"playerServerLogin":null,"activityId":338354,"mediaUrl":"","name":"test 3","room":{"id":118859,"name":"test 3","region":"eu-west","serverAccountId":"","maxPlayers":16,"playerCount":0,"maps":["FqOC38szG6MJIr_zLIGPGfbmAkl","RCeN91YppQKsVeaxljyNZrv08Ia"],"script":"TrackMania\/TM_TimeAttack_Online.Script.txt","scalable":false,"scriptSettings":{"S_DecoImageUrl_WhoAmIUrl":{"key":"S_DecoImageUrl_WhoAmIUrl","value":"\/api\/club\/55829","type":"text"},"S_ForceLapsNb":{"key":"S_ForceLapsNb","value":"-1","type":"integer"}},"serverInfo":null},"popularityLevel":0,"creationTimestamp":1671518327,"password":false}

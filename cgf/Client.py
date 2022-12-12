import asyncio
import json
from logging import debug, warn, warning, info
import os
import random
import struct
import traceback
from typing import Any, Literal, Optional, Union

import pymongo
from pydantic.dataclasses import dataclass
from pydantic import Field

from beanie import Document, PydanticObjectId, Link, Indexed
from beanie.operators import GTE, Eq, In
from beanie.exceptions import StateNotSaved
import beanie

import cgf.RandomMapCacher as RMC
from cgf.models.Map import Map, difficulty_to_int, int_to_difficulty
from cgf.User import User
from cgf.users import gen_join_code, gen_secret, gen_uid, gen_user_uid, get_user, register_authed_user, register_user, authenticate_user, uid_from_wsid
from cgf.consts import *
from cgf.utils import *
from cgf.op_auth import check_token

from .User import User
from .consts import SERVER_VERSION


class MsgException(Exception):
    pass


all_clients: set["Client"] = set()
# updated in Lobby constructor
all_lobbies: dict[str, "Lobby"] = dict()

class Message(Document):
    type: str
    payload: dict
    visibility: str = "global"
    user: Optional[Link[User]]
    ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=time.time)

    class Settings:
        indexes = ["type", "visibility"]

    def __getitem__(self, key):
        return self.payload[key]
    def __setitem__(self, key, value):
        self.payload[key] = value

    def save_via_task(self):
        asyncio.create_task(self.save())

    @property
    def safe_json(self):
        return { "type": self.type, "payload": self.payload, "visibility": self.visibility, "from": None if self.user is None else self.user.safe_json, "ts": self.ts }


class HasAdminsModel(Document):
    admins: list[Link[User]] = Field(default_factory=list)
    mods: list[Link[User]] = Field(default_factory=list)
    kicked_players: list[Link[User]] = Field(default_factory=list)


class LobbyModel(HasAdminsModel):
    uid: Indexed(str, unique=True) = Field(default_factory=gen_uid)
    name: Indexed(str, unique=True)
    parent_lobby: Optional[str] = None
    is_public: bool = True
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=lambda: time.time())

    class Settings:
        use_state_management = True
        name = "Lobby"



# class GameLobby(Lobby):
#     pass


class Room(HasAdminsModel):
    # room properties and common things
    name: Indexed(str, unique=True)
    lobby: Indexed(str)
    is_public: bool
    is_open: bool = True
    is_retired: bool = False
    join_code: str = Field(default_factory=gen_join_code)
    game_start_time: float = -1
    # state / config stuff
    player_limit: int
    players: list[Link[User]] = Field(default_factory=list)
    n_teams: int
    maps_required: int = Field(default=1)
    min_secs: int = Field(default=15)
    max_secs: int = Field(default=45)
    max_difficulty: int = Field(default=3)
    map_list: list[int]
    # note: this type signature causes beanie to cast values to strings
    game_opts: dict[str, str] = Field(default_factory=dict)
    # ts
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=time.time)

    class Settings:
        use_state_management = True
        indexes = [
            [
                ("creation_ts", pymongo.DESCENDING),
                ("lobby", pymongo.ASCENDING),
            ],
            [
                ("join_code", pymongo.ASCENDING),
            ],
            [
                ("is_retired", pymongo.ASCENDING),
                ("creation_ts", pymongo.DESCENDING),
                ("lobby", pymongo.ASCENDING),
            ],
            # IndexModel(
            #     [("test_str", pymongo.DESCENDING)],
            #     name="test_string_index_DESCENDING",
            # ),
        ]


    @property
    def to_created_room_json(self):
        return dict(
            **self.to_room_info_json,
            join_code=self.join_code
        )
    @property

    def to_room_info_json(self):
        return dict(
            name=self.name,
            player_limit=self.player_limit,
            n_teams=self.n_teams,
            n_players=len(self.players),  # might be updated by room controller
            is_public=self.is_public,
            is_open=self.is_open,
            n_maps=self.maps_required,
            min_secs=self.min_secs,
            max_secs=self.max_secs,
            max_difficulty=int_to_difficulty(self.max_difficulty),
            game_start_time=self.game_start_time,
            started=0 < self.game_start_time < time.time(),
            game_opts=self.game_opts,
        )


class GameSession(HasAdminsModel):
    name: Indexed(str, unique=True) = Field(default_factory=lambda: gen_uid(10))
    players: list[Link[User]]
    game_msgs: list[Link[Message]] = Field(default_factory=list)
    room: str
    lobby: str
    # list of player UIDs
    teams: list[list[str]]
    team_order: list[int] = Field(default_factory=list)
    map_list: list[int]
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=lambda: time.time())
    class Settings:
        use_state_management = True
        name = "Game"

        indexes = [
            pymongo.IndexModel(
                [
                    ("room", pymongo.ASCENDING),
                    ("lobby", pymongo.ASCENDING),
                ],
                name="room_lobby_uniq",
                unique=True,
            ),
        ]

    @property
    def to_full_game_info_json(self):
        if self.team_order is None or len(self.team_order) == 0:
            self.team_order = list(range(len(self.teams)))
            random.shuffle(self.team_order)
            self.persist_model()
        ret = dict(
            players=[p.safe_json for p in self.players],
            n_game_msgs=len(self.game_msgs),
            teams=self.teams,
            team_order=self.team_order,
            map_list=self.map_list,
            room=self.room,
            lobby=self.lobby,
        )
        print(ret)
        return ret

    @property
    def to_inprog_game_info_json(self):
        return dict(
            n_game_msgs=len(self.game_msgs),
        )




class HasClients:
    clients: set["Client"]

    def __init__(self):
        self.clients = set()

    def find_client_from_uid(self, uid: str):
        for client in self.clients:
            if client.user.uid == uid:
                return client

    def broadcast_msg(self, msg: Message):
        self.broadcast_json(msg.safe_json)

    def broadcast_json(self, msg_j: dict):
        for client in self.clients:
            client.write_json(msg_j)

    def broadcast_player_left(self, the_client: "Client"):
        msg_j = dict(type="PLAYER_LEFT", payload=the_client.user.safe_json)
        for client in self.clients:
            client.write_json(msg_j)

    def broadcast_player_joined(self, the_client: "Client"):
        msg_j = dict(type="PLAYER_JOINED", payload=the_client.user.safe_json)
        for client in self.clients:
            client.write_json(msg_j)

    def tell_player_list(self, the_client: "Client"):
        msg_j = dict(type="PLAYER_LIST", payload={'players': [c.user.safe_json for c in self.clients]})
        the_client.write_json(msg_j)


class HasAdmins(HasClients):
    model: HasAdminsModel
    to_kick: set[User]

    def __init__(self):
        super().__init__()
        self.to_kick = set()
        # asyncio.create_task(self.load_admins_mods())

    async def load_admins_mods(self):
        await self.model.fetch_link("admins")
        await self.model.fetch_link("mods")
        self.broadcast_new_admins_mods()

    @property
    def admins(self) -> list[Link[User]]:
        return self.model.admins

    @property
    def mods(self) -> list[Link[User]]:
        return self.model.mods

    @property
    def admins_mods_payload(self) -> dict:
        try:
            pl = dict(
                admins=[u.uid for u in self.admins],
                mods=[u.uid for u in self.mods])
            return pl
        except Exception as e:
            warning(f"Exception during admins_mods_payload: {e}\n{''.join(traceback.format_exception(e))}")
            return dict(admins=list(), mods=list())

    def broadcast_new_admins_mods(self):
        msg = Message(type="ADMIN_MOD_STATUS", payload=self.admins_mods_payload).safe_json
        self.broadcast_json(msg)

    def is_admin(self, user: User):
        return user in self.admins or any(map(lambda a: a.ref.id == user.id, self.admins))

    def is_mod(self, user: User):
        return user in self.mods or any(map(lambda a: a.ref.id == user.id, self.mods))

    def remove_admin(self, user: User):
        self.admins.remove(user)

    def remove_mod(self, user: User):
        self.mods.remove(user)

    def persist_model(self):
        asyncio.create_task(self.persist_model_inner())

    async def persist_model_inner(self):
        try:
            await self.model.save_changes()
        except StateNotSaved as e:
            pass

    def kick_this_client(self, client: "Client"):
        for c in self.clients:
            c.tell_info(f"Player Kicked: {client.user.name}")

    def send_admin_mod_status(self, client: "Client"):
        client.write_message("ADMIN_MOD_STATUS", self.admins_mods_payload)

    # call this from main process_msg function
    async def process_admin_msg(self, client: "Client", msg: Message):
        if client.user in self.to_kick:
            self.kick_this_client(client)
            return "LEAVE"
        if msg.type == "ADD_ADMIN": await self.on_add_admin(client, msg)
        elif msg.type == "RM_ADMIN": await self.on_rm_admin(client, msg)
        elif msg.type == "ADD_MOD": await self.on_add_mod(client, msg)
        elif msg.type == "RM_MOD": await self.on_rm_mod(client, msg)
        elif msg.type == "KICK_PLAYER": await self.on_kick_player(client, msg)

    def admin_only(f):
        def inner(self: "HasAdmins", client: "Client", msg: Message):
            if not self.is_admin(client.user):
                client.tell_warning("Permission denied (Admin only)")
            else:
                return f(client, msg)
        return inner

    def mod_only(f):
        def inner(self: "HasAdmins", client: "Client", msg: Message):
            if not self.is_admin(client.user) and not self.is_mod(client.user):
                client.tell_warning("Permission denied (Mod only)")
            else:
                return f(client, msg)
        return inner

    @admin_only
    async def on_add_admin(self, client: "Client", msg: Message):
        user_uid = msg.payload['uid']
        client = self.find_client_from_uid(user_uid)
        if self.is_admin(client.user): return client.tell_info(f"User {client.user.name} is already an admin.")
        self.model.admins.append(client.user)
        self.persist_model()

    @admin_only
    async def on_rm_admin(self, client: "Client", msg: Message):
        user_uid = msg.payload['uid']
        to_rm = self.find_client_from_uid(user_uid)
        if not self.is_admin(to_rm.user): return client.tell_info(f"User {to_rm.user.name} is not an admin.")
        else:
            self.remove_admin(to_rm.user)
            client.tell_info(f"User {to_rm.user.name} was removed as an admin.")
        self.persist_model()

    @admin_only
    async def on_add_mod(self, client: "Client", msg: Message):
        user_uid = msg.payload['uid']
        client = self.find_client_from_uid(user_uid)
        if self.is_mod(client.user): return client.tell_info(f"User {client.user.name} is already a mod.")
        self.model.mods.append(client.user)
        self.persist_model()

    @admin_only
    async def on_rm_mod(self, client: "Client", msg: Message):
        user_uid = msg.payload['uid']
        to_rm = self.find_client_from_uid(user_uid)
        if not self.is_mod(to_rm.user): return client.tell_info(f"User {to_rm.user.name} is not a mod.")
        else:
            self.remove_mod(to_rm.user)
            client.tell_info(f"User {to_rm.user.name} was removed as a mod.")
        self.persist_model()

    @mod_only
    async def on_kick_player(self, client: "Client", msg: Message):
        to_kick_uid = msg.payload['uid']
        c = self.find_client_from_uid(to_kick_uid)
        if c is None: return
        self.to_kick.add(c.user)
        client.tell_info(f"Kicking: {c.user.name}...")
        self.model.kicked_players.append(c.user)
        self.persist_model()


class HasChats(HasAdmins):
    chats: "ChatMessages" = None
    recent_chat_msgs: list[Message]
    container_type: Literal["lobby", "room", "game"]
    loaded_chat: bool

    def __init__(self):
        super().__init__()
        self.chats = None
        self.recent_chat_msgs = list()
        self.loaded_chat = False
        asyncio.create_task(self.load_chat_msgs())

    @property
    def name(self):
        raise Exception("override .name property")

    async def load_chat_msgs(self):
        with timeit_context("Load Chats"):
            if self.chats is not None: return
            self.chats = await ChatMessages.find_one({self.container_type: self.name}, with_children=True)
            if self.chats is None:
                self.chats = ChatMessages(**{self.container_type: self.name, 'msgs': list()})
                asyncio.create_task(self.chats.save())
            n_msgs = len(self.chats.msgs)
            start_ix = max(0, n_msgs - 20)
            msg_ids = [self.chats.msgs[i].ref.id for i in range(start_ix, n_msgs)]
            self.recent_chat_msgs = await Message.find(In(Message.id, msg_ids), fetch_links=True).to_list()
            log.info(f"[{self.container_type} | {self.name}] Loaded recent chats; nb: {len(self.recent_chat_msgs)}")
            self.loaded_chat = True

    async def process_chat_msg(self, client: "Client", msg: Message):
        if msg.type == "SEND_CHAT": await self.on_chat_msg(client, msg)

    chat_msg_keys = {"content"}

    async def on_chat_msg(self, client: "Client", msg: Message):
        if (msg.user is None): return
        if set(msg.payload.keys()) != self.chat_msg_keys:
            return client.tell_error(f"Chat message expects keys: {self.chat_msg_keys}")
        if not isinstance(msg['content'], str) or len(msg['content']) > 1024:
            return client.tell_error(f"Content wrong type or >1024 in length")
        while len(self.recent_chat_msgs) > 19:
            self.recent_chat_msgs.pop(0)
        self.recent_chat_msgs.append(msg)
        self.chats.append(msg)
        self.broadcast_msg(msg)

    def send_recent_chat(self, client: "Client"):
        for msg in self.recent_chat_msgs:
            client.write_json(msg.safe_json)
        log.info(f"Sent recent chats ({len(self.recent_chat_msgs)}) to {client.client_ip}")




class RoomController(HasChats):
    model: Room
    game: Union["GameController", None] = None
    loaded_game = False
    loaded_maps = False
    lobby_inst: "Lobby"
    container_type: Literal["lobby", "room", "game"] = "room"
    players_ready: dict[str, bool]
    ready_count: int = 0
    uid_to_teams: dict[str, int]
    teams: list[list["Client"]]
    maps: dict[int, Map]

    def __init__(self, model=None, lobby_inst=None, **kwargs):
        super().__init__()
        if 'map_list' not in kwargs: kwargs['map_list'] = list()
        self.model = model if model is not None else Room(**kwargs)
        self.lobby_inst: "Lobby" = lobby_inst
        self.players_ready = dict()
        self.uid_to_teams = dict()
        self.teams = [list() for _ in range(self.model.n_teams)]
        self.maps = dict()
        asyncio.create_task(self.load_game())
        asyncio.create_task(self.load_maps())
        asyncio.create_task(self.when_empty_retire_room())

    async def initialized(self):
        while not self.loaded_chat or not self.loaded_game or not self.loaded_maps:
            await asyncio.sleep(0.01)

    async def load_game(self):
        game_model = await GameSession.find_one(
            GTE(GameSession.creation_ts, time.time() - (3600 * 6)), # within last 6 hrs
            Eq(GameSession.room, self.name),
            Eq(GameSession.lobby, self.lobby_inst.name)
        )
        if game_model is not None:
            await game_model.fetch_all_links()
            for msg in game_model.game_msgs:
                asyncio.create_task(msg.fetch_all_links())
            game = GameController(game_model, room_inst=self)
            self.game = game
        self.loaded_game = True

    async def load_maps(self):
        # these maps (in the room) come back in a random order. Not the case for game session tho. IDK, weird
        # print([m.TrackID for m in self.model.map_list])
        # print(self.model.maps_required - len(self.model.map_list))
        maps_needed = self.model.maps_required - len(self.model.map_list)
        existing_maps = await Map.find_many(In(Map.TrackID, self.model.map_list)).to_list()
        for m in existing_maps:
            self.maps[m.TrackID] = m
        if maps_needed > 0:
            # log.debug(f"Room asking for {maps_needed} maps.")
            async for m in RMC.get_some_maps(maps_needed, self.model.min_secs, self.model.max_secs, self.model.max_difficulty):
                # log.debug(f"Got map: {m.json()}")
                if (m.id is None):
                    await m.save()
                self.model.map_list.append(m.TrackID)
                self.maps[m.TrackID] = m
            self.persist_model()
        self.loaded_maps = True
        for client in self.clients:
            self.tell_maps_loaded_if_loaded(client)

    @property
    def is_empty(self):
        has_clients = len(self.clients) > 0
        game_has_clients = self.game is not None and len(self.game.clients) > 0
        return not has_clients and not game_has_clients

    async def when_empty_retire_room(self):
        ''' Wait till the room is empty for 120s, and retire it.
        Initial delay of 6s.
        '''
        await asyncio.sleep(6)
        while self.name in self.lobby_inst.rooms:
            await asyncio.sleep(1)
            if not self.is_empty: continue
            slept = 0
            while self.is_empty:
                slept += 0.1
                await asyncio.sleep(0.1)
                if not self.is_empty or slept >= 120.0: break
            if self.is_empty: break
        # retire
        self.lobby_inst.retire_room(self)

    @property
    def name(self): return self.model.name

    @property
    def map_list(self): return self.model.map_list

    @property
    def to_created_room_json(self):
        ret = self.model.to_created_room_json
        ret['n_players'] = len(self.clients)
        ret['ready_count'] = self.ready_count
        ret['maps_loaded'] = self.loaded_maps
        return ret

    @property
    def to_room_info_json(self):
        ret = self.model.to_room_info_json
        ret['n_players'] = len(self.clients)
        ret['ready_count'] = self.ready_count
        return ret

    @property
    def is_public(self):
        return self.model.is_public

    @property
    def is_open(self):
        return self.model.is_open

    async def handoff(self, client: "Client", game_name: str = None):
        # todo: does kicked players work?
        if client.user in self.model.kicked_players:
            client.tell_error(f"You can't join again because you were already kicked.")
            return
        if client in self.clients:
            warn(f"I already have client: {client}")
            client.tell_warning("Tried to join room twice. This is probably a bug.")
            return
        if len(self.clients) >= self.model.player_limit:
            client.tell_info(f"Sorry, the room is full.")
            return
        if self.has_game_started() and not self.game.includes_player(client.user.uid):
            client.tell_info(f"Sorry, the game has already started with other players.")
            return
        # todo if game has started
        self.on_client_entered(client)
        try:
            if game_name is not None:
                await self.on_join_game_now(client, None)
            await self.run_client(client)
        except Exception as e:
            warning(f"[{client.client_ip}] Disconnecting: Exception during Room.run_client: {e}\n{''.join(traceback.format_exception(e))}")
            client.tell_error("Unknown server error.")
            # client.disconnect()
        self.on_client_handed_off(client)

    def tell_client_curr_scope(self, client: "Client"):
        client.set_scope(f"2|{self.name}")

    async def room_info_loop(self, client: "Client"):
        while client in self.clients and not client.disconnected:
            self.send_room_info(client)
            self.tell_player_list(client)
            self.on_list_teams(client)
            await asyncio.sleep(5.0)

    def send_room_info(self, client: "Client"):
        client.write_message("ROOM_INFO", self.to_created_room_json)

    def tell_maps_loaded_if_loaded(self, client: "Client"):
        if self.loaded_maps:
            client.write_message("MAPS_LOADED", dict())

    def on_client_handed_off(self, client: "Client"):
        self.players_ready.pop(client.user.uid, False)
        self.remove_player_from_teams(client)
        self.broadcast_player_left(client)
        if client in self.clients:
            self.clients.remove(client)
        self.lobby_inst.update_room_status(self)

    def on_client_entered(self, client: "Client"):
        self.tell_client_curr_scope(client)
        client.tell_info(f"Entered Room: {self.name}")
        asyncio.create_task(self.room_info_loop(client))
        self.send_room_info(client)
        self.send_recent_chat(client)
        self.send_admin_mod_status(client)
        self.on_list_teams(client)
        self.tell_player_list(client)
        self.tell_maps_loaded_if_loaded(client)
        self.clients.add(client)
        self.broadcast_player_joined(client)
        self.lobby_inst.update_room_status(self)

    def on_client_left(self, client: "Client"):
        self.on_client_handed_off(client)
        client.disconnect()

    # main room loop for client
    # - chatting
    # - join teams
    # - mark ready
    async def run_client(self, client: "Client"):
        while True:
            msg = await client.read_valid()
            if msg is None:
                self.on_client_left(client)
                return
            # process each message and break on leave
            if await self.process_msg(client, msg) == "LEAVE":
                break

    async def process_msg(self, client: "Client", msg: Message):
        if await self.process_admin_msg(client, msg) == "LEAVE": return "LEAVE"
        await self.process_chat_msg(client, msg)
        if msg.type == "JOIN_TEAM": await self.on_join_team(client, msg)
        elif msg.type == "LIST_TEAMS": self.on_list_teams(client)
        elif msg.type == "LIST_PLAYERS": self.tell_player_list(client)
        elif msg.type == "MARK_READY": await self.on_mark_ready(client, msg)
        elif msg.type == "LEAVE": return "LEAVE"
        elif msg.type == "FORCE_START": await self.on_force_start(client, msg)
        elif msg.type == "JOIN_GAME_NOW": await self.on_join_game_now(client, msg)

        # elif msg.type == "": await self.on_join_room(client, msg)
        # elif msg.type == "": await self.on_join_code(client, msg)
        # else: client.tell_warning(f"Unknown message type: {msg.type}")

    async def on_join_team(self, client: "Client", msg: Message):
        if self.game_start_forced and self.model.game_start_time > 0 and not self.is_mod(client):
            client.tell_info(f"You cannot change teams as the game was force-started by a mod.")
            return
        tn = msg.payload['team_n']
        if tn < 0 or tn >= self.model.n_teams: return client.tell_warning(f"Team {tn+1} does not exist!")
        self.remove_player_from_teams(client)
        self.set_player_ready(client, False)
        self.uid_to_teams[client.user.uid] = tn
        if client not in self.teams[tn]:
            self.teams[tn].append(client)
        self.broadcast_msg(Message(type="PLAYER_JOINED_TEAM", payload=dict(uid=client.user.uid, team=tn), visibility="global"))
        self.broadcast_player_ready(client)
        self.check_game_start_abort(client)

    def on_list_teams(self, client: "Client", msg: Message = None):
        teams = [[c.user.uid for c in team] for  team in self.teams]
        client.write_message(type="LIST_TEAMS", payload={'teams': teams}, visibility="global")
        uids = [c.user.uid for c in self.clients]
        ready = [self.players_ready.get(c.user.uid, False) for c in self.clients]
        client.write_message(type="LIST_READY_STATUS", payload={'uids': uids, 'ready': ready}, visibility="global")

    def remove_player_from_teams(self, client: "Client"):
        if client.user.uid in self.uid_to_teams:
            del self.uid_to_teams[client.user.uid]
        for team in self.teams:
            while client in team:
                team.remove(client)

    def has_game_started(self):
        return 0 < self.model.game_start_time < time.time()

    async def on_mark_ready(self, client: "Client", msg: Message):
        if client.user.uid not in self.uid_to_teams:
            return client.tell_error("You must join a team before you can set yourself ready.")
        if self.has_game_started():
            return client.tell_warning(f"Cannot change ready status after game started.")
        is_ready = bool(msg.payload['ready'])
        self.set_player_ready(client, is_ready)
        self.broadcast_player_ready(client)
        self.check_game_start_abort(client)

    def check_game_start_abort(self, client: "Client"):
        everyone_ready = self.ready_count == len(self.clients)
        teams_populated = all(len(team) > 0 for team in self.teams)
        if everyone_ready and teams_populated and 0 > self.model.game_start_time:
            self.on_game_start()
        elif 0 < self.model.game_start_time: # game started
            if self.game_start_forced and not self.is_mod(client.user): return
            # abort start game
            self.model.game_start_time = -1
            self.model.is_open = True
            self.persist_model()
            self.broadcast_msg(Message(type="GAME_START_ABORT", payload={}))

    def set_player_ready(self, client: "Client", is_ready: bool):
        self.players_ready[client.user.uid] = is_ready
        self.ready_count = sum(1 if self.players_ready.get(c.user.uid, False) else 0 for c in self.clients)

    def broadcast_player_ready(self, client: "Client"):
        self.broadcast_msg(Message(type="PLAYER_READY", payload=dict(uid=client.user.uid, is_ready=self.players_ready[client.user.uid], ready_count=self.ready_count), visibility="global"))

    @HasAdmins.mod_only
    async def on_force_start(self, client: "Client", msg: Message):
        self.on_game_start(forced=True)

    game_start_forced = False

    def on_game_start(self, forced=False):
        self.game_start_forced = forced
        wait_time = 5.0
        start_time = time.time() + wait_time
        self.model.game_start_time = start_time
        self.model.is_open = False
        self.persist_model()
        msg_j = dict(type="GAME_STARTING_AT", payload={'start_time': start_time, 'wait_time': wait_time})
        self.broadcast_json(msg_j)

    async def on_join_game_now(self, client: "Client", *args):
        game_started = self.has_game_started()
        if not game_started:
            time_left = self.model.game_start_time - time.time()
            if 0 < time_left < 1.0:
                await asyncio.sleep(time_left + 0.05)
            elif time_left >= 1.0:
                client.tell_warning(f"Can't join the game early.")
                return
            game_started = self.has_game_started()
        # only true if abort happened during above
        if not game_started: return
        # this awaits loading our maps if they're not already loaded
        await self.initialized()
        if self.game is None:
            team_order = list(range(len(self.teams)))
            random.shuffle(team_order)
            game_model = GameSession(
                admins=self.model.admins,
                mods=self.model.mods,
                players=[c.user for team in self.teams for c in team],
                room=self.name,
                lobby=self.lobby_inst.name,
                teams=[[c.user.uid for c in team] for team in self.teams],
                team_order=team_order,
                map_list=self.model.map_list,
            )
            self.game = GameController(game_model, self)
            await game_model.save()
        # game has started, so hand off to game lobby
        await self.handoff_to_game(client)

    async def handoff_to_game(self, client: "Client"):
        self.on_client_handed_off(client)
        await self.game.handoff(client)
        self.on_client_entered(client)



class GameController(HasChats):
    model: GameSession
    room: RoomController
    container_type: Literal["lobby", "room", "game"] = "game"
    teams: list[list["Client"]]
    track_id_to_players: dict[int, list["Client"]]

    @property
    def to_full_game_info_json(self):
        return dict(
            game_opts=self.room.model.game_opts,
            **self.model.to_full_game_info_json)

    @property
    def to_inprog_game_info_json(self):
        return self.model.to_inprog_game_info_json

    @property
    def get_maps_list_full_for_info(self):
        return [self.room.maps[tid].safe_json_shorter for tid in self.model.map_list]

    @property
    def name(self):
        return self.model.name

    def __init__(self, model: GameSession | None, room_inst: RoomController):
        self.model = model
        self.room = room_inst
        super().__init__()
        # clients assigned on joining the game
        self.teams = list(list() for _ in model.teams)
        self.fix_players_order()

    def fix_players_order(self):
        players = dict()
        for p in self.model.players:
            players[p.uid] = p
        new_players = []
        for team in self.model.teams:
            for uid in team:
                new_players.append(players[uid])
        self.model.players = new_players


    def players_team(self, uid: str):
        '''return the team based on a uid. this is NOT accurate when 1 account is running 2 clients.'''
        for i, team in enumerate(self.model.teams):
            if uid in team:
                return i
        return -1

    def includes_player(self, uid):
        return self.players_team(uid) >= 0

    async def handoff(self, client: "Client"):
        if client.user in self.model.kicked_players:
            client.tell_error(f"You can't join again because you were already kicked.")
            return
        if client in self.clients:
            warn(f"I already have client: {client}")
            client.tell_warning("Tried to join room twice. This is probably a bug.")
            return
        self.on_client_entered(client)
        try:
            await self.run_client(client)
        except Exception as e:
            warning(f"[{client.client_ip}] Disconnecting: Exception during GameController.run_client: {e}\n{''.join(traceback.format_exception(e))}")
            client.tell_error("Unknown server error.")
        self.on_client_handed_off(client)

    def tell_client_curr_scope(self, client: "Client"):
        client.set_scope(f"3|{self.name}")

    async def game_info_loop(self, client: "Client"):
        while client in self.clients and not client.disconnected:
            client.write_message("GAME_INFO", self.to_inprog_game_info_json)
            await asyncio.sleep(5.0)

    def send_game_info_full(self, client: "Client"):
        client.write_message("GAME_INFO_FULL", self.to_full_game_info_json)

    def send_maps_info_full(self, client: "Client"):
        client.write_message("MAPS_INFO_FULL", dict(maps=self.get_maps_list_full_for_info))

    def on_client_handed_off(self, client: "Client"):
        self.broadcast_player_left(client)
        if client in self.clients:
            self.clients.remove(client)

    def on_client_entered(self, client: "Client"):
        self.tell_client_curr_scope(client)
        client.tell_info(f"Entered Game: {self.name}")
        asyncio.create_task(self.game_info_loop(client))
        self.send_recent_chat(client)
        self.send_admin_mod_status(client)
        self.tell_player_list(client)
        self.send_game_info_full(client)
        self.send_maps_info_full(client)
        self.clients.add(client)
        self.assign_player_to_team(client)
        self.broadcast_player_joined(client)
        self.replay_game_so_far(client)

    def on_client_left(self, client: "Client"):
        self.on_client_handed_off(client)
        client.disconnect()

    def replay_game_so_far(self, client: "Client"):
        client.write_message("GAME_REPLAY_START", {'n_msgs': len(self.model.game_msgs)})
        for msg in self.model.game_msgs:
            client.write_json(msg.safe_json)
        client.write_message("GAME_REPLAY_END", {})

    def assign_player_to_team(self, client: "Client"):
        uid = client.user.uid
        for team_i, team in enumerate(self.model.teams):
            if uid in team:
                self.teams[team_i].append(client)
                return
        log.warn(f"Game cannot assign player to team: {client.user.name} / {uid}")

    async def run_client(self, client: "Client"):
        while True:
            msg = await client.read_valid()
            if msg is None:
                self.on_client_left(client)
                return
            if await self.process_msg(client, msg) == "LEAVE":
                break

    map_msg_types = {"CP_TIME", "FINAL_TIME", "ENTER_MAP", "LEAVE_MAP"}
    vote_msg_types = {"MAP_REROLL_VOTE_START", "MAP_REROLL_VOTE_SUBMIT", "MOD_MAP_REROLL"}

    async def process_msg(self, client: "Client", msg: Message):
        if await self.process_admin_msg(client, msg) == "LEAVE": return "LEAVE"
        await self.process_chat_msg(client, msg)
        if msg.type == "LEAVE": return "LEAVE"

        # note: if streaming data is added (car pos or mouse pos), it should not be cached

        is_game_msg = True
        if msg.type.startswith("G_"): await self.on_game_msg(client, msg)
        elif msg.type in self.map_msg_types: await self.on_map_msg(client, msg)
        elif msg.type in self.vote_msg_types: await self.on_map_vote_msg(client, msg)
        else: is_game_msg = False
        if is_game_msg:
            self.model.game_msgs.append(msg)
            self.persist_model()

    async def on_game_msg(self, client: "Client", msg: Message):
        # todo: visibility stuff
        self.broadcast_game_msg(msg)

    def broadcast_game_msg(self, msg: Message):
        msg.payload['seq'] = len(self.model.game_msgs)
        msg.save_via_task()
        self.persist_model()
        return self.broadcast_msg(msg)

    async def on_map_msg(self, client: "Client", msg: Message):
        if msg.type == "LEAVE_MAP": return await self.on_map_leave(client, msg)
        if msg.type == "ENTER_MAP": return await self.on_map_enter(client, msg)
        if msg.type == "FINAL_TIME": return await self.on_map_finish(client, msg)
        if msg.type == "CP_TIME": return await self.on_map_cp(client, msg)
        else: log.warning(f"Unknown map msg: {msg.type}")

    async def on_map_leave(self, client: "Client", msg: Message):
        pass

    async def on_map_enter(self, client: "Client", msg: Message):
        pass

    async def on_map_finish(self, client: "Client", msg: Message):
        pass

    async def on_map_cp(self, client: "Client", msg: Message):
        pass

    async def on_map_vote_msg(self, client: "Client", msg: Message):
        # todo
        if msg.type == "MOD_MAP_REROLL": return await self.on_map_mod_reroll(client, msg)
        elif msg.type == "MAP_REROLL_VOTE_START": return await self.on_map_reroll_start(client, msg)
        elif msg.type == "MAP_REROLL_VOTE_SUBMIT": return await self.on_map_reroll_vote_submit(client, msg)
        else: log.warning(f"Unknown map vote msg: {msg.type}")

    @HasAdmins.mod_only
    async def on_map_mod_reroll(self, client: "Client", msg: Message):
        pass

    async def on_map_reroll_start(self, client: "Client", msg: Message):
        pass

    async def on_map_reroll_vote_submit(self, client: "Client", msg: Message):
        pass



class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    uid: str
    user: User
    lobby: "Lobby"
    disconnected: bool = False

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self.uid = os.urandom(16).hex()
        self.user = None
        self.disconnected = False
        asyncio.create_task(self.ping_loop())

    def __hash__(self) -> int:
        return hash(self.uid)

    @property
    def client_ip(self) -> str:
        return self.reader._transport.get_extra_info('peername')

    async def ping_loop(self):
        while self.user is None:
            await asyncio.sleep(1.0)
        while not self.disconnected:
            if self.reader.at_eof():
                return
            self.send_server_info()
            await asyncio.sleep(5.0)

    def send_server_info(self):
        self.write_json({"server": {"version": SERVER_VERSION, "n_clients": len(all_clients)}})

    async def read_msg_inner(self) -> str:
        try:
            bs = None
            reader_ex = self.reader.exception()
            if reader_ex is None:
                bs = await self.reader.read(2)
            if (bs is None or len(bs) != 2):
                info(f"Client disconnected?? -- Maybe Reader Exception? e:{reader_ex}")
                self.disconnect()
                return None
            msg_len, = struct.unpack_from('<H', bs)
            # debug(f"Reading message of length: {msg_len}")
            incoming = await self.reader.read(msg_len)
            incoming = incoming.decode("UTF8")
            # debug(f"Read message: {incoming}")
            if self.user is not None:
                self.user.last_seen = time.time()
            if incoming == "END":
                info(f"Client disconnecting: {self.client_ip}")
                self.disconnect()
                return None
            if incoming == "PING": # skip pings
                if self.user is not None:
                    pass
                    # info(f"Got ping from user: {self.user.name} / {self.user.uid}")
                return None
            return incoming
        except Exception as e:
            self.tell_error(f"Unable to read message. {e}")
            # warn(traceback.format_exception(e))
        return None

    async def read_msg(self) -> str:
        msg_str = await self.read_msg_inner()
        while msg_str is None and not self.disconnected:
            msg_str = await self.read_msg_inner()
        return msg_str

    async def read_json(self) -> dict:
        msg = await self.read_msg()
        if msg is None or msg == "":
            return None
        return json.loads(msg)

    async def read_valid(self) -> Message:
        msg = await self.read_json()
        if msg is None: return None
        msg = self.validate_pl(msg)
        if not msg.type.startswith("LOGIN"):
            await msg.insert()
        return msg

    def write_raw(self, msg: str):
        if (self.writer.is_closing()): return
        if (len(msg) >= 2**16):
            raise MsgException(f"msg too long ({len(msg)})")
        # debug(f"Write message ({len(msg)}): {msg}")
        self.writer.write(struct.pack('<H', len(msg)))
        self.writer.write(bytes(msg, "UTF8"))

    def write_json(self, data: dict):
        return self.write_raw(json.dumps(data))

    def write_message(self, type: str, payload: Any, **kwargs):
        return self.write_json(dict(type=type, payload=payload, **kwargs))

    def set_scope(self, scope: str):
        if self.disconnected: return
        self.write_json({"scope": scope})
        self.user.set_last_scope(scope)

    async def main_loop(self):
        try:
            self.send_server_info()
            self.lobby = await get_main_lobby()
            while not self.disconnected and self.user is None:
                await self.init_client()
        except Exception as e:
            self.tell_error(f"Exception: {e}")
            warn(f"[Client:{self.client_ip}] Got exception: {e}, \n{''.join(traceback.format_exception(e))}")
        if self.user is None:
            warn(f"Failed to init for {self.client_ip} -- bailing")
        else:
            # rejoin
            lobby_name = None
            room_name = None
            game_name = None
            if len(self.user.last_scope) >= 3 and self.user.last_seen > (time.time() - (60 * 60 * 3)):
                scope_type = int(self.user.last_scope[:1])
                scope_name = self.user.last_scope[2:]
                log.info(f"Rejoining user {self.user.name} to scope: {self.user.last_scope}")
                if scope_type == 0:
                    pass # main lobby
                elif scope_type == 1:
                    lobby_name = scope_name
                elif scope_type == 2:
                    room_name = scope_name
                    _r = await Room.find_one(Room.name == scope_name)
                    if _r is not None:
                        lobby_name = _r.lobby
                elif scope_type == 3:
                    game_name = scope_name
                    _g = await GameSession.find_one(GameSession.name == scope_name)
                    if _g is not None:
                        lobby_name = _g.lobby
                        room_name = _g.room
                else:
                    log.warning(f"Unknown scope type: {scope_type} from last_scope: {self.user.last_scope}")
            await self.lobby.handoff(self, lobby_name, room_name, game_name)
        self.disconnect()

    async def init_client(self):
        # register_or_login
        msg = await self.read_valid()
        if msg is None:
            return
        user = None
        checked_for_user = False
        if msg.type == "LOGIN_TOKEN":
            checked_for_user = True
            tokenInfo = await check_token(msg['t'])
            if tokenInfo is not None:
                # user = await User.find_one(User.uid == uid_from_wsid(tokenInfo.account_id))
                user = get_user(uid_from_wsid(tokenInfo.account_id))
                if user is None:
                    user = await register_authed_user(tokenInfo)
                # update this in case the user updated their account name
                user.name = tokenInfo.display_name
                if user is not None:
                    self.write_json(dict(type="LOGGED_IN", uid=user.uid, account_id=tokenInfo.account_id, display_name=tokenInfo.display_name))
        if ENABLE_LEGACY_AUTH and msg.type == "LOGIN":
            user = authenticate_user(msg['uid'], msg['username'], msg['secret'])
            checked_for_user = True
            if user is not None:
                await user.set({User.last_seen: time.time(), User.n_logins: user.n_logins + 1})
                self.write_json(dict(type="LOGGED_IN"))
        if ENABLE_LEGACY_AUTH and msg.type == "REGISTER":
            user = await register_user(msg['username'], msg['wsid'])
            checked_for_user = True
            if user is not None:
                self.write_json(dict(type="REGISTERED", payload=user.unsafe_json))
        if user is None:
            if not checked_for_user:
                if ENABLE_LEGACY_AUTH:
                    self.tell_error("Invalid type, must be LOGIN, LOGIN_TOKEN, or REGISTER")
                else:
                    self.tell_error("Invalid type, must be LOGIN_TOKEN")
            else:
                self.tell_error("Login failed")
        self.user = user

    def tell_error(self, msg: str):
        warn(f"[Client:{self.client_ip}] Sending error to client: {msg}")
        self.write_json({"error": msg})

    def tell_warning(self, msg: str):
        warn(f"[Client:{self.client_ip}] Sending warning to client: {msg}")
        self.write_json({"warning": msg})

    def tell_info(self, msg: str):
        warn(f"[Client:{self.client_ip}] Sending info to client: {msg}")
        self.write_json({"info": msg})

    def validate_pl(self, pl: dict) -> Message:
        keys = pl.keys()
        if len(keys) != 3:
            self.tell_error("Bad payload: number of keys != 3")
            return None
        if "type" not in keys or "payload" not in keys or "visibility" not in keys:
            self.tell_error("Bad payload: required keys: `type`, `payload`, `visibility`.")
            return None
        if pl['visibility'] not in ['global', 'team', 'map', 'none']:
            self.tell_error("Bad payload: `visibility` must be 'global', 'team', 'map', or 'none'.")
            return None
        if not isinstance(pl['type'], str):
            self.tell_error("Bad payload: `type` must be a string.")
            return None
        return Message(type=pl['type'], payload=pl['payload'], visibility=pl['visibility'], user=self.user)

    def disconnect(self):
        if self.disconnected: return
        try:
            self.write_raw("END")
        except:
            pass
        transport: asyncio.Transport = self.reader._transport
        self.writer.write_eof()
        self.reader.feed_eof()
        transport.close()
        self.disconnected = True
        self.lobby.on_client_left(self)



class Lobby(HasChats):
    rooms: dict[str, RoomController]
    model: LobbyModel
    container_type = "lobby"

    def __init__(self, model: LobbyModel) -> None:
        super().__init__()
        self.rooms = dict()
        self.model = model
        if (model.name in all_lobbies):
            raise Exception("Lobby created more than once but exists in dict already")
        all_lobbies[model.name] = self
        self.loaded_rooms = False
        self.load_rooms_task = asyncio.create_task(self.load_rooms())
        self.clear_old_rooms_task = asyncio.create_task(self.clear_old_rooms())

    @property
    def name(self):
        return self.model.name

    @property
    def uid(self):
        return self.model.uid

    @property
    def admins(self):
        return self.model.admins

    @property
    def moderators(self):
        return self.model.moderators

    @property
    def parent_lobby(self):
        return self.model.parent_lobby

    @property
    def is_public(self):
        return self.model.is_public

    async def initialized(self):
        while not self.loaded_chat or not self.load_rooms:
            await asyncio.sleep(0.05)

    async def load_rooms(self):
        with timeit_context("Load Rooms"):
            rooms = Room.find(Eq(Room.is_retired, False), GTE(Room.creation_ts, time.time() - 86400), Eq(Room.lobby, self.name), fetch_links=True)
            async for room_model in rooms:
                room = RoomController(room_model, lobby_inst=self)
                self.rooms[room.name] = room
            self.loaded_rooms = True

    async def clear_old_rooms(self):
        await asyncio.sleep(20)
        while not SHUTDOWN:
            n_rooms = len(self.rooms)
            if n_rooms > 0:
                log.info(f"Checking {n_rooms} rooms for old rooms to remove.")
            for room in self.rooms.values():
                # clear rooms older than 6 hrs
                if time.time() > room.model.creation_ts + (60 * 60 * 6):
                    self.retire_room(room)
                    log.info(f"Removed old room: {room.name}")
            # sleep 15 min between loops
            await asyncio.sleep(60 * 15)

    def retire_room(self, room: RoomController):
        if room.model.is_retired and room.name not in self.rooms:
            return
        log.info(f"Retiring room: {room.name}")
        if room.model.is_open:
            room.model.is_open = False
            room.model.is_retired = True
            room.persist_model()
        if room.name in self.rooms:
            self.rooms.pop(room.name)
        self.broadcast_msg(Message(type="ROOM_RETIRED", payload=dict(name=room.name)))

    @property
    def json_info(self):
        _rooms = []
        for r in self.rooms.values():
            if r.is_public and r.is_open:
                _rooms.append(r.to_room_info_json)
        return {
            'name': self.name, 'n_clients': len(self.clients),
            'n_rooms': len(self.rooms), 'n_public_rooms': len(_rooms),
            'rooms': _rooms
        }

    # HANDOFF

    async def handoff(self, client: Client, lobby_name: str = None, room_name: str = None, game_name: str = None):
        if client in self.clients:
            warn(f"I already have client: {client}")
            client.tell_warning("Tried to join lobby twice. This is probably a server bug.")
            return
        self.on_client_entered(client)
        all_clients.add(client)
        await self.initialized()
        # self.tell_client_curr_scope(client)
        info(f"Running client: {client.client_ip}")
        try:
            if lobby_name is not None and lobby_name != self.name:
                await self.handoff_to_game_lobby(client, await get_named_lobby(lobby_name), room_name=room_name, game_name=game_name)
            else:
                if room_name is not None and room_name in self.rooms:
                    await self.handoff_to_room(client, self.rooms[room_name], game_name=game_name)
            await self.run_client(client)
        except Exception as e:
            warning(f"[{client.client_ip}] Disconnecting: Exception during run_client: {e}\n{''.join(traceback.format_exception(e))}")
            client.disconnect()
        self.on_client_handed_off(client)

    def tell_client_curr_scope(self, client: Client):
        client.set_scope(f"{0 if self.parent_lobby is None else 1}|{self.name}")

    async def lobby_info_loop(self, client: Client):
        while client in self.clients and not client.disconnected:
            self.send_lobby_info(client)
            await asyncio.sleep(5.0)

    def send_lobby_info(self, client: Client):
        client.write_message("LOBBY_INFO", self.json_info)

    def on_client_handed_off(self, client: Client):
        if client in self.clients:
            self.clients.remove(client)
        self.broadcast_player_left(client)

    def on_client_entered(self, client: Client):
        if (len(self.model.admins) == 0):
            self.model.admins.append(client.user)
            self.persist_model()
        self.tell_client_curr_scope(client)
        client.tell_info(f"Entered Lobby: {self.name}")
        asyncio.create_task(self.lobby_info_loop(client))
        self.send_lobby_info(client)
        self.send_lobbies_list(client)
        self.send_admin_mod_status(client)
        self.send_recent_chat(client)
        self.tell_player_list(client)
        self.clients.add(client)
        self.broadcast_player_joined(client)

    def on_client_left(self, client: Client):
        client.disconnect()
        if client in self.clients:
            self.clients.remove(client)
        if client in all_clients:
            all_clients.remove(client)

    async def handoff_to_game_lobby(self, client: Client, dest: "Lobby", *args, **kwargs):
        if self.model.parent_lobby is None:
            self.on_client_handed_off(client)
            await dest.handoff(client, *args, **kwargs)
            self.on_client_entered(client)
        else:
            client.tell_warning("Can only hand off to game lobby from the main lobby")

    async def handoff_to_room(self, client: Client, room: "RoomController", *args, **kwargs):
        if self.model.parent_lobby is not None or True:
            self.on_client_handed_off(client)
            await room.handoff(client, *args, **kwargs)
            self.on_client_entered(client)
        else:
            client.tell_warning("Can only hand off to room from a game lobby")

    async def run_client(self, client: Client):
        # self.send_recent_chat(client)
        while True:
            msg = await client.read_valid()
            if msg is None:
                self.on_client_left(client)
                return
            if await self.process_msg(client, msg) == "LEAVE":
                break

    async def process_msg(self, client: Client, msg: Message):
        if await self.process_admin_msg(client, msg) == "LEAVE": return "LEAVE"
        await self.process_chat_msg(client, msg)
        return await self.process_lobby_msg(client, msg)
        # else: client.tell_warning(f"Unknown message type: {msg.type}")

    async def process_lobby_msg(self, client: "Client", msg: Message):
        if msg.type == "CREATE_LOBBY": await self.on_create_lobby(client, msg)
        elif msg.type == "JOIN_LOBBY": await self.on_join_lobby(client, msg)
        elif msg.type == "LEAVE": return "LEAVE"
        elif msg.type == "LIST_LOBBIES": await self.on_list_lobbies(client, msg)
        elif msg.type == "CREATE_ROOM": await self.on_create_room(client, msg)
        elif msg.type == "JOIN_ROOM": await self.on_join_room(client, msg)
        elif msg.type == "JOIN_CODE": await self.on_join_code(client, msg)

    async def on_msg_template(self, client: Client, msg: Message):
        pass

    async def on_create_lobby(self, client: Client, msg: Message):
        if client.user is None:
            client.tell_error(f"Not authenticated!")
        else:
            name = msg.payload['name']
            if name in all_lobbies or await LobbyModel.find_one({'name': name}).exists():
                client.tell_error(f"Lobby named {name} already exists.")
            else:
                model = LobbyModel(name=name)
                model.admins.append(client.user)
                model.parent_lobby = self.name
                await model.save()
                new_lobby = Lobby(model)
                all_lobbies[name] = new_lobby
                client.tell_info(f"Lobby named {new_lobby.name} created successfully.")

    async def on_join_lobby(self, client: Client, msg: Message):
        name = msg.payload['name']
        if name == self.name:
            client.tell_info(f"You are already in the {name} lobby.")
            return
        lobby = await get_named_lobby(name)
        # todo: check permissions or things?
        if lobby is None:
            client.tell_error(f"Cannot find lobby named: {name}")
        else:
            await self.handoff_to_game_lobby(client, lobby)

    async def on_list_lobbies(self, client: Client, msg: Message):
        self.send_lobbies_list(client)

    def send_lobbies_list(self, client: "Client"):
        client.write_json(dict(type="LOBBY_LIST", payload=list(
            l.json_info for l in all_lobbies.values()
        )))

    async def on_create_room(self, client: Client, msg: Message):
        # incoming: {name: string, player_limit: int, n_teams: int}
        name = str(msg.payload['name']) + "##"+gen_uid(4)
        player_limit = max(MIN_PLAYERS, min(MAX_PLAYERS, int(msg.payload['player_limit'])))
        n_teams = max(MIN_TEAMS, min(MAX_TEAMS, int(msg.payload['n_teams'])))
        n_teams = clamp(int(msg.payload['n_teams']), MIN_TEAMS, MAX_TEAMS)
        if n_teams > player_limit:
            return client.tell_error(f"Cannot create a room with more teams than players.")
        is_public = msg.visibility == "global"
        maps_required = clamp(msg.payload['maps_required'], MIN_MAPS, MAX_MAPS)
        min_secs = clamp(msg.payload['min_secs'], MIN_SECS, MAX_SECS)
        max_secs = clamp(msg.payload['max_secs'], MIN_SECS, MAX_SECS)
        if (max_secs < min_secs):
            return client.tell_error(f"max map length less than min map length")
        max_difficulty = clamp(msg.payload.get('max_difficulty', 3), 0, 5)
        game_opts: dict = msg.payload.get('game_opts', dict())
        if not isinstance(game_opts, dict): return client.tell_error(f"Invalid format for game_opts.")
        for k,v in game_opts.items():
            if not isinstance(k, str) or isinstance(v, dict) or isinstance(v, list):
                return client.tell_error(f"Invalid k,v pair in game opts: `{k}: {v}`")
        # send back: {name: string, player_limit: int, n_teams: int, is_public: bool, join_code: str}
        room = RoomController(lobby_inst=self,
            name=name, lobby=self.name,
            player_limit=player_limit, n_teams=n_teams,
            is_public=is_public,
            admins=[msg.user],
            maps_required=maps_required,
            min_secs=min_secs, max_secs=max_secs, max_difficulty=max_difficulty,
            game_opts=game_opts,
        )
        # note: will throw if name collision
        await room.model.save()
        self.rooms[room.name] = room
        self.broadcast_msg(Message(type="NEW_ROOM", payload=room.to_room_info_json))
        client.write_message("ROOM_INFO", room.to_created_room_json)
        await self.handoff_to_room(client, room)

    def update_room_status(self, room: RoomController):
        self.broadcast_msg(Message(type="ROOM_UPDATE", payload=dict(name=room.name, n_players=len(room.clients))))

    async def on_join_room(self, client: Client, msg: Message):
        name = msg.payload.get('name', None)
        room = self.rooms.get(name, None)
        if room is None or not room.is_public:
            client.tell_warning(f"Room not found or is not public: {name}")
        else:
            await self.handoff_to_room(client, room)

    async def on_join_code(self, client: Client, msg: Message):
        code = msg.payload.get('code', None)
        room_model = None if code is None else await Room.find_one({'join_code': code})
        room = None if room_model is None else self.rooms.get(room_model.name, None)
        if room is None:
            client.tell_warning(f"Cannot find room with join code: {code}")
        else:
            await self.handoff_to_room(client, room)




async def populate_all_lobbies():
    async for lobby_model in LobbyModel.find_all(fetch_links=True):
        Lobby(lobby_model)



main_lobby: Lobby = None

async def get_main_lobby() -> Lobby:
    global main_lobby
    if main_lobby is None:
        if "MainLobby" in all_lobbies:
            main_lobby = all_lobbies["MainLobby"]
        else:
            find_lobby = LobbyModel.find_one(dict(name="MainLobby"), fetch_links=True)
            if (await find_lobby.exists()):
                main_lobby = Lobby(await find_lobby)
            else:
                model = LobbyModel(name="MainLobby")
                asyncio.create_task(model.save())
                main_lobby = Lobby(model)
    return main_lobby


async def get_named_lobby(name: str) -> Optional[Lobby]:
    if name in all_lobbies:
        return all_lobbies[name]
    model = await LobbyModel.find_one({'name': name}, fetch_links=True)
    if model is not None:
        return Lobby(model)

class ChatMessages(Document):
    msgs: list[Link[Message]]
    lobby: Optional[Indexed(str, name="lobby_ix")] = None
    room: Optional[Indexed(str, name="room_ix")] = None
    game: Optional[Indexed(str, name="game_ix")] = None

    class Settings:
        use_state_management = True

    def append(self, msg: Message):
        self.msgs.append(msg)
        self.save_changes_in_task()

    def save_changes_in_task(self):
        asyncio.create_task(self.save_changes())

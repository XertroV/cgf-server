import asyncio
import json
from logging import debug, warn, warning
import os
import struct
import traceback
from typing import Any, Literal, Optional

import pymongo
from pydantic.dataclasses import dataclass
from pydantic import Field

from beanie import Document, PydanticObjectId, Link, Indexed
from beanie.operators import GTE, Eq, In
import beanie

from cfg.users import *
from cfg.consts import *

from .User import User
from .consts import SERVER_VERSION


class MsgException(Exception):
    pass


all_clients: set["Client"] = set()
# updated in Lobby constructor
all_lobbies: dict[str, "Lobby"] = dict()

class Message(Document):
    type: Indexed(str)
    payload: dict
    visibility: Indexed(str)
    user: Optional[Link[User]]
    ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=time.time)

    def __getitem__(self, key):
        return self.payload[key]
    def __setitem__(self, key, value):
        self.payload[key] = value

    @property
    def safe_json(self):
        return { "type": self.type, "payload": self.payload, "visibility": self.visibility, "from": None if self.user is None else self.user.safe_json, "ts": self.ts }


class LobbyModel(Document):
    uid: Indexed(str, unique=True) = Field(default_factory=gen_uid)
    name: Indexed(str, unique=True)
    admins: list[Link[User]] = Field(default_factory=list)
    moderators: list[Link[User]] = Field(default_factory=list)
    parent_lobby: Optional[str] = None
    is_public: bool = True
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=lambda: time.time())

    class Settings:
        use_state_management = True
        name = "Lobby"



# class GameLobby(Lobby):
#     pass


class Room(Document):
    name: Indexed(str, unique=True)
    # name: str
    lobby: Indexed(str)
    chat: list[Link[Message]] = Field(default_factory=list)
    admins: list[Link[User]]
    mods: list[Link[User]] = Field(default_factory=list)
    is_public: bool
    join_code: str = Field(default_factory=gen_join_code)
    player_limit: int
    n_teams: int
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=lambda: time.time())
    kicked_players: list[Link[User]] = Field(default_factory=list)

    class Settings:
        use_state_management = True

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
            n_players=0,  # should be updated by room controller
            is_public=self.is_public
        )


class GameSession(Document):
    players: list[Link[User]]
    room: str
    lobby: str
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=lambda: time.time())

    class Settings:
        use_state_management = True
        name = "Game"

    async def handoff(self, client: "Client"):
        pass




class HasClients:
    clients: set["Client"]

    def __init__(self):
        self.clients = set()

    def find_client_from_uid(self, uid: str):
        for client in self.clients:
            if client.user.uid == uid:
                return client

    def broadcast_msg(self, msg: Message):
        msg_j = msg.safe_json
        for client in self.clients:
            # if client.user != msg.user:
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



class HasAdminsModel(Document):
    admins: list[Link[User]]
    mods: list[Link[User]]


class HasAdmins(HasClients):
    model: HasAdminsModel

    @property
    def admins(self) -> list[Link[User]]:
        return self.model.admins

    @property
    def mods(self) -> list[Link[User]]:
        return self.model.mods

    def is_admin(self, user: User):
        return user in self.admins or any(map(lambda a: a.ref.id == user.id, self.admins))

    def is_mod(self, user: User):
        return user in self.mods or any(map(lambda a: a.ref.id == user.id, self.mods))

    def remove_admin(self, user: User):
        self.admins.remove(user)

    def remove_mod(self, user: User):
        self.mods.remove(user)

    def persist_model(self):
        asyncio.create_task(self.model.save_changes())

    # call this from main process_msg function
    async def process_admin_msg(self, client: "Client", msg: Message):
        if msg.type == "ADD_ADMIN": await self.on_add_admin(client, msg)
        elif msg.type == "RM_ADMIN": await self.on_rm_admin(client, msg)
        elif msg.type == "ADD_MOD": await self.on_add_mod(client, msg)
        elif msg.type == "RM_MOD": await self.on_rm_mod(client, msg)
        elif msg.type == "KICK_PLAYER": await self.on_kick_player(client, msg)

    def admin_only(f):
        def inner(self, client: "Client", msg: Message):
            if not self.is_admin(client.user):
                client.tell_warning("Permission denied (Admin only)")
            else:
                return f(client, msg)
        return inner

    def mod_only(f):
        def inner(self, client: "Client", msg: Message):
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
        pass


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
        if self.chats is not None: return
        self.chats = await ChatMessages.find_one({self.container_type: self.name}, with_children=True)
        if self.chats is None:
            self.chats = ChatMessages(**{self.container_type: self.name, 'msgs': list()})
            asyncio.create_task(self.chats.save())
        n_msgs = len(self.chats.msgs)
        start_ix = max(0, n_msgs - 20)
        msg_ids = [self.chats.msgs[i].ref.id for i in range(start_ix, n_msgs)]
        self.recent_chat_msgs = await Message.find(In(Message.id, msg_ids), fetch_links=True).to_list()
        info(f"Loaded recent chats; nb: {len(self.recent_chat_msgs)}; ids: {msg_ids}")
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
        info(f"Sent recent chats ({len(self.recent_chat_msgs)}) to {client.client_ip}")




class RoomController(HasChats):
    model: Room
    games: set["GameController"]
    lobby_inst: "Lobby"
    container_type: Literal["lobby", "room", "game"] = "room"

    def __init__(self, model=None, lobby_inst=None, **kwargs):
        super().__init__()
        self.model = model if model is not None else Room(**kwargs)
        self.lobby_inst: "Lobby" = lobby_inst
        asyncio.create_task(self.load_games())

    async def load_games(self):
        games = GameSession.find(
            GTE(GameSession.creation_ts, time.time() - 3600), # within last hour
            Eq(GameSession.room, self.name),
            Eq(GameSession.lobby, self.lobby_inst.name),
            fetch_links=True)
        async for game_model in games:
            game = GameController(game_model, room_inst=self)
            self.games[game.name] = game
        self.loaded_games = True

    @property
    def name(self): return self.model.name

    @property
    def to_created_room_json(self):
        ret = self.model.to_created_room_json
        ret['n_players'] = len(self.clients)
        return ret

    @property
    def to_room_info_json(self):
        ret = self.model.to_room_info_json
        ret['n_players'] = len(self.clients)
        return ret

    @property
    def is_public(self):
        return self.model.is_public

    async def handoff(self, client: "Client"):
        # todo: does this work?
        if client.user in self.model.kicked_players:
            client.tell_error(f"You can't join again because you were already kicked.")
            return
        if client in self.clients:
            warn(f"I already have client: {client}")
            client.tell_warning("Tried to join room twice. This is probably a bug.")
            return
        self.on_client_entered(client)
        # await self.initialized()
        # self.tell_client_curr_scope(client)
        try:
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
            client.write_message("ROOM_INFO", self.to_created_room_json)
            await asyncio.sleep(5.0)

    def on_client_handed_off(self, client: "Client"):
        if client in self.clients:
            self.clients.remove(client)
        self.broadcast_player_left(client)

    def on_client_entered(self, client: "Client"):
        self.tell_client_curr_scope(client)
        client.tell_info(f"Entered Room: {self.name}")
        asyncio.create_task(self.room_info_loop(client))
        self.broadcast_player_joined(client)
        self.send_recent_chat(client)
        self.clients.add(client)

    def on_client_left(self, client: "Client"):
        client.disconnect()
        if client in self.clients:
            self.clients.remove(client)
        self.broadcast_player_left(client)

    # main room loop
    # - chatting
    # - join teams
    # - mark ready
    async def run_client(self, client: "Client"):
        # self.send_recent_chat(client)
        while True:
            msg = await client.read_valid()
            if msg is None:
                self.on_client_left(client)
                return
            if await self.process_msg(client, msg) == "LEAVE_ROOM":
                break

    async def process_msg(self, client: "Client", msg: Message):
        await self.process_admin_msg(client, msg)
        await self.process_chat_msg(client, msg)
        if msg.type == "JOIN_TEAM": await self.on_join_team(client, msg)
        elif msg.type == "MARK_READY": await self.on_mark_read(client, msg)
        elif msg.type == "LEAVE_ROOM": return "LEAVE_ROOM"

        # elif msg.type == "": await self.on_join_room(client, msg)
        # elif msg.type == "": await self.on_join_code(client, msg)
        # else: client.tell_warning(f"Unknown message type: {msg.type}")




class GameController(HasChats):
    model: GameSession



class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    uid: str
    user: User
    lobby: "Lobby"

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
            bs = await self.reader.read(2)
            if (len(bs) != 2):
                info(f"Client disconnected??")
                self.disconnect()
                return
            msg_len, = struct.unpack_from('<H', bs)
            debug(f"Reading message of length: {msg_len}")
            incoming = await self.reader.read(msg_len)
            incoming = incoming.decode("UTF8")
            info(f"Read message: {incoming}")
            if self.user is not None:
                self.user.last_seen = time.time()
            if incoming == "END":
                info(f"Client disconnecting: {self.client_ip}")
                self.disconnect()
                return None
            if incoming == "PING": # skip pings
                if self.user is not None:
                    info(f"Got ping from user: {self.user.name} / {self.user.uid}")
                return None
            return incoming
        except Exception as e:
            self.tell_error(f"Unable to read message. {e}")
            warn(traceback.format_exception(e))

    async def read_msg(self) -> str:
        msg_str = await self.read_msg_inner()
        while msg_str is None and not self.disconnected:
            msg_str = await self.read_msg_inner()
        return msg_str

    async def read_json(self) -> dict:
        msg = await self.read_msg()
        if msg is None: return None
        return json.loads(msg)

    async def read_valid(self) -> Message:
        msg = await self.read_json()
        if msg is None: return None
        msg = self.validate_pl(msg)
        await msg.insert()
        return msg

    def write_raw(self, msg: str):
        if (self.writer.is_closing()): return
        if (len(msg) >= 2**16):
            raise MsgException(f"msg too long ({len(msg)})")
        debug(f"Write message ({len(msg)}): {msg}")
        self.writer.write(struct.pack('<H', len(msg)))
        self.writer.write(bytes(msg, "UTF8"))

    def write_json(self, data: dict):
        return self.write_raw(json.dumps(data))

    def write_message(self, type: str, payload: Any, **kwargs):
        return self.write_json(dict(type=type, payload=payload, **kwargs))

    def set_scope(self, scope: str):
        self.write_json({"scope": scope})

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
            await self.lobby.handoff(self)
        self.disconnect()

    async def init_client(self):
        # register_or_login
        pl_j = await self.read_json()
        msg = self.validate_pl(pl_j)
        if msg is None:
            return
        user = None
        checked_for_user = False
        if msg.type == "LOGIN":
            user = authenticate_user(msg['uid'], msg['username'], msg['secret'])
            checked_for_user = True
            if user is not None:
                await user.set({User.last_seen: time.time(), User.n_logins: user.n_logins + 1})
                self.write_json(dict(type="LOGGED_IN"))
        if msg.type == "REGISTER":
            user = await register_user(msg['username'], msg['wsid'])
            checked_for_user = True
            if user is not None:
                self.write_json(dict(type="REGISTERED", payload=user.unsafe_json))
        if user is None:
            if not checked_for_user:
                self.tell_error("Invalid type, must be LOGIN or REGISTER")
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
        asyncio.create_task(self.load_rooms())


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
        while not self.loaded_chat or not self.loaded_rooms:
            await asyncio.sleep(0.05)

    async def load_rooms(self):
        rooms = Room.find(GTE(Room.creation_ts, time.time() - 86400), Eq(Room.lobby, self.name), fetch_links=True)
        async for room_model in rooms:
            room = RoomController(room_model, lobby_inst=self)
            self.rooms[room.name] = room
        self.loaded_rooms = True

    @property
    def json_info(self):
        _rooms = []
        for r in self.rooms.values():
            if r.model.is_public:
                _rooms.append(r.to_room_info_json)
        return {
            'name': self.name, 'n_clients': len(self.clients),
            'n_rooms': len(self.rooms), 'n_public_rooms': len(_rooms),
            'rooms': _rooms
        }

    # HANDOFF

    async def handoff(self, client: Client):
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
            await self.run_client(client)
        except Exception as e:
            warning(f"[{client.client_ip}] Disconnecting: Exception during run_client: {e}\n{''.join(traceback.format_exception(e))}")
            client.disconnect()
        self.on_client_handed_off(client)

    def tell_client_curr_scope(self, client: Client):
        client.set_scope(f"{0 if self.parent_lobby is None else 1}|{self.name}")

    async def lobby_info_loop(self, client: Client):
        while client in self.clients and not client.disconnected:
            client.write_message("LOBBY_INFO", self.json_info)
            await asyncio.sleep(5.0)

    def on_client_handed_off(self, client: Client):
        if client in self.clients:
            self.clients.remove(client)
        self.broadcast_player_left(client)

    def on_client_entered(self, client: Client):
        self.tell_client_curr_scope(client)
        client.tell_info(f"Entered Lobby: {self.name}")
        asyncio.create_task(self.lobby_info_loop(client))
        self.broadcast_player_joined(client)
        self.send_recent_chat(client)
        self.clients.add(client)

    def on_client_left(self, client: Client):
        client.disconnect()
        if client in self.clients:
            self.clients.remove(client)
        if client in all_clients:
            all_clients.remove(client)

    async def handoff_to_game_lobby(self, client: Client, dest: "Lobby"):
        if self.model.parent_lobby is None:
            self.on_client_handed_off(client)
            await dest.handoff(client)
            self.on_client_entered(client)
        else:
            client.tell_warning("Can only hand off to game lobby from the main lobby")

    async def handoff_to_room(self, client: Client, room: "RoomController"):
        if self.model.parent_lobby is not None or True:
            self.on_client_handed_off(client)
            await room.handoff(client)
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
            if await self.process_msg(client, msg) == "LEAVE_LOBBY":
                break

    async def process_msg(self, client: Client, msg: Message):
        await self.process_chat_msg(client, msg)
        await self.process_admin_msg(client, msg)
        return await self.process_lobby_msg(client, msg)
        # else: client.tell_warning(f"Unknown message type: {msg.type}")

    async def process_lobby_msg(self, client: "Client", msg: Message):
        if msg.type == "CREATE_LOBBY": await self.on_create_lobby(client, msg)
        elif msg.type == "JOIN_LOBBY": await self.on_join_lobby(client, msg)
        elif msg.type == "LEAVE_LOBBY": return "LEAVE_LOBBY"
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
        client.write_json(dict(type="LOBBY_LIST", payload=list(
            l.json_info for l in all_lobbies.values()
        )))

    async def on_create_room(self, client: Client, msg: Message):
        # incoming: {name: string, player_limit: int, n_teams: int}
        name = str(msg.payload['name'])
        player_limit = max(MIN_PLAYERS, min(MAX_PLAYERS, int(msg.payload['player_limit'])))
        n_teams = max(MIN_TEAMS, min(MAX_TEAMS, int(msg.payload['n_teams'])))
        is_public = msg.visibility == "global"
        # send back: {name: string, player_limit: int, n_teams: int, is_public: bool, join_code: str}
        room = RoomController(lobby_inst=self,
            name=name, lobby=self.name,
            player_limit=player_limit, n_teams=n_teams,
            is_public=is_public,
            admins=[msg.user]
        )
        await room.model.save()
        self.rooms[room.name] = room
        client.write_message("ROOM_INFO", room.to_created_room_json)
        await self.handoff_to_room(client, room)

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
    lobby: Optional[str] = None
    room: Optional[str] = None
    game: Optional[str] = None

    class Settings:
        use_state_management = True

    def append(self, msg: Message):
        self.msgs.append(msg)
        asyncio.create_task(self.save_changes())

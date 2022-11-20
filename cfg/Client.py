import asyncio
from dataclasses import dataclass
import json
from logging import debug, warn, warning
import os
import struct
import traceback

from cfg.users import *

from .User import User
from .consts import SERVER_VERSION


class MsgException(Exception):
    pass


@dataclass
class Message:
    type: str
    payload: dict
    visibility: str
    user: User
    ts: float

    def __getitem__(self, key):
        return self.payload[key]
    def __setitem__(self, key, value):
        self.payload[key] = value

    @property
    def json(self):
        return { "type": self.type, "payload": self.payload, "visibility": self.visibility, "from": self.user.json, "ts": self.ts }


class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    uid: str
    user: User
    lobby: "Lobby"

    def __init__(self, reader, writer, lobby) -> None:
        self.reader = reader
        self.writer = writer
        self.uid = os.urandom(16).hex()
        self.user = None
        self.disconnected = False
        self.lobby = lobby
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
        self.write_json({"server": {"version": SERVER_VERSION, "nbClients": len(all_clients)}})

    async def read_msg(self) -> str:
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
                    info(f"Got ping from user: {self.user.name} / {self.user.id}")
                return await self.read_msg()
            return incoming
        except Exception as e:
            self.tell_error(f"Unable to read message. {e}")
            warn(traceback.format_exception(e))

    async def read_json(self) -> dict:
        msg = await self.read_msg()
        if msg is None: return None
        return json.loads(msg)

    async def read_valid(self) -> Message:
        msg = await self.read_json()
        if msg is None: return None
        return self.validate_pl(msg)

    def write_msg(self, msg: str):
        if (len(msg) >= 2**16):
            raise MsgException(f"msg too long ({len(msg)})")
        debug(f"Write message ({len(msg)}): {msg}")
        self.writer.write(struct.pack('<H', len(msg)))
        self.writer.writelines([bytes(msg, "UTF8")])

    def write_json(self, data: dict):
        return self.write_msg(json.dumps(data))

    def set_scope(self, scope: str):
        self.write_json({"scope": scope})

    async def main_loop(self):
        try:
            self.send_server_info()
            while not self.disconnected and self.user is None:
                await self.init_client()
        except Exception as e:
            self.tell_error(f"Exception: {e}")
            warn(f"[Client:{self.client_ip}] Got exception: {e}, \n{''.join(traceback.format_exception(e))}")
        if self.user is None:
            warn(f"Failed to init for {self.client_ip} -- bailing")
        else:
            self.set_scope("0|MainLobby")
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
            user = authenticate_user(msg['id'], msg['username'], msg['secret'])
            checked_for_user = True
            if user is not None:
                self.write_json(dict(type="LOGGED_IN"))
        if msg.type == "REGISTER":
            user = register_user(msg['username'], msg['wsid'])
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
        return Message(pl['type'], pl['payload'], pl['visibility'], self.user, time.time())

    def disconnect(self):
        if self.disconnected: return
        transport: asyncio.Transport = self.reader._transport
        self.writer.write_eof()
        self.reader.feed_eof()
        transport.close()
        self.disconnected = True
        self.lobby.on_client_left(self)


all_clients: set[Client] = set()


class Lobby:
    clients: set[Client]
    recent_chat_msgs: list[Message]

    def __init__(self) -> None:
        self.clients = set()
        self.recent_chat_msgs = list()

    async def handoff(self, client: Client):
        if client in self.clients:
            warn(f"I already have client: {client}")
            client.tell_warning("Tried to join lobby twice. This is probably a server bug.")
            return
        self.on_client_entered(client)
        all_clients.add(client)
        info(f"Running client: {client.client_ip}")
        try:
            await self.run_client(client)
        except Exception as e:
            warning(f"[{client.client_ip}] Disconnecting: Exception during run_client: {e}")
            client.disconnect()
        self.on_client_handed_off(client)

    def on_client_handed_off(self, client: Client):
        if client in self.clients:
            self.clients.remove(client)

    def on_client_entered(self, client: Client):
        self.clients.add(client)

    async def handoff_to_game_lobby(self, client: Client, dest: "Lobby"):
        self.on_client_handed_off(client)
        await dest.handoff(client)
        self.on_client_entered(client)

    async def handoff_to_room(self, client: Client, room: "Room"):
        self.on_client_handed_off(client)
        await room.handoff(client)
        self.on_client_entered(client)

    def on_client_left(self, client: Client):
        client.disconnect()
        if client in self.clients:
            self.clients.remove(client)
        if client in all_clients:
            all_clients.remove(client)

    async def run_client(self, client: Client):
        await self.send_recent_chat(client)
        while True:
            msg = await client.read_valid()
            if msg is None:
                self.on_client_left(client)
                return
            await self.process_msg(client, msg)

    async def send_recent_chat(self, client: Client):
        for msg in self.recent_chat_msgs:
            client.write_json(msg.json)
        info(f"Sent recent chats ({len(self.recent_chat_msgs)}) to {client.client_ip}")

    async def process_msg(self, client: Client, msg: Message):
        if msg.type == "SEND_CHAT": await self.on_chat_msg(client, msg)
        elif msg.type == "CREATE_ROOM": await self.on_create_room(client, msg)
        elif msg.type == "JOIN_ROOM": await self.on_join_room(client, msg)
        else: client.tell_error(f"Unknown message type: {msg.type}")

    def broadcast_msg(self, msg: Message):
        msg_j = msg.json
        for client in self.clients:
            # if client.user != msg.user:
            client.write_json(msg_j)

    async def on_msg_template(self, client: Client, msg: Message):
        pass

    chat_msg_keys = {"content"}

    async def on_chat_msg(self, client: Client, msg: Message):
        if (msg.user is None): return
        if set(msg.payload.keys()) != self.chat_msg_keys:
            return client.tell_error(f"Chat message expects keys: {self.chat_msg_keys}")
        if not isinstance(msg['content'], str) or len(msg['content']) > 1024:
            return client.tell_error(f"Content wrong type or >1024 in length")
        if len(self.recent_chat_msgs) > 19:
            self.recent_chat_msgs = self.recent_chat_msgs[:-19]
        self.recent_chat_msgs.append(msg)
        self.broadcast_msg(msg)

    async def on_create_room(self, client: Client, msg: Message):
        pass

    async def on_join_room(self, client: Client, msg: Message):
        pass




class GameLobby(Lobby):
    pass


class Room:
    async def handoff(self, client: Client):
        pass




lobby = None

def get_lobby() -> Lobby:
    global lobby
    if lobby is None:
        lobby = Lobby()
    return lobby

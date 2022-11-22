# Protocol / message docs

## Special Msgs

| type | description | extra |
|--- |--- |--- |
| PING | client sends to server every 5s ||
| END | client sends to server on disconnect, or vice versa ||
| "server" | server status info, sent to client regularly | `{"server": {"version": string, "n_clients": int}}` |
| "scope" | server updates client on currently active scope (MainLobby, InGameLobby, InRoom, InGame) | `{"scope": string}` |

## To Server

| implemented | scope | type | payload | visibility req | notes |
|---|--- |--- |--- |--- |--- |
|y| init | REGISTER | `{username: string, wsid: string}` | none ||
|y| init | LOGIN | `account` | none ||
|y| `0|MainLobby` | CREATE_LOBBY | `{name: string}` | global | used by developers to create a game lobby. note: lobbies that are not whitelisted may be deleted after 1hr. |
|y| `0|MainLobby` | JOIN_LOBBY | `{name: string}` | global | join a game lobby |
|y| `0|MainLobby` | LIST_LOBBIES | `` | none | request a list of known lobbies |
|y| `0|<LobbyName>` | LEAVE_LOBBY | `` | global | leave a game lobby |
|n| `1|<LobbyName>` | CREATE_ROOM | `{name: string, player_limit: int, n_teams: int}` | none, global | | visibility corresponds to private/public room. public rooms are listed and can be joined by anyone. |
|n| `1|<LobbyName>` | JOIN_ROOM | `{name: string}` | global ||
|n| `1|<LobbyName>` | JOIN_CODE | `{code: string}` | global ||

## From Server

| implemented | scope | type | payload | extra |
|---|---|--- |--- |--- |
|y| init | REGISTERED | `account` | |
|y| init | LOGGED_IN | `null` | |
|y| `0` or `1` | LOBBY_LIST | `array<{name: string, n_clients: int, n_rooms: int}>` | |
|y| `0` or `1` | LOBBY_INFO | `{name: string, n_clients: int, n_rooms: int, rooms: {name: string, player_limit: int, n_teams: int}[]}` | |
|n| all | PLAYER_JOINED | `{username: string, uid: string}` | |
|n| all | PLAYER_LEFT | `{username: string, uid: string}` | |
|n| all | PLAYER_LIST | `{players: {username: string, uid: string}[]}` | |
|y| `1|<LobbyName>` | CREATED_ROOM | `{name: string, player_limit: int, player_count: int, n_teams: int, is_public: bool, join_code: str}` | |
|y| `1|<LobbyName>` | NEW_ROOM | `{name: string, player_limit: int, player_count: int}` | |

## User Broadcast

*Note:* all broadcasted messages come with extra properties: `{ts: float, from: User}`.
These are added by the server.

`User` is `{username: string, uid: string}`

| type | payload | visibility |
|--- |--- |--- |
| SEND_CHAT | `{content: string}` | none, global, team, map |

## InRoom

|n| `2|<RoomName>` | JOIN_TEAM | `{team_n: int}` | global ||
|n| `2|<RoomName>` | MARK_READY | `{ready: bool}` | global ||
|n| `2|<RoomName>` | ADD_ADMIN | `{uid: string}` | global | admin only |
|n| `2|<RoomName>` | RM_ADMIN | `{uid: string}` | global | admin only |
|n| `2|<RoomName>` | ADD_MOD | `{uid: string}` | global | admin only |
|n| `2|<RoomName>` | RM_MOD | `{uid: string}` | global | admin only |
|n| `2|<RoomName>` | KICK_PLAYER | `{}` | global | admin/mod only |

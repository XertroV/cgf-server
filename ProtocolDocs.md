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
|y| init | REJOIN_INTENT | `{lobby: string}` | none | optional msg that will avoid rejoining the client to a different lobby or a room/game hosted in a different lobby. must be sent before `LOGIN` |
|y| init | REGISTER | `{username: string, wsid: string}` | none ||
|y| init | LOGIN | `account` | none ||
|y| init | LOGIN_TOKEN | `{t: string}` | none | from openplanet `Auth::` functionality. |
|y| `0|MainLobby` | CREATE_LOBBY | `{name: string}` | global | used by developers to create a game lobby. note: lobbies that are not whitelisted may be deleted after 1hr. |
|y| `0|MainLobby` | JOIN_LOBBY | `{name: string}` | global | join a game lobby |
|y| `0|MainLobby` | LIST_LOBBIES | `` | none | request a list of known lobbies |
|y| `0`,`1` | LEAVE | `` | global | leave a game lobby, will end the connection if the user is in the main lobby |
|y| `1|<LobbyName>` | CREATE_ROOM | `{name: string, player_limit: int, n_teams: int, maps_required: int, min_secs: int, max_secs: int, max_difficulty: int, map_pack?: int, game_opts: dict, use_club_room: bool}` | none, global | | visibility corresponds to private/public room. public rooms are listed and can be joined by anyone. |
|y| `1|<LobbyName>` | JOIN_ROOM | `{name: string}` | global ||
|y| `1|<LobbyName>` | JOIN_CODE | `{code: string}` | global ||

## From Server

| implemented | scope | type | payload | extra |
|---|---|--- |--- |--- |
|y| init | REGISTERED | `account` | |
|y| init | LOGGED_IN | `null` | |
|y| `0` or `1` | LOBBY_LIST | `array<{name: string, n_clients: int, n_rooms: int}>` | |
|y| `0` or `1` | LOBBY_INFO | `{name: string, n_clients: int, n_rooms: int, rooms: {name: string, player_limit: int, n_teams: int}[]}` | |
|y| all | PLAYER_JOINED | `{username: string, uid: string}` | |
|y| all | PLAYER_LEFT | `{username: string, uid: string}` | |
|y| all | PLAYER_LIST | `{players: {username: string, uid: string}[]}` | |
|y| `1|<LobbyName>` | NEW_ROOM | `Room: {name: string, player_limit, n_teams, is_public, is_open, n_maps, min_secs, max_secs, game_start_time, n_players: int, ready_count: int}` | |
|y| `1|<LobbyName>` | ROOM_INFO | `Room & {join_code: string}` | |
|y| `1|<LobbyName>` | ROOM_UPDATE | `{name: string, n_players: int}` | |
|y| `1|<LobbyName>` | ROOM_RETIRED | `{name: string}` | |

## User Broadcast

*Note:* all broadcasted messages come with extra properties: `{ts: float, from: User}`.
These are added by the server.

`User` is `{username: string, uid: string}`

| type | payload | visibility |
|--- |--- |--- |
| SEND_CHAT | `{content: string}` | none, global, team, map |

## InRoom

|y| `2|<RoomName>` | LEAVE | `{}` | global | leave a room and return to the game lobby |
|y| `2|<RoomName>` | JOIN_TEAM | `{team_n: int}` | global ||
|y| `2|<RoomName>` | MARK_READY | `{ready: bool}` | global ||
|y| `2|<RoomName>` | JOIN_GAME_NOW | `{}` | global | client sends after game has started |

|t| `2|<RoomName>` | ADD_ADMIN | `{uid: string}` | global | admin only |
|t| `2|<RoomName>` | RM_ADMIN | `{uid: string}` | global | admin only |
|t| `2|<RoomName>` | ADD_MOD | `{uid: string}` | global | admin only |
|t| `2|<RoomName>` | RM_MOD | `{uid: string}` | global | admin only |
|t| `2|<RoomName>` | KICK_PLAYER | `{uid: string}` | global | admin/mod only |
|y| `2|<RoomName>` | FORCE_START | `{}` | global | admin/mod only |
|y| `2|<RoomName>` | UPDATE_GAME_OPTS | `{game_opts: dict}` | global | admin/mod only |


PLAYER_JOINED, etc

|y| 2 | LIST_TEAMS | `{teams: str[][]}` | note: list of user UIDs |
|y| 2 | LIST_READY_STATUS | `{uids: str[], ready: bool[]}` | note: list of user UIDs |
<!-- |y| 2 | LIST_PLAYERS | `{players: str[]}` | players in this room / lobby. note: list of user UIDs | -->
|y| 2 | ADMIN_MOD_STATUS | `{admins: str[], mods: str[]}` | note: lists of user UIDs |
|y| 2 | PLAYER_JOINED_TEAM | `{uid: str, team: int}` |
|y| 2 | PLAYER_READY | `{uid: str, is_ready: bool, ready_count: int}` | `ready_count` is the total number of players that are ready |
|y| 2 | GAME_STARTING_AT | `{start_time: float, wait_time: float}` |
|y| 2 | GAME_START_ABORT | `{}` |

|y| 2 | PREPARATION_STATUS | `{msg: str, error?: bool}` |
|y| 2 | MAP_LOAD_ERROR | `{msg: str, status_code: int}` |
|y| 2 | MAPS_LOADED | `{}` | status msg that the server has selected the maps required |
|y| 2 | MAPS_PRELOAD | `{maps: int[]}` | trackIds to preload (optional) |

|y| 2 | SERVER_JOIN_LINK | `{join_link: str}` | the join link to connect to when the game starts |
|y| 2 | ENSURE_MAPS_NADEO | `{map_tids_uids: array<[int, str]>}` | the client should download the maps (based on TrackID) and upload them to nadeo services (based on UID) |


## IN GAME



### from server

|y| 3 | ADMIN_MOD_STATUS | `{admins: str[], mods: str[]}` | note: lists of user UIDs |

|y| 3 | GAME_INFO_FULL | `{players: User[], n_game_msgs: uint, teams: string[][], team_order: int[], map_list: int[], room: string, lobby: string}` ||
|n| 3 | GAME_INFO | `{}` ||
|y| 3 | GAME_REPLAY_START | `{n_msgs: int}` | on rejoin, this is sent immediately before events are replayed |
|y| 3 | GAME_REPLAY_END | `{}` | on rejoin, this is sent immediately once events have been replayed |

|n| 3 | MAPS_INFO_FULL | `{maps: Map[]}` | `Map` is according to TMX schema |

|n| 3 | MAP_LIST_UPDATE | `{}` | if something in the map list is updated e.g., due to reroll |
|n| 3 | MAP_REROLL_VOTE_RESULT | `{}` | result of a vote to reroll a map |
|n| 3 | MAP_REROLL_VOTE_INITIATED | `{}` | sent when a vote to reroll starts |

|n| 3 | G_xxxxxxxx | `any & {payload: {seq: int}}` | broadcasted game messages from other clients; can be scoped via visibility. seq is the sequence number. |

|n| 3 | GM_xxxxxxxx | `any & {payload: {seq: int}}` | a message from the 'game master' (server). this is entered into the game log. |
|n| 3 | GM_PLAYER_LEFT | `{seq: int}` | a message from the 'game master' (server). this is entered into the game log. |

### to server

|n| 3 | G_xxxxxxxx | any | game messages -- they will be broadcast to clients and should be handled by the game engine. global -> all players. team -> players on that team. map -> players on that map. |

|n| 3 | ENTER_MAP | `{TrackID: int}` | sent on entering a map |
|n| 3 | LEAVE_MAP | `{}` | sent on leaving a map |
|n| 3 | CP_TIME | `{TrackID: int, cp: int, time: int}` | sent on getting a checkpoint |
|n| 3 | FINAL_TIME | `{TrackID: int, cp: int, time: int}` | sent on finished race |

|n| 3 | MAP_REROLL_VOTE_START | `{}` ||
|n| 3 | MAP_REROLL_VOTE_SUBMIT | `{}` ||
|n| 3 | MOD_MAP_REROLL | `{}` | admin/mod only |


# todo game:

-


# todo general:

- on game instance start: save players and check on rejoining
- room/lobby updates -- cache?
- raffle room



- random map cache (done)
- room name collision, search: `will throw if name collision`
    - add UID and use those instead?
    - add `##938475` after room name and hide it when rendering?

# Protocol / message docs

## Special Msgs

| type | description | extra |
|--- |--- |--- |
| PING | client sends to server every 5s ||
| END | client sends to server on disconnect, or vice versa ||
| "server" | server status info, sent to client regularly | `{"server": {"version": string, "nbClients": int}}` |
| "scope" | server updates client on currently active scope (MainLobby, InGameLobby, InRoom, InGame) | `{"scope": string}` |

## To Server

| scope | type | payload | visibility req | notes |
|--- |--- |--- |--- |--- |
| init | REGISTER | `{username: string, wsid: string}` | none ||
| init | LOGIN | `account` | none ||
| `0|MainLobby` | CREATE_LOBBY | `{name: string}` | global | used by developers to create a game lobby. note: lobbies that are not whitelisted may be deleted after 1hr. |
| `0|MainLobby` | JOIN_LOBBY | `{name: string}` | global | join a game lobby |
| `0|MainLobby` | LIST_LOBBIES | `` | none | request a list of known lobbies |
| `0|<LobbyName>` | LEAVE_LOBBY | `` | global | leave a game lobby |
| `1|<LobbyName>` | CREATE_ROOM | `{}` | none, global | visibility corresponds to private/public room. public rooms are listed and can be joined by anyone. |
| `1|<LobbyName>` | JOIN_ROOM | `{joinCode: string}` | global ||

## From Server

| type | payload | extra |
|--- |--- |--- |
| REGISTERED | `account` | |
| LOGGED_IN | `null` | |
| LOBBY_LIST | `array<{name: string, nbClients: int, nbRooms: int}>` | |
| ENTERED_LOBBY | `{name: string, nbClients: int, nbRooms: int}` | |

## User Broadcast

*Note:* all broadcasted messages come with extra properties: `{ts: float, from: User}`.
These are added by the server.

| type | payload | visibility |
|--- |--- |--- |
| SEND_CHAT | `{content: string}` | none, global, team, map |

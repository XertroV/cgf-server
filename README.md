## dev:

`find . | entr -r poetry run python main.py`

## notes:

- very simple architecture

- data:
  - room
    - owner
    - moderators[]
    - teams[]
      - players[]
    - gameLog[]
      - {type: string, payload: json, visibility: "team" | "global", from: player}

- players join a room and issue update objects
- those are broadcast to other players in the room based on visibility
- on reconnect, just replay the gameLog
- chat


- user connects
- A. register immediately
  - u: login name str, etc
  - s: id, secret
  - u: authenticate w/ ID and secret
- B. login
  - u: authenticate w/ ID and secret

user state: lobby
- global chat?

options:
- join room
- create room
  - provides join code

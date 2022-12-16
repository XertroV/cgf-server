## dev:

`find . | entr -r poetry run python main.py`

todo:
- rejoin will interfere with a player changing game (same account, diff game).
  - mb introduce a 'game code' on login that determines if we rejoin or not.

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



done:

- user registration
- mongodb backing infra

todo:

- handoff flow:
  - lobby -> game lobby
  - game lobby -> room
  - room -> game(s)
  - game -> map(s)
  - (and inverse on completion)


- server functions:
  - role die/dice
  - find random maps within params


- basic game: tic tac toe
  - take turns placing X or O
  - first move & middle decided by first race; first to finish or get selected medal
  - then other player goes, alternating
  - each round player can
    - place in blank square for free
    - fight for a prior square, re-race that map, winner takes it
      - possible handicap for challenging player?
  - first to 3 in a row wins




- future features
  - stream position data (not recorded in DB)
  - game results -> ranking for players of that game (ELO?)

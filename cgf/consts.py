import toml

SERVER_VERSION = toml.load("./pyproject.toml")['tool']['poetry']['version']

MAX_PLAYERS = 64
MIN_PLAYERS = 2

MAX_TEAMS = 16
MIN_TEAMS = 2

MIN_MAPS = 1
MAX_MAPS = 100

MIN_SECS = 15
MAX_SECS = 600

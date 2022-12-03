import os
import threading
import toml

SERVER_VERSION = toml.load("./pyproject.toml")['tool']['poetry']['version']

# set to True on shutdown of server.
# this is not really const, but easy to put here
SHUTDOWN = False
SHUTDOWN_EVT = threading.Event()

LOCAL_DEV_MODE = os.environ.get('CFG_LOCAL_DEV', '').lower().strip() == 'true'

MAX_PLAYERS = 64
MIN_PLAYERS = 2

MAX_TEAMS = 16
MIN_TEAMS = 2

MIN_MAPS = 1
MAX_MAPS = 100

MIN_SECS = 15
MAX_SECS = 600

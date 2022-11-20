import toml

SERVER_VERSION = toml.load("./pyproject.toml")['tool']['poetry']['version']

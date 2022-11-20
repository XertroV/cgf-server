
import os
import hashlib
from logging import info
import time

from cfg.User import User


all_users: dict[str, User] = dict()

# class ClientSecrets:
#     user_to_secret: dict[str, str]
#     secret_to_user: dict[str, User]

#     def __init__(self):
#         self.user_to_secret = dict()
#         self.secret_to_user = dict()

#     def add(self, user: User):
#         self.user_to_secret[user.id] = user.secret
#         self.secret_to_user[user.secret] = user

#     def get_secret(self, user: User):
#         if user.id not in self.user_to_secret:
#             s = gen_secret()
#             self.user_to_secret[user.id] = s
#             self.secret_to_user[s] = user
#         return self.user_to_secret[user.id]


# client_secrets = ClientSecrets()


def get_user(id: str) -> User:
    return all_users.get(id, None)


def register_user(name: str, wsid: str) -> User:
    id = gen_user_id(name, wsid)
    u = User(id, name, gen_secret())
    all_users[u.id] = u
    # client_secrets.add(u)
    return u


def authenticate_user(id: str, username: str, secret: str) -> User:
    if (id not in all_users):
        info(f"Attempted login ({username}); ID not found: {id}")
        return None
    u = all_users[id]
    if u.name != username or u.id != id or u.secret != secret:
        info(f"Attempted login ({username}); secret mismatch!!")
        return None
    u.n_logins += 1
    info(f"Authenticated user: {u.name} / id:{u.id}")
    return u


def gen_secret() -> str:
    return os.urandom(20).hex()

def gen_user_id(name: str, wsid: str) -> str:
    return sha_256("|".join([name, str(time.time()), wsid]))[:20]

def sha_256(text: str) -> str:
    return hashlib.sha256(text.encode("UTF8")).hexdigest()

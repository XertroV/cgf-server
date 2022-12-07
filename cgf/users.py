
import os
import hashlib
from logging import info
import time

from cgf.User import User
from cgf.op_auth import TokenResp


all_users: dict[str, User] = dict()


def get_user(uid: str) -> User:
    return all_users.get(uid, None)


async def register_authed_user(token: TokenResp) -> User:
    uid = token.account_id
    u = User(uid=uid, name=token.display_name, secret=gen_secret())
    await u.save()
    all_users[u.uid] = u
    return u


async def register_user(name: str, wsid: str) -> User:
    uid = gen_user_uid(name, wsid)
    u = User(uid=uid, name=name, secret=gen_secret())
    await u.save()
    all_users[u.uid] = u
    return u


def authenticate_user(uid: str, username: str, secret: str) -> User:
    if (uid not in all_users):
        info(f"Attempted login ({username}); UID not found: {uid}")
        return None
    u = all_users[uid]
    if u.name != username or u.uid != uid or u.secret != secret:
        info(f"Attempted login ({username}); secret mismatch!!")
        return None
    u.n_logins += 1
    info(f"Authenticated user: {u.name} / uid:{u.uid}")
    return u


def gen_secret() -> str:
    return os.urandom(20).hex()

def gen_user_uid(name: str, wsid: str) -> str:
    return sha_256("|".join([name, str(time.time()), wsid]))[:20]

def gen_uid(length=10) -> str:
    return os.urandom(length).hex()

def sha_256(text: str) -> str:
    return hashlib.sha256(text.encode("UTF8")).hexdigest()

join_code_allowed = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

def gen_join_code() -> str:
    ret = ""
    while len(ret) < 6:
        for c in os.urandom(32):
            if chr(c) in join_code_allowed:
                ret += chr(c)
    return ret[:6]

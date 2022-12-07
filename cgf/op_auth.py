

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Optional

from cgf.http import get_session

op_secret = None
op_url = None

op_lines = Path('.openplanet-auth').read_text().strip().split('\n')
for line in op_lines:
    key, val = line.strip().split("=", 2)
    if key.strip() == "secret":
        op_secret = val.strip()
    elif key.strip() == "url":
        op_url = val.strip()
for n,v in [
        ('secret', op_secret),
        ('url', op_url),
    ]:
    if v is None:
        raise Exception(f'Missing Openplanet Auth config: {n}.')

@dataclass
class TokenResp:
    account_id: str
    display_name: str
    token_time: int


async def check_token(token: str) -> Optional[TokenResp]:
    pl = dict(token=token, secret=op_secret)
    async with get_session() as session:
        async with session.post(op_url, data=pl) as resp:
            if (resp.status != 200):
                logging.warn(f"Checking token failed, status: {resp.status}, body: {await resp.text()}")
                return None
            resp_j = await resp.json()
            if "error" in resp_j:
                logging.warn(f"Error from server for token check, status: {resp_j['error']}")
                return None
            return TokenResp(**resp_j)

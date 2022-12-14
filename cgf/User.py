import asyncio
import logging
import time

import pymongo
from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass

from beanie import Document, Indexed, init_beanie


class User(Document):
    uid: Indexed(str, unique=True)
    name: Indexed(str)
    secret: str
    registration_ts: Indexed(float, unique=True) = Field(default_factory=lambda: time.time())
    n_logins: int = 0
    last_seen: Indexed(float, pymongo.DESCENDING) = 0
    last_scope: str = ""

    class Settings:
        use_state_management = True

    def __hash__(self) -> int:
        return hash(self.secret)

    @property
    def safe_json(self):
        return dict(uid=self.uid, username=self.name, last_seen=self.last_seen)

    @property
    def unsafe_json(self):
        # warning(f"json: {self.json()}")
        d = self.safe_json
        d.update(dict(secret=self.secret))
        return d

    # updated every time the user changes scopes -- facilitate rejoining
    def set_last_scope(self, scope: str):
        self.last_scope = scope
        self.persist()
        logging.info(f"User: {self.name} set scope: {scope}")

    def persist(self):
        asyncio.create_task(self.save_changes())

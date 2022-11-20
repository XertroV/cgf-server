# from dataclasses import field
import time

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass

from beanie import Document, Indexed, init_beanie


# @dataclass
class User(Document):
    uid: Indexed(str, unique=True)
    name: str
    secret: str
    registration_ts: int = Field(default_factory=lambda: int(time.time()))
    n_logins: int = 0
    last_seen: float = 0

    def __hash__(self) -> int:
        return hash(self.secret)

    @property
    def json(self):
        return dict(uid=self.uid, username=self.name)

    @property
    def unsafe_json(self):
        d = self.json
        d.update(dict(secret=self.secret))
        return d

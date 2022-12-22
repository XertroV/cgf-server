import datetime
import json
import logging

from pydantic import Field, BaseModel
from beanie import Document, Indexed

class MapJustID(BaseModel):
    TrackID: int

class RandomMapQueue(Document):
    tracks: list[int]
    name: Indexed(str, unique=True)

import time

import pymongo
from pydantic import Field
from beanie import Indexed


class HasCreationTs:
    creation_ts: Indexed(float, pymongo.DESCENDING) = Field(default_factory=time.time)

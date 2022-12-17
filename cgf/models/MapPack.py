import datetime
import json
import logging

from pydantic import Field, BaseModel
from beanie import Document, Indexed

from cgf.models.Map import tmx_date_to_ts

class MapPack(Document):
    ID: Indexed(int, unique=True)
    UserID: Indexed(int)
    Username: str
    Name: str
    Description: str | None
    TypeName: str
    StyleName: str
    Titlepack: str | None
    EnvironmentName: str | None
    Unreleased: bool
    TrackUnreleased: bool
    Downloadable: bool
    TrackHidden: bool
    Downloads: int
    Created: str
    Edited: str | None
    CreatedTimestamp: float
    EditedTimestamp: float
    TrackCount: int
    TagsString: str
    Tracks: list[int] | None

    def __init__(self, *args, **kwargs):
        kwargs['CreatedTimestamp'] = tmx_date_to_ts(kwargs['Created'])
        kwargs['EditedTimestamp'] = tmx_date_to_ts(kwargs['Edited'])
        super().__init__(*args, **kwargs)

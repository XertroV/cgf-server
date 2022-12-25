import datetime
import json
import logging

from pydantic import Field, BaseModel
from beanie import Document, Indexed


LONG_MAP_SECS = 315

"""Length Enums:
0: Anything
1: 15 seconds
2: 30
3: 45
4: 1min
5: 1:15
6: 1:30
7: 1:45
8: 2:00
9: 2:30
10: 3:00
11: 3:30
12: 4:00
13: 4:30
14: 5:00
15: Longer than 5 min
"""

def length_secs_to_enum(length_secs):
    if length_secs <= 120:
        return round(length_secs / 15)
    if length_secs <= 300:
        return 8 + round((length_secs - 120) / 30)
    return 15


def tmx_date_to_ts(date_str: str):
    # "2020-10-26T20:11:55.657"
    # "2022-02-26T00:29:13"
    frac = "000"
    if len(date_str) > 19 and date_str[19] == ".":
        frac = date_str[20:]
        date_str = date_str[:19]
    elif len(date_str) != 19:
        logging.warning(f"Unknown date format: {date_str} ??")
    return datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S").timestamp() + float(frac)/1000


def difficulty_to_int(d: str) -> int:
    # d = d.lower()
    if d == "Beginner": return 0
    if d == "Intermediate": return 1
    if d == "Advanced": return 2
    if d == "Expert": return 3
    if d == "Lunatic": return 4
    if d == "Impossible": return 5
    raise Exception(f"Unknown difficulty: {d}")

def int_to_difficulty(d: int) -> str:
    if d == 0: return "Beginner"
    if d == 1: return "Intermediate"
    if d == 2: return "Advanced"
    if d == 3: return "Expert"
    if d == 4: return "Lunatic"
    if d == 5: return "Impossible"
    raise Exception(f"Unknown difficulty int: {d}")



class MapJustID(BaseModel):
    TrackID: int


class Map(Document):
    TrackID: Indexed(int, unique=True)
    UserID: Indexed(int)
    Username: Indexed(str)
    AuthorLogin: Indexed(str)
    Name: Indexed(str)
    GbxMapName: Indexed(str)
    TrackUID: str
    TitlePack: Indexed(str)
    ExeVersion: Indexed(str)
    ExeBuild: Indexed(str)
    Mood: str
    ModName: str | None
    AuthorTime: int
    ParserVersion: int
    UploadedAt: str
    UpdatedAt: str
    UploadTimestamp: float
    UpdateTimestamp: float
    Tags: str | None
    TypeName: str
    StyleName: Indexed(str) | None
    RouteName: str
    LengthName: str
    LengthSecs: Indexed(int)
    LengthEnum: int
    DifficultyName: str
    DifficultyInt: int | None
    Laps: int
    Comments: str
    Downloadable: bool
    Unlisted: bool
    Unreleased: bool
    RatingVoteCount: int
    RatingVoteAverage: float
    VehicleName: str
    EnvironmentName: str
    HasScreenshot: bool
    HasThumbnail: bool
    MapType: str | None

    def __init__(self, *args, LengthName="2 m 30 s", LengthSecs=None, **kwargs):
        if LengthSecs is None:
            LengthSecs = 0
            # can be in formats: "Long", "2 m 30 s", "1 min", "2 min", "15 secs"
            if LengthName == "Long":
                LengthSecs = LONG_MAP_SECS
            elif " m " in LengthName:
                _mins, rest = LengthName.split(" m ")
                mins_s = int(_mins) * 60
                secs = int(rest.split(" s")[0])
                LengthSecs = mins_s + secs
            elif " min" in LengthName:
                LengthSecs = 60 * int(LengthName.split(" min")[0])
            elif " secs" in LengthName:
                LengthSecs = int(LengthName.split(" secs")[0])
            else:
                raise Exception(f"Unknown LengthName format; {LengthName}")
        kwargs['LengthEnum'] = length_secs_to_enum(LengthSecs)
        kwargs['UploadTimestamp'] = tmx_date_to_ts(kwargs['UploadedAt'])
        kwargs['UpdateTimestamp'] = tmx_date_to_ts(kwargs['UpdatedAt'])
        kwargs['DifficultyInt'] = difficulty_to_int(kwargs["DifficultyName"])
        super().__init__(*args, LengthSecs=LengthSecs, LengthName=LengthName, **kwargs)

    @property
    def safe_json(self) -> dict:
        return json.loads(self.json())

    @property
    def safe_json_shorter(self):
        d = dict()
        fields = ['TrackID', 'TrackUID', 'Name', 'AuthorTime', 'Tags', 'TypeName', 'StyleName', 'LengthName', 'LengthSecs', 'LengthEnum', 'DifficultyName', 'HasThumbnail']
        for f in fields:
            d[f] = self.__getattribute__(f)
        if self.GbxMapName != "?":
            d['Name'] = self.GbxMapName
        return d

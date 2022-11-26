from pydantic import Field, BaseModel
from beanie import Document, Indexed


LONG_MAP_SECS = 10000


class MapJustID(BaseModel):
    TrackID: int

class Map(Document):
    TrackID: Indexed(int, unique=True)
    Name: str
    GbxMapName: str
    TrackUID: str
    ExeVersion: str
    ExeBuild: str
    AuthorTime: int
    UploadedAt: str
    UpdatedAt: str
    Tags: str = Field(default_factory=str)
    TypeName: str
    StyleName: str | None
    RouteName: str
    LengthName: str
    LengthSecs: int
    DifficultyName: str
    Laps: int
    Comments: str
    Downloadable: bool
    RatingVoteCount: int
    RatingVoteAverage: float

    def __init__(self, *args, LengthName="2 m 30 s", **kwargs):
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
        super().__init__(*args, LengthSecs=LengthSecs, LengthName=LengthName, **kwargs)

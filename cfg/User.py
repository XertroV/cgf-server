from dataclasses import dataclass, field
import time



@dataclass
class User:
    id: str
    name: str
    secret: str
    registration_ts: int = field(default_factory=lambda: int(time.time()))
    n_logins: int = 0
    last_seen: float = 0

    def __hash__(self) -> int:
        return hash(self.secret)

    @property
    def json(self):
        return dict(id=self.id, username=self.name)

    @property
    def unsafe_json(self):
        d = self.json
        d.update(dict(secret=self.secret))
        return d

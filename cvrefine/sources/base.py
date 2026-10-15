"""Common shape for job sources.

Two kinds:
- feed sources return the board's recent jobs regardless of profile; results are cached and
  every profile filters them locally, so each board is hit at most once per `min_interval`.
- search sources run a query per profile (keywords + location).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from cvrefine.models import CriteriaProfile

USER_AGENT = "CVRefineTool/0.1 (personal job search)"


@dataclass
class RawJob:
    source: str
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    description_is_html: bool = False
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    remote: bool | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawJob":
        data = dict(data)
        data["posted_at"] = datetime.fromisoformat(data["posted_at"]) if data.get("posted_at") else None
        return cls(**data)


class FeedSource(Protocol):
    name: str
    label: str
    min_interval: timedelta
    attribution: str

    def fetch(self) -> list[RawJob]: ...


class SearchSource(Protocol):
    name: str
    label: str
    attribution: str
    warning: str

    def search(self, profile: CriteriaProfile, max_age_days: int) -> list[RawJob]: ...


class SourceError(RuntimeError):
    pass

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

WORK_MODES = ("any", "remote", "hybrid", "onsite")
# Lowest factor a job in the wrong work mode is scaled to; None = work mode does not affect the score.
WORK_MODE_STRICTNESS = {"strict": 0.2, "soft": 0.6, "off": None}
JOB_STATUSES = ("new", "shortlisted", "dismissed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""


class CriteriaProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    keywords: str = ""
    work_mode: str = "any"
    work_mode_strictness: str = "strict"  # key of WORK_MODE_STRICTNESS
    locations: str = ""
    seniority: str = ""
    must_have: str = ""
    nice_to_have: str = ""
    min_salary: int | None = None
    excluded_companies: str = ""
    # One statement per line that should be true of a good job, e.g. "No security clearance required".
    custom_criteria: str = ""
    # Comma-separated source names from cvrefine.sources.registry.
    sources: str = "remoteok,remotive,arbeitnow,weworkremotely,indeed"
    # jobspy country for Indeed/Glassdoor, e.g. "italy", "germany", "uk".
    country: str = "italy"
    scan_interval_hours: int = 0
    max_age_days: int = 14
    last_scan_at: datetime | None = None

    def custom_list(self) -> list[str]:
        return [line.strip() for line in self.custom_criteria.splitlines() if line.strip()]

    def source_list(self) -> list[str]:
        return [name.strip() for name in self.sources.split(",") if name.strip()]

    def keyword_list(self) -> list[str]:
        return [keyword.strip() for keyword in self.keywords.split(",") if keyword.strip()]


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    profile_id: int | None = Field(default=None, foreign_key="criteriaprofile.id")
    source: str = "manual"
    url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    status: str = "new"
    score: float | None = None
    uncertain: bool = False
    breakdown: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    score_error: str = ""
    # User label for threshold tuning: 1 good match, -1 bad match.
    feedback: int | None = None
    created_at: datetime = Field(default_factory=_now)
    external_id: str = Field(default="", index=True)
    dedupe_key: str = Field(default="", index=True)
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    # Free-text group for tailoring one CV to several similar jobs, e.g. "AWS platform".
    area: str = Field(default="", index=True)


class ScanRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="criteriaprofile.id")
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    # Per source: fetched, matched, new, duplicates, filtered, error.
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    scored: int = 0
    score_errors: int = 0
    error: str = ""


class FeedCache(SQLModel, table=True):
    source: str = Field(primary_key=True)
    fetched_at: datetime
    jobs: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class Tailoring(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    # Set when tailoring to a group of jobs; job_id then points at the group's best-scored job.
    area: str = ""
    requirements: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    keywords: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Each edit: block_id, label, original, proposed, rationale, keywords, grounded, status, final.
    edits: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    dropped: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    grounding_error: str = ""
    output_dir: str = ""
    render_error: str = ""
    created_at: datetime = Field(default_factory=_now)
    applied_at: datetime | None = None


class CoverLetter(SQLModel, table=True):
    """Optional, written only on request for one job."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    # Applied tailoring whose CV the letter was written from; None = master CV.
    tailoring_id: int | None = Field(default=None, foreign_key="tailoring.id")
    language: str = "en"
    notes: str = ""
    requirements: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Each paragraph: purpose, text, requirements, grounded, status (kept | dropped), final.
    paragraphs: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    grounding_error: str = ""
    output_path: str = ""
    created_at: datetime = Field(default_factory=_now)
    saved_at: datetime | None = None

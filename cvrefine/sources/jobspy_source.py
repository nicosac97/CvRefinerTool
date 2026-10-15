"""Indeed, Glassdoor and LinkedIn through python-jobspy (guest search, no login).

Scraping these sites goes against their terms of service and they block aggressively, so:
low result counts, one site at a time, one query per keyword, LinkedIn opt-in per profile.
"""

import logging
import math
from datetime import datetime, timezone

from cvrefine.models import CriteriaProfile
from cvrefine.sources.base import RawJob, SourceError

RESULTS_PER_QUERY = 25
MAX_KEYWORDS = 3


def _clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _to_datetime(value) -> datetime | None:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:  # pandas gives datetime.date for date_posted
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    except AttributeError:
        return None


class _ErrorCollector(logging.Handler):
    """jobspy logs blocks (403, 429) and returns no rows instead of raising; collect them to report."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class JobSpySite:
    attribution = ""

    def __init__(self, site: str, label: str, warning: str = "") -> None:
        self.name = site
        self.label = label
        self.site = site
        self.warning = warning

    def search(self, profile: CriteriaProfile, max_age_days: int) -> list[RawJob]:
        from jobspy import scrape_jobs

        keywords = [keyword.strip() for keyword in profile.keywords.split(",") if keyword.strip()][:MAX_KEYWORDS]
        if not keywords:
            raise SourceError("profile has no search keywords")
        location = profile.locations.split(",")[0].strip() or None
        jobs: dict[str, RawJob] = {}
        collector = _ErrorCollector()
        logger = logging.getLogger(f"JobSpy:{self.label}")
        logger.addHandler(collector)
        try:
            self._search_keywords(scrape_jobs, keywords, location, profile, max_age_days, jobs)
        finally:
            logger.removeHandler(collector)
        if not jobs and collector.messages:
            raise SourceError(f"{self.label}: {collector.messages[-1]}")
        return list(jobs.values())

    def _search_keywords(self, scrape_jobs, keywords, location, profile, max_age_days, jobs: dict[str, RawJob]) -> None:
        for keyword in keywords:
            try:
                frame = scrape_jobs(
                    site_name=[self.site],
                    search_term=keyword,
                    location=location,
                    is_remote=profile.work_mode == "remote",
                    results_wanted=RESULTS_PER_QUERY,
                    hours_old=max_age_days * 24,
                    country_indeed=profile.country or "italy",
                    linkedin_fetch_description=True,
                    description_format="markdown",
                )
            except Exception as error:  # jobspy raises many types, including from inside threads
                raise SourceError(f"{self.label}: {type(error).__name__}: {error}") from error
            for row in frame.to_dict("records") if not frame.empty else []:
                url = _clean(row.get("job_url")) or ""
                external_id = str(_clean(row.get("id")) or url)
                if not external_id or external_id in jobs:
                    continue
                jobs[external_id] = RawJob(
                    source=self.name,
                    external_id=external_id,
                    url=url,
                    title=_clean(row.get("title")) or "",
                    company=_clean(row.get("company")) or "",
                    location=_clean(row.get("location")) or "",
                    description=_clean(row.get("description")) or "",
                    posted_at=_to_datetime(row.get("date_posted")),
                    salary_min=_clean(row.get("min_amount")) if _clean(row.get("interval")) in (None, "yearly") else None,
                    salary_max=_clean(row.get("max_amount")) if _clean(row.get("interval")) in (None, "yearly") else None,
                    remote=_clean(row.get("is_remote")),
                )

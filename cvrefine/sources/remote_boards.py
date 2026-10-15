"""Remote job boards with public JSON APIs or RSS. Each fetch pulls the board's recent jobs once."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from cvrefine.sources.base import USER_AGENT, RawJob, SourceError

TIMEOUT = 30


def _get(url: str, **params) -> httpx.Response:
    try:
        response = httpx.get(url, params=params or None, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response
    except httpx.HTTPError as error:
        raise SourceError(f"{url}: {error}") from error


def _from_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _from_iso(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RemoteOK:
    name = "remoteok"
    label = "Remote OK"
    min_interval = timedelta(hours=3)
    attribution = "Jobs from Remote OK (remoteok.com)"

    def fetch(self) -> list[RawJob]:
        data = _get("https://remoteok.com/api").json()
        jobs = []
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue  # first element is the legal notice
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=str(item["id"]),
                    url=item.get("url") or item.get("apply_url") or "",
                    title=item.get("position", ""),
                    company=item.get("company", ""),
                    location=item.get("location") or "Remote",
                    description=item.get("description", ""),
                    description_is_html=True,
                    posted_at=_from_epoch(item.get("epoch")),
                    salary_min=item.get("salary_min") or None,
                    salary_max=item.get("salary_max") or None,
                    remote=True,
                    tags=[str(tag) for tag in item.get("tags") or []],
                )
            )
        return jobs


class Remotive:
    name = "remotive"
    label = "Remotive"
    # Remotive asks for at most 4 requests a day.
    min_interval = timedelta(hours=6)
    attribution = "Jobs from Remotive (remotive.com)"

    def fetch(self) -> list[RawJob]:
        data = _get("https://remotive.com/api/remote-jobs").json()
        return [
            RawJob(
                source=self.name,
                external_id=str(item["id"]),
                url=item.get("url", ""),
                title=item.get("title", ""),
                company=(item.get("company_name") or "").strip(),
                location=item.get("candidate_required_location") or "Remote",
                description=item.get("description", ""),
                description_is_html=True,
                posted_at=_from_iso(item.get("publication_date")),
                remote=True,
                tags=[str(tag) for tag in item.get("tags") or []] + [item.get("category") or ""],
            )
            for item in data.get("jobs", [])
        ]


class Arbeitnow:
    name = "arbeitnow"
    label = "Arbeitnow (Europe)"
    min_interval = timedelta(hours=3)
    attribution = "Jobs from Arbeitnow (arbeitnow.com)"
    pages = 3

    def fetch(self) -> list[RawJob]:
        jobs = []
        for page in range(1, self.pages + 1):
            data = _get("https://www.arbeitnow.com/api/job-board-api", page=page).json()
            for item in data.get("data", []):
                jobs.append(
                    RawJob(
                        source=self.name,
                        external_id=item["slug"],
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        company=item.get("company_name", ""),
                        location=item.get("location", ""),
                        description=item.get("description", ""),
                        description_is_html=True,
                        posted_at=_from_epoch(item.get("created_at")),
                        remote=bool(item.get("remote")),
                        tags=[str(tag) for tag in item.get("tags") or []],
                    )
                )
            if not (data.get("links") or {}).get("next"):
                break
        return jobs


class WeWorkRemotely:
    name = "weworkremotely"
    label = "We Work Remotely"
    min_interval = timedelta(hours=3)
    attribution = "Jobs from We Work Remotely (weworkremotely.com)"
    categories = ("remote-devops-sysadmin-jobs", "remote-back-end-programming-jobs", "remote-full-stack-programming-jobs")

    def fetch(self) -> list[RawJob]:
        jobs = []
        for category in self.categories:
            root = ElementTree.fromstring(_get(f"https://weworkremotely.com/categories/{category}.rss").content)
            for item in root.iter("item"):
                text = {child.tag: (child.text or "").strip() for child in item}
                company, _, title = text.get("title", "").partition(": ")
                if not title:
                    company, title = "", company
                try:
                    posted = parsedate_to_datetime(text["pubDate"]) if text.get("pubDate") else None
                except (TypeError, ValueError):
                    posted = None
                jobs.append(
                    RawJob(
                        source=self.name,
                        external_id=text.get("guid") or text.get("link", ""),
                        url=text.get("link", ""),
                        title=title,
                        company=company,
                        location=text.get("region") or "Remote",
                        description=text.get("description", ""),
                        description_is_html=True,
                        posted_at=posted,
                        remote=True,
                        tags=[tag for tag in (text.get("category"), text.get("skills")) if tag],
                    )
                )
        return jobs

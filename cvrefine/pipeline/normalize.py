"""RawJob -> Job conversion, text cleanup, keyword matching and dedupe keys."""

import re
from datetime import datetime, timezone

from lxml import etree
from lxml import html as lxml_html

from cvrefine.models import CriteriaProfile, Job
from cvrefine.sources.base import RawJob

_BLOCK_TAGS = ("p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section")
_GENDER_TAG_RE = re.compile(r"\((?:[mwfdx]\s*/\s*){1,3}[mwfdx]\)|\b[mwfdx]/[mwfdx]/[mwfdx]\b", re.IGNORECASE)
_COMPANY_SUFFIX_RE = re.compile(r"\b(gmbh|inc|ltd|limited|llc|srl|s\.r\.l|spa|s\.p\.a|ag|se|bv|b\.v|corp|corporation|co|plc|sa|sas|oy|ab)\b\.?")
_NON_WORD_RE = re.compile(r"[^a-z0-9+#]+")


def html_to_text(markup: str) -> str:
    if "<" not in markup:
        return markup.strip()
    try:
        root = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError):
        return markup.strip()
    for element in root.iter(*_BLOCK_TAGS):
        if element.tag == "li":
            element.text = "- " + (element.text or "")
        element.tail = "\n" + (element.tail or "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in root.text_content().splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def normalize_title(title: str) -> str:
    title = _GENDER_TAG_RE.sub(" ", title.lower())
    return _NON_WORD_RE.sub(" ", title).strip()


def normalize_company(company: str) -> str:
    company = _COMPANY_SUFFIX_RE.sub(" ", company.lower())
    return _NON_WORD_RE.sub(" ", company).strip()


def dedupe_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def matches_keywords(raw: RawJob, keywords: list[str]) -> bool:
    """Keyword in the title or tags. Descriptions are too noisy ("cloud" appears everywhere)."""
    if not keywords:
        return True
    haystack = " " + normalize_title(raw.title + " " + " ".join(raw.tags)) + " "
    return any(" " + normalize_title(keyword) + " " in haystack for keyword in keywords)


def to_job(raw: RawJob, profile: CriteriaProfile) -> Job:
    return Job(
        profile_id=profile.id,
        source=raw.source,
        external_id=raw.external_id,
        url=raw.url,
        title=raw.title.strip() or "Untitled job",
        company=raw.company.strip(),
        location=raw.location.strip(),
        description=html_to_text(raw.description) if raw.description_is_html else raw.description.strip(),
        posted_at=raw.posted_at,
        salary_min=raw.salary_min,
        salary_max=raw.salary_max,
        dedupe_key=dedupe_key(raw.company, raw.title),
    )


def hard_filter_reason(raw: RawJob, profile: CriteriaProfile, now: datetime | None = None) -> str | None:
    """Deterministic rejects that need no model. Returns the reason, or None to keep the job."""
    now = now or datetime.now(timezone.utc)
    company = raw.company.lower()
    for excluded in (name.strip().lower() for name in profile.excluded_companies.split(",")):
        if excluded and excluded in company:
            return "excluded company"
    if raw.posted_at and profile.max_age_days and (now - raw.posted_at).days > profile.max_age_days:
        return "too old"
    if profile.min_salary and raw.salary_max and raw.salary_max < profile.min_salary:
        return "salary below minimum"
    return None

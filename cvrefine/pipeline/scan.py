"""Scan a criteria profile: fetch sources, filter, dedupe, store and score new jobs."""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from rapidfuzz import fuzz
from sqlmodel import Session, select

from cvrefine.db import CANDIDATE_KEY, get_setting
from cvrefine.llm.decisions import DecisionClient, DecisionError, NoulResult, get_decision_client, noul
from cvrefine.models import CriteriaProfile, FeedCache, Job, ScanRun
from cvrefine.pipeline.normalize import hard_filter_reason, matches_keywords, normalize_company, to_job
from cvrefine.pipeline.scoring import score_job
from cvrefine.sources.base import RawJob, SourceError
from cvrefine.sources.registry import FEEDS, SEARCHES

log = logging.getLogger(__name__)

MAX_NEW_PER_SCAN = 150
SCORING_WORKERS = 4
SAME_TITLE = 92  # rapidfuzz token_set_ratio: at or above, same company + title is a duplicate
MAYBE_SAME_TITLE = 75  # between this and SAME_TITLE, ask the decision model
SAME_POSITION_P = 0.7
DEDUPE_DESCRIPTION_CHARS = 4_000


def _utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; everything stored is UTC.
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def get_feed(session: Session, name: str, now: datetime) -> list[RawJob]:
    """Cached feed results; the board is only contacted once its min_interval has passed."""
    source = FEEDS[name]
    cache = session.get(FeedCache, name)
    if cache and now - _utc(cache.fetched_at) < source.min_interval:
        return [RawJob.from_dict(item) for item in cache.jobs]
    jobs = source.fetch()
    session.merge(FeedCache(source=name, fetched_at=now, jobs=[job.to_dict() for job in jobs]))
    session.commit()
    return jobs


class Deduper:
    def __init__(self, session: Session, decisions: DecisionClient | None) -> None:
        self.decisions = decisions
        self.seen_ids: set[tuple[str, str]] = set()
        self.keys: set[str] = set()
        self.by_company: dict[str, list[tuple[str, str]]] = {}
        for source, external_id, key, description in session.exec(select(Job.source, Job.external_id, Job.dedupe_key, Job.description)):
            if external_id:
                self.seen_ids.add((source, external_id))
            self.add_key(key, description)

    def add_key(self, key: str, description: str) -> None:
        if not key:
            return
        self.keys.add(key)
        company, _, title = key.partition("|")
        if company:
            self.by_company.setdefault(company, []).append((title, description))

    def is_seen(self, raw: RawJob) -> bool:
        return (raw.source, raw.external_id) in self.seen_ids

    def is_duplicate(self, job: Job) -> bool:
        if job.dedupe_key in self.keys:
            return True
        company, _, title = job.dedupe_key.partition("|")
        for other_title, other_description in self.by_company.get(company, []) if company else []:
            similarity = fuzz.token_set_ratio(title, other_title)
            if similarity >= SAME_TITLE:
                return True
            if similarity >= MAYBE_SAME_TITLE and self._same_position(job.description, other_description):
                return True
        return False

    def _same_position(self, first: str, second: str) -> bool:
        if self.decisions is None:
            return False
        state = f"POSTING A:\n{first[:DEDUPE_DESCRIPTION_CHARS]}\n\nPOSTING B:\n{second[:DEDUPE_DESCRIPTION_CHARS]}"
        try:
            answer = self.decisions.decide(state, {"same": noul("Are posting A and posting B the same open position at the same company?")})["same"]
        except (DecisionError, KeyError):
            return False
        return isinstance(answer, NoulResult) and answer.probability >= SAME_POSITION_P


def _collect(session: Session, profile: CriteriaProfile, now: datetime, stats: dict) -> list[RawJob]:
    raws: list[RawJob] = []
    keywords = profile.keyword_list()
    for name in profile.source_list():
        entry = stats.setdefault(name, {"fetched": 0, "matched": 0, "new": 0, "duplicates": 0, "filtered": 0, "error": ""})
        try:
            if name in FEEDS:
                fetched = get_feed(session, name, now)
                matched = [raw for raw in fetched if matches_keywords(raw, keywords)]
            elif name in SEARCHES:
                fetched = matched = SEARCHES[name].search(profile, profile.max_age_days or 14)
            else:
                entry["error"] = "unknown source"
                continue
        except SourceError as error:
            entry["error"] = str(error)[:500]
            continue
        except Exception as error:  # one broken source must never fail the scan
            log.exception("source %s failed", name)
            entry["error"] = f"{type(error).__name__}: {error}"[:500]
            continue
        entry["fetched"], entry["matched"] = len(fetched), len(matched)
        raws.extend(matched)
    return raws


def run_scan(
    profile_id: int,
    session_factory: Callable[[], Session],
    decisions_factory: Callable[[], DecisionClient] = get_decision_client,
    now: datetime | None = None,
) -> int:
    """Run one scan synchronously and return the ScanRun id."""
    now = now or datetime.now(timezone.utc)
    with session_factory() as session:
        stored_profile = session.get(CriteriaProfile, profile_id)
        if stored_profile is None:
            raise ValueError(f"profile {profile_id} not found")
        # Detached copy: commits below expire ORM attributes, and scoring threads must not trigger reloads.
        profile = CriteriaProfile(**stored_profile.model_dump())
        run = ScanRun(profile_id=profile_id, started_at=now)
        session.add(run)
        session.commit()
        stats: dict = {}
        try:
            decisions = decisions_factory()
        except DecisionError as error:
            decisions, run.error = None, f"Scoring disabled: {error}"

        raws = _collect(session, profile, now, stats)
        raws.sort(key=lambda raw: raw.posted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        deduper = Deduper(session, decisions)
        new_jobs: list[Job] = []
        for raw in raws:
            entry = stats[raw.source]
            if deduper.is_seen(raw):
                continue
            deduper.seen_ids.add((raw.source, raw.external_id))
            if hard_filter_reason(raw, profile, now):
                entry["filtered"] += 1
                continue
            job = to_job(raw, profile)
            if deduper.is_duplicate(job):
                entry["duplicates"] += 1
                continue
            deduper.add_key(job.dedupe_key, job.description)
            if len(new_jobs) >= MAX_NEW_PER_SCAN:
                entry["filtered"] += 1
                continue
            entry["new"] += 1
            new_jobs.append(job)

        if decisions is not None and new_jobs:
            candidate = get_setting(session, CANDIDATE_KEY)

            def score(job: Job) -> bool:
                try:
                    score_job(decisions, candidate, profile, job)
                    return True
                except DecisionError as error:
                    job.score_error = str(error)
                    return False

            with ThreadPoolExecutor(SCORING_WORKERS) as pool:
                results = list(pool.map(score, new_jobs))
            run.scored, run.score_errors = sum(results), results.count(False)

        session.add_all(new_jobs)
        run.stats = stats
        run.finished_at = datetime.now(timezone.utc)
        stored_profile.last_scan_at = now
        session.add_all([run, stored_profile])
        session.commit()
        return run.id


class ScanManager:
    """Runs scans in background threads, at most one per profile at a time."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def is_running(self, profile_id: int) -> bool:
        return profile_id in self._running

    def start(self, profile_id: int) -> bool:
        with self._lock:
            if profile_id in self._running:
                return False
            self._running.add(profile_id)
        threading.Thread(target=self._run, args=(profile_id,), daemon=True, name=f"scan-{profile_id}").start()
        return True

    def _run(self, profile_id: int) -> None:
        try:
            run_scan(profile_id, self.session_factory)
        except Exception:
            log.exception("scan of profile %s failed", profile_id)
        finally:
            with self._lock:
                self._running.discard(profile_id)

    def start_due(self) -> None:
        """Scheduler tick: start every profile whose interval has elapsed."""
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            profiles = session.exec(select(CriteriaProfile).where(CriteriaProfile.scan_interval_hours > 0)).all()
            due = [
                profile.id
                for profile in profiles
                if profile.last_scan_at is None or (now - _utc(profile.last_scan_at)).total_seconds() >= profile.scan_interval_hours * 3600
            ]
        for profile_id in due:
            self.start(profile_id)

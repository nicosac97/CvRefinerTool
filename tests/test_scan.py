from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from cvrefine.db import init_db
from cvrefine.llm.decisions import ChoiceResult, NoulResult, ScoreResult
from cvrefine.models import CriteriaProfile, FeedCache, Job, ScanRun
from cvrefine.pipeline import scan
from cvrefine.pipeline.normalize import dedupe_key, hard_filter_reason, html_to_text, matches_keywords, normalize_company
from cvrefine.sources.base import RawJob, SourceError
from cvrefine.sources.remote_boards import RemoteOK

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def raw(external_id, title, company="Acme", source="feed", days_old=1, **extra):
    return RawJob(source=source, external_id=external_id, url=f"https://x/{external_id}", title=title, company=company,
                  description=extra.pop("description", f"<p>{title} job</p><ul><li>AWS</li></ul>"), description_is_html=True,
                  posted_at=NOW - timedelta(days=days_old), **extra)


def test_html_to_text_keeps_structure():
    assert html_to_text("<p>Hello&nbsp;world</p><ul><li>AWS</li><li>K8s</li></ul>") == "Hello world\n- AWS\n- K8s"
    assert html_to_text("plain text") == "plain text"


def test_normalizers():
    assert dedupe_key("Acme GmbH", "Cloud Engineer (m/w/d)") == "acme|cloud engineer"
    assert normalize_company("Big Co, Inc.") == "big"
    assert matches_keywords(raw("1", "Senior DevOps Engineer"), ["devops", "cloud engineer"])
    assert matches_keywords(raw("1", "Engineer", tags=["cloud engineer"]), ["cloud engineer"])
    assert not matches_keywords(raw("1", "Sales Manager"), ["devops"])
    assert not matches_keywords(raw("1", "DevOpsy role"), ["devops"])


def test_hard_filters():
    profile = CriteriaProfile(name="p", excluded_companies="Evil, Bad Corp", min_salary=60000, max_age_days=10)
    assert hard_filter_reason(raw("1", "x", company="Evil Inc"), profile, NOW) == "excluded company"
    assert hard_filter_reason(raw("1", "x", days_old=11), profile, NOW) == "too old"
    assert hard_filter_reason(raw("1", "x", salary_max=50000), profile, NOW) == "salary below minimum"
    assert hard_filter_reason(raw("1", "x", salary_max=70000), profile, NOW) is None
    assert hard_filter_reason(raw("1", "x"), profile, NOW) is None


def test_remoteok_parsing(monkeypatch):
    payload = [{"legal": "notice"}, {"id": "9", "position": "Cloud Engineer", "company": "Acme", "epoch": 1791878411, "tags": ["aws"],
                                     "url": "https://remoteok.com/9", "description": "<p>x</p>", "salary_min": 0, "salary_max": 90000}]

    class Response:
        def json(self):
            return payload

    monkeypatch.setattr("cvrefine.sources.remote_boards._get", lambda url, **params: Response())
    [job] = RemoteOK().fetch()
    assert (job.external_id, job.title, job.salary_min, job.salary_max, job.remote) == ("9", "Cloud Engineer", None, 90000, True)
    assert job.posted_at.year == 2026


class FakeFeed:
    name, label, attribution = "feed", "Feed", ""
    min_interval = timedelta(hours=3)

    def __init__(self, jobs):
        self.jobs, self.calls = jobs, 0

    def fetch(self):
        self.calls += 1
        return self.jobs


class FakeSearch:
    name, label, attribution, warning = "search", "Search", "", ""

    def __init__(self, jobs=None, error=None):
        self.jobs, self.error = jobs or [], error

    def search(self, profile, max_age_days):
        if self.error:
            raise self.error
        return self.jobs


class FakeDecisions:
    def __init__(self, same=0.0):
        self.same, self.calls = same, 0

    def decide(self, state, questions):
        self.calls += 1
        if "same" in questions:
            return {"same": NoulResult(self.same)}
        return {
            "skills_fit": ScoreResult(3.0, 0.9, {}, 5),
            "seniority_fit": ScoreResult(2.0, 0.9, {}, 3),
            "work_mode": ChoiceResult("remote", 0.9, {"remote": 0.9, "unclear": 0.1}),
        }


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def setup(engine, monkeypatch, feed_jobs, search=None, sources="feed,search"):
    feed = FakeFeed(feed_jobs)
    monkeypatch.setattr(scan, "FEEDS", {"feed": feed})
    monkeypatch.setattr(scan, "SEARCHES", {"search": search or FakeSearch()})
    with Session(engine) as session:
        profile = CriteriaProfile(name="p", keywords="cloud engineer, devops", sources=sources, max_age_days=10)
        session.add(profile)
        session.commit()
        return feed, profile.id


def test_scan_filters_dedupes_and_scores(engine, monkeypatch):
    feed_jobs = [
        raw("1", "Cloud Engineer", company="Acme GmbH"),
        raw("2", "Cloud Engineer (m/w/d)", company="Acme"),  # same company + title as 1
        raw("3", "Sales Manager"),  # no keyword
        raw("4", "DevOps Engineer", days_old=30),  # too old
        raw("5", "Senior Cloud Engineer", company="Beta"),
    ]
    search = FakeSearch([raw("s1", "DevOps Engineer", company="Gamma", source="search")])
    feed, profile_id = setup(engine, monkeypatch, feed_jobs, search)
    decisions = FakeDecisions()

    run_id = scan.run_scan(profile_id, lambda: Session(engine), lambda: decisions, NOW)
    with Session(engine) as session:
        run = session.get(ScanRun, run_id)
        jobs = session.exec(select(Job)).all()
        assert sorted(job.external_id for job in jobs) == ["1", "5", "s1"]
        assert all(job.score is not None and job.profile_id == profile_id for job in jobs)
        assert run.stats["feed"] == {"fetched": 5, "matched": 4, "new": 2, "duplicates": 1, "filtered": 1, "error": ""}
        assert run.stats["search"]["new"] == 1
        assert run.scored == 3 and run.finished_at is not None
        assert session.get(CriteriaProfile, profile_id).last_scan_at is not None
        assert "- AWS" in jobs[0].description

    # Second scan: feed is cached, already-seen jobs are skipped silently.
    run_id = scan.run_scan(profile_id, lambda: Session(engine), lambda: decisions, NOW + timedelta(hours=1))
    with Session(engine) as session:
        assert feed.calls == 1
        assert session.get(ScanRun, run_id).stats["feed"]["new"] == 0
        assert len(session.exec(select(Job)).all()) == 3

    # After the interval the feed is fetched again.
    scan.run_scan(profile_id, lambda: Session(engine), lambda: decisions, NOW + timedelta(hours=4))
    assert feed.calls == 2


def test_ambiguous_titles_ask_decision_model(engine, monkeypatch):
    feed_jobs = [raw("1", "Cloud Engineer AWS", company="Acme"), raw("2", "Cloud Engineer Azure Team", company="Acme")]
    _, profile_id = setup(engine, monkeypatch, feed_jobs, sources="feed")
    decisions = FakeDecisions(same=0.9)
    scan.run_scan(profile_id, lambda: Session(engine), lambda: decisions, NOW)
    with Session(engine) as session:
        assert len(session.exec(select(Job)).all()) == 1


def test_source_errors_do_not_fail_scan(engine, monkeypatch):
    _, profile_id = setup(engine, monkeypatch, [raw("1", "Cloud Engineer")], FakeSearch(error=SourceError("blocked 429")))
    run_id = scan.run_scan(profile_id, lambda: Session(engine), lambda: FakeDecisions(), NOW)
    with Session(engine) as session:
        run = session.get(ScanRun, run_id)
        assert run.stats["search"]["error"] == "blocked 429"
        assert run.stats["feed"]["new"] == 1


def test_scan_without_decision_model_stores_unscored(engine, monkeypatch):
    from cvrefine.llm.decisions import DecisionError

    _, profile_id = setup(engine, monkeypatch, [raw("1", "Cloud Engineer")], sources="feed")

    def no_key():
        raise DecisionError("key missing")

    run_id = scan.run_scan(profile_id, lambda: Session(engine), no_key, NOW)
    with Session(engine) as session:
        assert "key missing" in session.get(ScanRun, run_id).error
        [job] = session.exec(select(Job)).all()
        assert job.score is None


def test_feed_cache_round_trip(engine, monkeypatch):
    feed, _ = setup(engine, monkeypatch, [raw("1", "Cloud Engineer", tags=["aws"])])
    with Session(engine) as session:
        scan.get_feed(session, "feed", NOW)
        cached = scan.get_feed(session, "feed", NOW + timedelta(minutes=5))
        assert feed.calls == 1 and cached[0].tags == ["aws"] and cached[0].posted_at == NOW - timedelta(days=1)
        assert session.get(FeedCache, "feed") is not None


def test_jobspy_logged_block_becomes_source_error(monkeypatch):
    import logging

    import jobspy
    import pandas as pd

    from cvrefine.sources.jobspy_source import JobSpySite

    def blocked(**kwargs):
        logging.getLogger("JobSpy:Glassdoor").error("Glassdoor: bad response status code: 403")
        return pd.DataFrame()

    monkeypatch.setattr(jobspy, "scrape_jobs", blocked)
    with pytest.raises(SourceError, match="403"):
        JobSpySite("glassdoor", "Glassdoor").search(CriteriaProfile(name="p", keywords="devops"), 14)

    def rows(**kwargs):
        return pd.DataFrame([{"id": "in-1", "job_url": "https://indeed/1", "title": "DevOps", "company": "Acme", "location": "Milano",
                              "description": "text", "date_posted": datetime(2026, 10, 1).date(), "min_amount": float("nan"),
                              "max_amount": 60000.0, "interval": "yearly", "is_remote": True}])

    monkeypatch.setattr(jobspy, "scrape_jobs", rows)
    [job] = JobSpySite("indeed", "Indeed").search(CriteriaProfile(name="p", keywords="devops, sre"), 14)
    assert (job.external_id, job.salary_min, job.salary_max, job.posted_at.day) == ("in-1", None, 60000.0, 1)


def test_init_db_adds_new_columns_to_old_tables():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE job (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)"))
        connection.execute(text("INSERT INTO job (id, title) VALUES (1, 'old')"))
    init_db(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("job")}
    assert {"external_id", "dedupe_key", "area", "posted_at"} <= columns
    with Session(engine) as session:
        assert session.get(Job, 1).area == ""

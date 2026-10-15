import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from cvrefine import app as app_module
from cvrefine.db import get_session
from cvrefine.llm.decisions import ChoiceResult, DecisionError, ScoreResult


class FakeDecisions:
    def decide(self, state, questions):
        return {
            "skills_fit": ScoreResult(4.0, 0.9, {}, 5),
            "seniority_fit": ScoreResult(2.0, 0.9, {}, 3),
            "work_mode": ChoiceResult("remote", 0.9, {"remote": 0.9, "unclear": 0.1}),
        }


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def session_override():
        with Session(engine) as session:
            yield session

    app_module.app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr(app_module, "get_decision_client", FakeDecisions)
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def test_tabs_render(client):
    for path in ("/monitor", "/jobs", "/tailor"):
        assert client.get(path).status_code == 200


def test_profile_and_manual_job_flow(client):
    client.post("/candidate", data={"summary": "Cloud dev, AWS"})
    client.post("/profiles", data={"name": "Remote EU", "work_mode": "remote", "min_salary": "50000"})
    assert "Remote EU" in client.get("/monitor").text

    response = client.post(
        "/jobs/manual",
        data={"title": "Cloud Engineer", "company": "Acme", "description": "AWS, Terraform", "profile_id": "1"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/jobs/1"
    detail = client.get("/jobs/1").text
    assert "Cloud Engineer" in detail and "/ 100" in detail and "Work mode" in detail

    client.post("/jobs/1/feedback", data={"value": "1"})
    client.post("/jobs/1/status", data={"status": "shortlisted"})
    assert "Cloud Engineer" in client.get("/tailor").text
    client.post("/jobs/1/status", data={"status": "dismissed"})
    assert "Cloud Engineer" not in client.get("/jobs").text
    assert "Cloud Engineer" in client.get("/jobs?status=dismissed").text


def test_manual_job_validation(client):
    empty = client.post("/jobs/manual", data={}, follow_redirects=False)
    assert "error=" in empty.headers["location"]
    bad_url = client.post("/jobs/manual", data={"url": "javascript:alert(1)", "description": "x"}, follow_redirects=False)
    assert "error=" in bad_url.headers["location"]


def test_tailoring_flow(client, monkeypatch, tmp_path):
    from pathlib import Path

    from cvrefine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.cv_dir.mkdir()
    master = (Path(__file__).parent / "fixtures" / "cv.tex").read_text(encoding="utf-8")
    settings.cv_file.write_text(master, encoding="utf-8")
    (settings.cv_dir / "photo.jpeg").write_bytes(b"jpeg")

    class FakeWriter:
        def propose_edits(self, editable, locked_context, job_text):
            return {
                "requirements": ["Terraform"],
                "keywords": ["Terraform", "Python"],
                "edits": [
                    {"block_id": "computer-skills:intermediate", "proposed": "Python, AWS, Docker", "rationale": "Python first", "keywords_covered": ["Python"]},
                    {"block_id": "summary", "proposed": "Engineer with 10 years of Terraform.", "rationale": "", "keywords_covered": ["Terraform"]},
                ],
            }

    monkeypatch.setattr(app_module, "Writer", FakeWriter)
    monkeypatch.setattr(app_module, "render_pdf", lambda tex: (None, "No LaTeX engine found."))
    client.post("/jobs/manual", data={"title": "Platform Engineer", "description": "Terraform, Python"})

    assert "computer-skills:intermediate" not in client.get("/tailor").text  # labels shown, not ids
    response = client.post("/tailor/job/1", follow_redirects=False)
    assert response.headers["location"] == "/tailor/1"
    review = client.get("/tailor/1").text
    assert "Python first" in review and "Apply accepted edits" in review

    client.post("/tailor/1", data={"decision_0": "accepted", "text_0": "Python, Docker, AWS", "decision_1": "rejected"})
    out_dir = settings.tailored_dir / "job1-t1"
    tailored = (out_dir / "main.tex").read_text(encoding="utf-8")
    assert "\\cvitem{Intermediate}{Python, Docker, AWS}" in tailored
    assert "10 years" not in tailored
    assert (out_dir / "photo.jpeg").exists()
    assert settings.cv_file.read_text(encoding="utf-8") == master
    review = client.get("/tailor/1").text
    assert "No LaTeX engine found." in review and "Download .tex" in review
    assert client.get("/tailor/1/file/tex").status_code == 200
    assert client.get("/tailor/1/file/pdf").status_code == 404
    assert client.get("/tailor/1/file/db").status_code == 404


def test_profile_edit_and_scan_trigger(client, monkeypatch):
    started = []
    monkeypatch.setattr(app_module.scans, "start", started.append)
    client.post("/profiles", data={"name": "EU", "keywords": "devops", "sources": ["remoteok", "linkedin", "bogus"], "max_age_days": "500"})
    page = client.get("/monitor").text
    assert "remoteok, linkedin" in page and "max age 90d" in page

    client.post("/profiles/1", data={"name": "EU remote", "work_mode": "remote", "sources": ["indeed"], "country": "Germany", "scan_interval_hours": "6"})
    page = client.get("/monitor?edit=1").text
    assert "EU remote" in page and "every 6h" in page and "country germany" in page

    client.post("/profiles/1/scan")
    assert started == [1]
    assert client.post("/profiles/99/scan").status_code == 404


def test_area_tailoring(client, monkeypatch, tmp_path):
    from pathlib import Path

    from cvrefine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.cv_dir.mkdir()
    settings.cv_file.write_text((Path(__file__).parent / "fixtures" / "cv.tex").read_text(encoding="utf-8"), encoding="utf-8")
    seen = {}

    class FakeWriter:
        def propose_edits(self, editable, locked_context, job_text):
            seen["job_text"] = job_text
            return {"requirements": [], "keywords": [], "edits": []}

    monkeypatch.setattr(app_module, "Writer", FakeWriter)
    for title in ("AWS Engineer", "Platform Engineer", "Unrelated"):
        client.post("/jobs/manual", data={"title": title, "description": f"{title} description"})
    client.post("/jobs/1/area", data={"area": " AWS platform "})
    client.post("/jobs/2/area", data={"area": "AWS platform"})
    assert "2 open jobs" in client.get("/tailor").text
    assert client.get("/jobs?area=AWS%20platform").text.count("/jobs/") >= 2

    response = client.post("/tailor/area", data={"area": "AWS platform"}, follow_redirects=False)
    assert response.headers["location"] == "/tailor/1"
    assert "AWS Engineer description" in seen["job_text"] and "Platform Engineer description" in seen["job_text"]
    assert "Unrelated" not in seen["job_text"]
    assert "Area: AWS platform" in client.get("/tailor/1").text
    assert "error=" in client.post("/tailor/area", data={"area": "nothing"}, follow_redirects=False).headers["location"]


def test_scoring_failure_is_recorded_not_raised(client, monkeypatch):
    def broken():
        raise DecisionError("401 User not found.")

    monkeypatch.setattr(app_module, "get_decision_client", broken)
    client.post("/jobs/manual", data={"title": "Job", "description": "text"})
    assert "Scoring failed: 401 User not found." in client.get("/jobs/1").text
    assert "error" in client.get("/jobs").text

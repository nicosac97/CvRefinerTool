from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from cvrefine import app as app_module
from cvrefine.cv import letter
from cvrefine.cv.parser import parse
from cvrefine.db import get_session
from cvrefine.llm.decisions import ChoiceResult, DecisionError, NoulResult, ScoreResult
from cvrefine.llm.writer import WriterError
from cvrefine.models import Job

SOURCE = (Path(__file__).parent / "fixtures" / "cv.tex").read_text(encoding="utf-8")
JOB = Job(title="Backend Engineer", company="Acme", description="Java, AWS, Kafka. Lavoro ibrido a Milano.")


class FakeWriter:
    """Paragraph texts containing 'invent' are fabrications; the fake decision model scores them low."""

    def __init__(self):
        self.calls = []

    def write_cover_letter(self, cv_facts, job_text, notes, language):
        self.calls.append(("letter", cv_facts, notes, language))
        return {
            "requirements": ["Java", "AWS", "Kafka"],
            "paragraphs": [
                {"purpose": "opening", "text": "Backend role at Acme.", "requirements_covered": []},
                {"purpose": "match", "text": "Java microservices on AWS.", "requirements_covered": ["Java", "aws"]},
                {"purpose": "match", "text": "I invent 5 years of Kafka.", "requirements_covered": ["Kafka"]},
                {"purpose": "closing", "text": "  ", "requirements_covered": []},
            ],
        }

    def rewrite_paragraph(self, paragraphs, index, instruction, cv_facts, job_text, notes, language):
        self.calls.append(("rewrite", paragraphs, index, instruction))
        return {"text": f"Rewritten: {instruction}", "requirements_covered": ["Java"], "note": "Ignored: not in CV." if "invent" in instruction else ""}


class FakeDecisions:
    def __init__(self):
        self.questions = []

    def decide(self, state, questions):
        self.questions.append(set(questions))
        if "language" in questions:
            return {"language": ChoiceResult("it", 0.9, {"it": 0.9, "en": 0.1})}
        if "skills_fit" in questions:
            return {
                "skills_fit": ScoreResult(3.0, 0.9, {}, 5),
                "seniority_fit": ScoreResult(2.0, 0.9, {}, 3),
                "work_mode": ChoiceResult("remote", 0.9, {"remote": 0.9, "unclear": 0.1}),
            }
        if "COVER LETTER PARAGRAPHS:\n" not in state:  # CV edit grounding
            return {name: NoulResult(0.9) for name in questions}
        texts = state.split("COVER LETTER PARAGRAPHS:\n")[1].split("\n\n")
        return {name: NoulResult(0.1 if "invent" in text else 0.5 if "shorter" in text else 0.9) for name, text in zip(questions, texts)}


def test_cv_name():
    assert letter.cv_name(SOURCE) == "Jane Doe"
    assert letter.cv_name(r"\name{John}{Smith}") == "John Smith"
    assert letter.cv_name("no name") == ""


def test_draft_checks_paragraphs_and_drops_fabrications():
    result = letter.draft("facts", JOB, "notes", "en", FakeWriter(), FakeDecisions())
    paragraphs = result["paragraphs"]
    assert [p["purpose"] for p in paragraphs] == ["opening", "match", "match"]  # empty paragraph skipped
    assert [p["status"] for p in paragraphs] == ["kept", "kept", "dropped"]
    assert paragraphs[2]["grounded"] == pytest.approx(0.1)
    covered, missing = letter.covered_requirements(result["requirements"], paragraphs)
    assert covered == ["Java", "AWS"] and missing == ["Kafka"]


def test_draft_without_decision_model_keeps_everything_unchecked():
    result = letter.draft("facts", JOB, "", "en", FakeWriter(), None)
    assert all(p["status"] == "kept" and p["grounded"] is None for p in result["paragraphs"])
    assert "not fact-checked" in result["grounding_error"]


def test_regenerate_changes_one_paragraph_and_rechecks():
    writer = FakeWriter()
    paragraphs = letter.draft("facts", JOB, "", "en", writer, FakeDecisions())["paragraphs"]
    new, error = letter.regenerate(paragraphs, 2, "invent more", "facts", JOB, "", "en", writer, FakeDecisions())
    assert writer.calls[-1][1] == [p["final"] for p in paragraphs] and writer.calls[-1][2] == 2
    assert new["final"] == "Rewritten: invent more" and new["purpose"] == "match"
    assert new["status"] == "dropped" and new["note"] == "Ignored: not in CV." and error == ""
    # "Check facts" band (0.4-0.65) stays kept for letters: soft but faithful prose scores there.
    new, _ = letter.regenerate(paragraphs, 2, "shorter", "facts", JOB, "", "en", writer, FakeDecisions())
    assert new["grounded"] == pytest.approx(0.5) and new["status"] == "kept" and new["note"] == ""


def test_assemble():
    paragraphs = letter.draft("facts", JOB, "", "it", FakeWriter(), FakeDecisions())["paragraphs"]
    text = letter.assemble(paragraphs, JOB, "Jane Doe", "it", date(2026, 10, 15))
    assert text.startswith("15 ottobre 2026\n\nCandidatura per Backend Engineer - Acme\n\nGentile Team di Selezione,\n\nBackend role at Acme.")
    assert "invent" not in text
    assert text.endswith("Cordiali saluti,\nJane Doe\n")


def test_letter_facts_prefers_tailored_cv():
    master = parse(SOURCE)
    tailored = SOURCE.replace("AWS, Docker, Python", "Python, AWS, Docker")
    assert "Python, AWS, Docker" in letter.letter_facts(master, tailored, 2026)
    assert letter.letter_facts(master, None, 2026).startswith("Professional experience: about 4 years")


@pytest.fixture
def client(monkeypatch, tmp_path):
    from cvrefine.config import settings

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def session_override():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.cv_dir.mkdir()
    settings.cv_file.write_text(SOURCE, encoding="utf-8")
    decisions, writer = FakeDecisions(), FakeWriter()
    monkeypatch.setattr(app_module, "get_decision_client", lambda: decisions)
    monkeypatch.setattr(app_module, "Writer", lambda: writer)
    app_module.app.dependency_overrides[get_session] = session_override
    yield TestClient(app_module.app), decisions, writer
    app_module.app.dependency_overrides.clear()


def test_letter_flow_without_tailoring(client):
    from cvrefine.config import settings

    http, decisions, writer = client
    http.post("/jobs/manual", data={"title": "Backend Engineer", "company": "Acme", "description": "Java, AWS, Kafka"})
    # Scoring a job asks no letter questions; nothing letter-related runs until requested.
    assert decisions.questions == [{"work_mode", "skills_fit", "seniority_fit"}] and writer.calls == []
    assert "Write cover letter" in http.get("/jobs/1").text

    response = http.post("/letters", data={"job_id": "1", "language": "auto", "notes": "I like Acme"}, follow_redirects=False)
    assert response.headers["location"] == "/letters/1"
    assert writer.calls[0][2:] == ("I like Acme", "Italian")  # language detected by the decision model
    assert "Professional experience" in writer.calls[0][1] and "jane@example.com" not in writer.calls[0][1]
    page = http.get("/letters/1").text
    assert "possible fabrication" in page and "Italian" in page and "master CV" in page

    http.post("/letters/1/paragraph/0", data={"text_1": "Edited match.", "decision_1": "kept", "decision_0": "kept", "instruction_0": "shorter"})
    assert writer.calls[-1][1][1] == "Edited match."  # edits to other paragraphs kept

    http.post("/letters/1", data={"decision_0": "kept", "text_0": "Rewritten: shorter", "decision_1": "kept", "text_1": "Edited match.", "decision_2": "dropped"})
    saved = (settings.tailored_dir / "job1-letter1" / "cover_letter-1.md").read_text(encoding="utf-8")
    assert "Rewritten: shorter\n\nEdited match." in saved and "invent" not in saved and saved.endswith("Jane Doe\n")
    assert "Copy text" in http.get("/letters/1").text
    assert http.get("/letters/1/file").status_code == 200
    assert settings.cv_file.read_text(encoding="utf-8") == SOURCE
    assert "1 written" in http.get("/jobs/1").text


def test_letter_uses_applied_tailoring(client, monkeypatch):
    http, _, writer = client
    monkeypatch.setattr(app_module, "render_pdf", lambda tex: (None, "no engine"))

    def propose_edits(editable, locked_context, job_text):
        return {"requirements": [], "keywords": [], "edits": [{"block_id": "computer-skills:intermediate", "proposed": "Python, AWS, Docker", "rationale": "", "keywords_covered": []}]}

    writer.propose_edits = propose_edits
    http.post("/jobs/manual", data={"title": "Backend Engineer", "description": "Python"})
    http.post("/tailor/job/1")
    http.post("/tailor/1", data={"decision_0": "accepted", "text_0": "Python, AWS, Docker"})
    assert "tailored CV from tailoring #1" in http.get("/tailor/1").text

    http.post("/letters", data={"job_id": "1", "tailoring_id": "1", "language": "en"})
    assert "Python, AWS, Docker" in writer.calls[-1][1] and writer.calls[-1][3] == "English"
    http.post("/letters/1", data={"decision_0": "kept", "text_0": "Hello."})
    assert (Path(app_module.settings.tailored_dir) / "job1-t1" / "cover_letter-1.md").exists()


def test_letter_errors_are_shown(client, monkeypatch):
    http, _, writer = client
    http.post("/jobs/manual", data={"title": "Job", "description": "text"})

    def broken(*args):
        raise WriterError("writer model returned 429")

    writer.write_cover_letter = broken
    response = http.post("/letters", data={"job_id": "1"}, follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert "writer model returned 429" in http.get(response.headers["location"]).text

    def no_key():
        raise DecisionError("no key")

    monkeypatch.setattr(app_module, "get_decision_client", no_key)
    writer.write_cover_letter = FakeWriter().write_cover_letter
    http.post("/letters", data={"job_id": "1", "language": "auto"})
    page = http.get("/letters/1").text
    assert "not fact-checked" in page and "English" in page
    assert http.get("/letters/99").status_code == 404
    assert http.post("/letters/1/paragraph/9", data={}).status_code == 404

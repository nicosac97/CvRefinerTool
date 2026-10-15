from pathlib import Path

import pytest

from cvrefine.cv.parser import ParseError, apply_edits, parse, validate_latex
from cvrefine.cv.tailor import GROUNDED_OK, keyword_coverage, propose
from cvrefine.llm.decisions import DecisionError, NoulResult
from cvrefine.models import Job

SOURCE = (Path(__file__).parent / "fixtures" / "cv.tex").read_text(encoding="utf-8")


def test_blocks_and_editability():
    cv = parse(SOURCE)
    ids = {block.id: block for block in cv.blocks}
    assert set(ids) == {
        "title",
        "summary",
        "work-experience:0",
        "education:0",
        "computer-skills:advanced",
        "computer-skills:intermediate",
        "languages:english",
    }
    assert ids["work-experience:0"].text.startswith("Built Java")
    assert ids["work-experience:0"].label == "Cloud Developer - Acme"
    assert ids["summary"].text.startswith("Engineer focused")
    assert ids["computer-skills:advanced"].text == "Java"
    assert [block.id for block in cv.editable] == ["title", "summary", "work-experience:0", "computer-skills:advanced", "computer-skills:intermediate"]
    assert "jane@example.com" not in cv.plain_text()
    assert "Commented out" not in cv.plain_text()


def test_candidate_summary_adds_experience_years():
    cv = parse(SOURCE)
    summary = cv.candidate_summary(2026)
    # Education 2019 and the commented-out 2019 job must not count.
    assert summary.startswith("Professional experience: about 4 years (since 2022)\n")
    assert summary.endswith(cv.plain_text())
    assert parse(r"\section{Skills}\cvitem{x}{Java}").candidate_summary(2026) == "Skills: x | Java"


def test_round_trip_and_targeted_edit():
    cv = parse(SOURCE)
    assert apply_edits(cv, {}) == SOURCE
    edited = apply_edits(cv, {"computer-skills:intermediate": "Python, AWS, Docker", "title": "Cloud Engineer"})
    assert "\\cvitem{Intermediate}{Python, AWS, Docker}" in edited
    assert "\\title{Cloud Engineer}" in edited
    assert edited.replace("Python, AWS, Docker", "AWS, Docker, Python").replace("Cloud Engineer}", "Cloud \\& Backend Engineer}") == SOURCE


def test_locked_and_invalid_edits_rejected():
    cv = parse(SOURCE)
    with pytest.raises(ParseError):
        apply_edits(cv, {"education:0": "PhD"})
    with pytest.raises(ParseError):
        apply_edits(cv, {"title": "Broken {"})


@pytest.mark.parametrize(
    "text,ok",
    [("AWS \\& GCP, 30\\% faster", True), ("\\textbf{AWS}", True), ("a}b{", False), ("50% off", False), ("\\input{/etc/passwd}", False), ("x \\end{document}", False)],
)
def test_validate_latex(text, ok):
    assert (validate_latex(text) is None) == ok


class FakeWriter:
    def __init__(self, edits):
        self.edits = edits
        self.calls = []

    def propose_edits(self, editable, locked_context, job_text):
        self.calls.append((editable, locked_context, job_text))
        return {"requirements": ["AWS"], "keywords": ["AWS", "Terraform", "Java"], "edits": self.edits}


class FakeDecisions:
    def __init__(self, probabilities=None, fail=False):
        self.probabilities, self.fail = probabilities or {}, fail

    def decide(self, state, questions):
        if self.fail:
            raise DecisionError("down")
        return {name: NoulResult(self.probabilities.get(name, 0.95)) for name in questions}


def test_propose_filters_and_grounds():
    cv = parse(SOURCE)
    writer = FakeWriter(
        [
            {"block_id": "summary", "proposed": "Cloud-native backend engineer.", "rationale": "focus", "keywords_covered": []},
            {"block_id": "computer-skills:intermediate", "proposed": "AWS, Terraform, Docker, Python", "rationale": "", "keywords_covered": ["Terraform"]},
            {"block_id": "education:0", "proposed": "PhD", "rationale": "", "keywords_covered": []},
            {"block_id": "title", "proposed": "Cloud \\& Backend Engineer", "rationale": "", "keywords_covered": []},
            {"block_id": "computer-skills:advanced", "proposed": "Java {", "rationale": "", "keywords_covered": []},
            {"block_id": "summary", "proposed": "Second try", "rationale": "", "keywords_covered": []},
        ]
    )
    result = propose(cv, Job(title="Cloud Engineer", description="AWS role"), writer, FakeDecisions({"edit_1": 0.1}))
    editable, locked, job_text = writer.calls[0]
    assert {item["block_id"] for item in editable} == {block.id for block in cv.editable}
    assert "MSc Computer Science" in locked and "Cloud Engineer" in job_text

    assert [edit["block_id"] for edit in result["edits"]] == ["summary", "computer-skills:intermediate"]
    assert len(result["dropped"]) == 3  # locked block, invalid LaTeX, duplicate block
    summary, skills = result["edits"]
    assert summary["grounded"] >= GROUNDED_OK and summary["status"] == "accepted"
    assert skills["grounded"] == 0.1 and skills["status"] == "rejected"


def test_propose_survives_grounding_failure():
    cv = parse(SOURCE)
    writer = FakeWriter([{"block_id": "summary", "proposed": "Short.", "rationale": "", "keywords_covered": []}])
    result = propose(cv, Job(description="x"), writer, FakeDecisions(fail=True))
    assert result["grounding_error"] == "down"
    assert result["edits"][0]["grounded"] is None and result["edits"][0]["status"] == "pending"


def test_keyword_coverage_matches_whole_terms():
    assert keyword_coverage("Java \\& AWS, Kubernetes", ["AWS", "Java", "JavaScript", "Go", "k8s"]) == ["AWS", "Java"]

import json

import httpx
import pytest

from cvrefine.llm.decisions import ChatDecisionClient, ChoiceResult, DecisionError, NoulResult, ScoreResult
from cvrefine.models import CriteriaProfile, Job
from cvrefine.pipeline.scoring import MAX_DESCRIPTION_CHARS, build_questions, build_state, combine, score_job


def answers(skills=4.0, seniority=2.0, mode="remote", mode_p=0.9, confidence=0.9, **nouls):
    rest = (1 - mode_p) / 3
    probabilities = {label: (mode_p if label == mode else rest) for label in ("remote", "hybrid", "onsite", "unclear")}
    return {
        "skills_fit": ScoreResult(skills, confidence, {}, 5),
        "seniority_fit": ScoreResult(seniority, confidence, {}, 3),
        "work_mode": ChoiceResult(mode, mode_p, probabilities),
        **{name: NoulResult(p) for name, p in nouls.items()},
    }


def test_questions_follow_profile():
    assert set(build_questions(None)) == {"work_mode", "skills_fit", "seniority_fit"}
    profile = CriteriaProfile(name="p", locations="Italy", must_have="AWS", custom_criteria="No clearance\n\nPermanent")
    questions = build_questions(profile)
    assert {"location_ok", "must_have", "custom_0", "custom_1"} <= set(questions)
    assert "Italy" in questions["location_ok"]["instructions"]
    assert all(question["criteria"] for question in questions.values() if question["type"] != "noul")


def test_perfect_fit_without_profile_is_100():
    total, uncertain, breakdown = combine(None, answers())
    assert total == 100
    assert not uncertain
    assert [row["label"] for row in breakdown] == ["Skills fit", "Seniority fit", "Work mode"]
    assert breakdown[2]["factor"] is None


def test_wrong_work_mode_pulls_score_down():
    profile = CriteriaProfile(name="p", work_mode="remote")
    remote, _, _ = combine(profile, answers(mode="remote", mode_p=0.97))
    onsite, _, _ = combine(profile, answers(mode="onsite", mode_p=0.97))
    assert remote > 95
    assert onsite < 30


def test_work_mode_strictness():
    hybrid = answers(mode="hybrid", mode_p=1.0)
    scores = {
        strictness: combine(CriteriaProfile(name="p", work_mode="remote", work_mode_strictness=strictness), hybrid)
        for strictness in ("strict", "soft", "off")
    }
    assert scores["strict"][0] == pytest.approx(20)
    assert scores["soft"][0] == pytest.approx(60)
    assert scores["off"][0] == 100
    assert scores["off"][2][2] == {"label": "Work mode", "answer": "hybrid", "confidence": 1.0, "factor": None}


def test_skills_use_chance_of_most_or_all():
    # Spread distribution: expected level 2.2 / 4 (old fraction 0.55), but 45% on "most" or "all".
    spread = {0: 0.05, 1: 0.2, 2: 0.3, 3: 0.3, 4: 0.15}
    result = answers()
    result["skills_fit"] = ScoreResult(2.2, 0.3, spread, 5)
    total, _, breakdown = combine(None, result)
    assert total == pytest.approx(100 * (0.7 * 0.45 + 0.3))
    assert breakdown[0]["answer"] == "most/all 45% (avg 2.2 / 4)"


def test_failed_criteria_scale_and_flag_uncertainty():
    profile = CriteriaProfile(name="p", locations="Italy", custom_criteria="No clearance")
    passed, uncertain, _ = combine(profile, answers(location_ok=1.0, custom_0=1.0))
    assert passed == 100 and not uncertain
    failed, _, breakdown = combine(profile, answers(location_ok=0.0, custom_0=0.0))
    assert failed == pytest.approx(100 * 0.2 * 0.5)
    assert breakdown[-1]["label"] == "No clearance"
    _, uncertain, _ = combine(profile, answers(location_ok=0.5, custom_0=1.0))
    assert uncertain


def test_low_confidence_flags_uncertain():
    _, uncertain, _ = combine(None, answers(confidence=0.4))
    assert uncertain


def test_state_truncates_long_description():
    job = Job(title="Cloud Engineer", company="Acme", description="x" * (MAX_DESCRIPTION_CHARS + 500))
    state = build_state("AWS dev", job)
    assert "Cloud Engineer | Acme" in state
    assert state.count("x") == MAX_DESCRIPTION_CHARS


def test_score_job_writes_result():
    class Fake:
        def decide(self, state, questions):
            return answers(skills=2.0, seniority=1.0)

    job = Job(title="t", description="d", score_error="old")
    score_job(Fake(), "summary", None, job)
    assert job.score == pytest.approx(100 * (0.7 * 0.5 + 0.3 * 0.5))
    assert job.score_error == "" and len(job.breakdown) == 3


def chat_client(content: str, status: int = 200) -> ChatDecisionClient:
    body = {"choices": [{"message": {"content": content}}]}
    return ChatDecisionClient(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))))


def test_chat_fallback_parses_and_normalizes():
    content = json.dumps(
        {
            "work_mode": {"probabilities": {"remote": 0.6, "hybrid": 0.2}},
            "skills_fit": {"probabilities": [0, 0, 0, 0.5, 0.5]},
            "seniority_fit": {"probabilities": [0, 0, 1]},
        }
    )
    results = chat_client(content).decide("state", build_questions(None))
    mode = results["work_mode"]
    assert mode.choice == "remote" and mode.probabilities["remote"] == pytest.approx(0.75)
    assert mode.probabilities["onsite"] == 0
    assert results["skills_fit"].score == pytest.approx(3.5)
    # All skills probability sits on "most" and "all", so skills count in full.
    assert combine(None, results)[0] == pytest.approx(100, abs=0.05)


@pytest.mark.parametrize("content,status", [("not json", 200), ("{}", 200), ("{}", 401)])
def test_chat_fallback_raises_decision_error(content, status):
    with pytest.raises(DecisionError):
        chat_client(content, status).decide("state", build_questions(None))

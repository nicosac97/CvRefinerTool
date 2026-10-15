"""Turn a criteria profile into decision questions and the answers into a 0-100 score."""

from typing import Any

from cvrefine.llm.decisions import ChoiceResult, DecisionClient, NoulResult, Questions, Result, ScoreResult, choice, noul, score
from cvrefine.models import WORK_MODE_STRICTNESS, CriteriaProfile, Job

# Jev context is 32K tokens; keep the state far below it. Typical postings are well under this.
MAX_DESCRIPTION_CHARS = 24_000
MAX_CANDIDATE_CHARS = 8_000

SKILLS_TOP_LEVELS = 2  # "Most" and "All" of SKILLS_LEVELS
SKILLS_WEIGHT = 0.7
SENIORITY_WEIGHT = 0.3
# A failed criterion scales the score down to this floor rather than to zero, so the job stays visible.
HARD_FLOOR = 0.2
SOFT_FLOOR = 0.5
LOW_CONFIDENCE = 0.6
NOUL_UNCERTAIN = (0.35, 0.65)

WORK_MODE_CRITERIA = {
    "remote": "Fully remote; at most occasional travel to an office",
    "hybrid": "Regular office days every week or month",
    "onsite": "Office based",
    "unclear": "Work mode is not stated",
}
SKILLS_LEVELS = [
    "None of the required skills are in the candidate profile",
    "Few of the required skills are in the candidate profile",
    "About half of the required skills are in the candidate profile",
    "Most of the required skills are in the candidate profile",
    "All of the required skills are in the candidate profile",
]
SENIORITY_LEVELS = [
    "The role's level is far from the candidate's experience",
    "The role's level is one step above or below the candidate's experience",
    "The role's level matches the candidate's experience",
]


def build_state(candidate_summary: str, job: Job) -> str:
    header = " | ".join(part for part in (job.title, job.company, job.location) if part)
    return (
        f"CANDIDATE PROFILE:\n{candidate_summary[:MAX_CANDIDATE_CHARS]}\n\n"
        f"JOB POSTING: {header}\n{job.description[:MAX_DESCRIPTION_CHARS]}"
    )


def build_questions(profile: CriteriaProfile | None) -> Questions:
    questions: Questions = {
        "work_mode": choice("What is the work mode of this job?", WORK_MODE_CRITERIA),
        "skills_fit": score("How well does the candidate profile cover the skills this job requires?", SKILLS_LEVELS),
        "seniority_fit": score("How well does the role's seniority match the candidate's experience?", SENIORITY_LEVELS),
    }
    if profile is None:
        return questions
    if profile.locations.strip():
        questions["location_ok"] = noul(f"Can a candidate based in {profile.locations.strip()} take this job without relocating?")
    if profile.must_have.strip():
        questions["must_have"] = noul(f"Does the job involve working with these technologies: {profile.must_have.strip()}?")
    for index, criterion in enumerate(profile.custom_list()):
        questions[f"custom_{index}"] = noul(f"Is this statement true of the job: {criterion}")
    return questions


def combine(profile: CriteriaProfile | None, answers: dict[str, Result]) -> tuple[float, bool, list[dict[str, Any]]]:
    """Return (score 0-100, uncertain flag, per-question breakdown for the UI)."""
    skills, seniority = answers["skills_fit"], answers["seniority_fit"]
    assert isinstance(skills, ScoreResult) and isinstance(seniority, ScoreResult)
    # Skills: chance the candidate covers most or all required skills. The expected level sits
    # near the middle of the scale even for good matches, because Jev spreads probability.
    skills_share = _top_share(skills, SKILLS_TOP_LEVELS)
    total = 100 * (SKILLS_WEIGHT * skills_share + SENIORITY_WEIGHT * _fraction(seniority))
    uncertain = skills.confidence < LOW_CONFIDENCE or seniority.confidence < LOW_CONFIDENCE
    breakdown = [
        _row("Skills fit", f"most/all {skills_share:.0%} (avg {skills.score:.1f} / {skills.levels - 1})", skills.confidence, None),
        _row("Seniority fit", f"{seniority.score:.1f} / {seniority.levels - 1}", seniority.confidence, None),
    ]

    mode = answers["work_mode"]
    assert isinstance(mode, ChoiceResult)
    factor = None
    floor = WORK_MODE_STRICTNESS.get(profile.work_mode_strictness, HARD_FLOOR) if profile else None
    if profile and profile.work_mode != "any" and floor is not None:
        # "unclear" counts half: the posting does not rule the wanted mode out.
        wanted = mode.probabilities.get(profile.work_mode, 0.0) + 0.5 * mode.probabilities.get("unclear", 0.0)
        factor = _scale(min(1.0, wanted), floor)
        total *= factor
        uncertain = uncertain or mode.confidence < LOW_CONFIDENCE
    breakdown.append(_row("Work mode", mode.choice, mode.confidence, factor))

    labels = {"location_ok": ("Location compatible", HARD_FLOOR), "must_have": ("Must-have stack", SOFT_FLOOR)}
    if profile:
        labels |= {f"custom_{index}": (criterion, SOFT_FLOOR) for index, criterion in enumerate(profile.custom_list())}
    for name, (label, floor) in labels.items():
        answer = answers.get(name)
        if not isinstance(answer, NoulResult):
            continue
        factor = _scale(answer.probability, floor)
        total *= factor
        uncertain = uncertain or NOUL_UNCERTAIN[0] < answer.probability < NOUL_UNCERTAIN[1]
        breakdown.append(_row(label, f"yes {answer.probability:.0%}", None, factor))

    return round(total, 1), uncertain, breakdown


def score_job(client: DecisionClient, candidate_summary: str, profile: CriteriaProfile | None, job: Job) -> None:
    """Score in place. Raises DecisionError; caller decides how to record it."""
    answers = client.decide(build_state(candidate_summary, job), build_questions(profile))
    job.score, job.uncertain, job.breakdown = combine(profile, answers)
    job.score_error = ""


def _fraction(result: ScoreResult) -> float:
    return result.score / (result.levels - 1)


def _top_share(result: ScoreResult, top: int) -> float:
    """Probability of the `top` highest levels; falls back to the expected value without a distribution."""
    if not result.probabilities:
        return _fraction(result)
    levels = sorted(result.probabilities)
    return min(1.0, sum(result.probabilities[level] for level in levels[-top:]))


def _scale(probability: float, floor: float) -> float:
    return floor + (1 - floor) * probability


def _row(label: str, answer: str, confidence: float | None, factor: float | None) -> dict[str, Any]:
    return {"label": label, "answer": answer, "confidence": confidence, "factor": factor}

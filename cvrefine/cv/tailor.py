"""Propose edits for a job, then check each one against the original CV with the decision model."""

import re
from typing import Any, Protocol

from cvrefine.cv.parser import ParsedCV, latex_to_text, validate_latex
from cvrefine.llm.decisions import ChoiceResult, DecisionClient, DecisionError, NoulResult, choice, noul
from cvrefine.models import Job
from cvrefine.pipeline.scoring import MAX_DESCRIPTION_CHARS

# From a small live sample: faithful edits scored 0.70-0.93, fabricated ones 0.01-0.27. Retune with more data.
GROUNDED_OK = 0.65
GROUNDED_WARN = 0.4
MAX_GROWTH = 1.3


class EditWriter(Protocol):
    def propose_edits(self, editable: list[dict[str, str]], locked_context: str, job_text: str) -> dict[str, Any]: ...


def job_text(job: Job) -> str:
    header = " | ".join(part for part in (job.title, job.company, job.location) if part)
    return f"{header}\n{job.description[:MAX_DESCRIPTION_CHARS]}"


def propose(cv: ParsedCV, job: Job, writer: EditWriter, decisions: DecisionClient | None) -> dict[str, Any]:
    """Return {"requirements", "keywords", "edits", "dropped"}; edits carry grounding probabilities."""
    editable = [{"block_id": block.id, "section": block.section, "label": latex_to_text(block.label), "text": block.text} for block in cv.editable]
    locked = "\n".join(f"{block.section}: {latex_to_text(block.facts)}" for block in cv.blocks if not block.editable)
    raw = writer.propose_edits(editable, locked, job_text(job))

    edits, dropped, seen = [], [], set()
    for item in raw["edits"]:
        block = cv.get(str(item.get("block_id", "")))
        proposed = str(item.get("proposed", "")).strip()
        reason = None
        if block is None or not block.editable:
            reason = f"unknown or locked block {item.get('block_id')!r}"
        elif block.id in seen:
            reason = f"second edit for {block.id}"
        elif proposed == block.text.strip():
            continue
        elif error := validate_latex(proposed):
            reason = f"{block.id}: {error}"
        elif len(proposed) > max(len(block.text) * MAX_GROWTH, len(block.text) + 40):
            reason = f"{block.id}: too long for a one-page CV"
        if reason:
            dropped.append(reason)
            continue
        seen.add(block.id)
        edits.append(
            {
                "block_id": block.id,
                "label": latex_to_text(block.label),
                "original": block.text,
                "proposed": proposed,
                "rationale": str(item.get("rationale", "")),
                "keywords": [str(keyword) for keyword in item.get("keywords_covered", [])],
                "grounded": None,
                "status": "pending",
                "final": proposed,
            }
        )

    grounding_error = ""
    if edits and decisions is not None:
        try:
            for edit, probability in zip(edits, check_grounding(cv, edits, decisions)):
                edit["grounded"] = probability
                edit["status"] = "accepted" if probability >= GROUNDED_OK else "rejected"
        except DecisionError as error:
            grounding_error = str(error)

    return {
        "requirements": [str(item) for item in raw.get("requirements", [])],
        "keywords": [str(item) for item in raw.get("keywords", [])],
        "edits": edits,
        "dropped": dropped,
        "grounding_error": grounding_error,
    }


def check_grounding(cv: ParsedCV, edits: list[dict[str, Any]], decisions: DecisionClient) -> list[float]:
    """One decision call; one yes/no question per edit."""
    proposals = "\n\n".join(f"[edit_{index}] replaces '{edit['label']}':\n{latex_to_text(edit['proposed'])}" for index, edit in enumerate(edits))
    state = f"ORIGINAL CV:\n{cv.plain_text()}\n\nPROPOSED EDITS:\n{proposals}"
    questions = {
        f"edit_{index}": (
            f"Is every fact in [edit_{index}] (technologies, skills and skill levels, employers, sectors, roles, dates, "
            "numbers, achievements) also stated in the ORIGINAL CV?"
        )
        for index in range(len(edits))
    }
    return check_claims(state, questions, decisions)


def check_claims(state: str, questions: dict[str, str], decisions: DecisionClient) -> list[float]:
    """One decision call with one yes/no question per item; probabilities in question order."""
    answers = decisions.decide(state, {name: noul(text) for name, text in questions.items()})
    probabilities = []
    for name in questions:
        answer = answers.get(name)
        if not isinstance(answer, NoulResult):
            raise DecisionError(f"missing grounding answer for {name}")
        probabilities.append(answer.probability)
    return probabilities


LANGUAGES = {"en": "English", "it": "Italian", "de": "German", "es": "Spanish"}
LANGUAGE_SAMPLE_CHARS = 3000


def detect_language(job: Job, decisions: DecisionClient) -> str:
    """Language code of the posting, from LANGUAGES; English when unsure or another language."""
    criteria = {**LANGUAGES, "other": "Another language"}
    answers = decisions.decide(job_text(job)[:LANGUAGE_SAMPLE_CHARS], {"language": choice("In which language is this job posting written?", criteria)})
    answer = answers.get("language")
    return answer.choice if isinstance(answer, ChoiceResult) and answer.choice in LANGUAGES else "en"


def keyword_coverage(text: str, keywords: list[str]) -> list[str]:
    plain = latex_to_text(text).lower()
    return [keyword for keyword in keywords if re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])", plain)]

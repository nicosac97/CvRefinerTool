"""Optional cover letter for one job: written from CV facts, checked paragraph by paragraph, assembled locally.

Only CV facts (no contact details), the candidate's notes and the posting go to the models.
Greeting, date, subject and name are added here.
"""

import re
from datetime import date
from typing import Any, Protocol

from cvrefine.cv.parser import ParsedCV, latex_to_text, parse
from cvrefine.cv.tailor import GROUNDED_WARN, LANGUAGES, check_claims, job_text
from cvrefine.llm.decisions import DecisionClient, DecisionError
from cvrefine.models import Job

# The fact check sees the posting so it can tell company claims from candidate claims; keep it short.
CHECK_JOB_CHARS = 6000
# Live sample: faithful paragraphs 0.52-0.95 (soft prose scores lower than CV edits), invented claims 0.02-0.05.
# So only "possible fabrication" (< GROUNDED_WARN) starts dropped; 0.4-0.65 is kept with a "check facts" badge.
KEEP_THRESHOLD = GROUNDED_WARN

SUBJECTS = {"en": "Application for {title}", "it": "Candidatura per {title}", "de": "Bewerbung als {title}", "es": "Candidatura para {title}"}
GREETINGS = {"en": "Dear Hiring Team,", "it": "Gentile Team di Selezione,", "de": "Sehr geehrtes Recruiting-Team,", "es": "Estimado equipo de selección,"}
SIGNOFFS = {"en": "Kind regards,", "it": "Cordiali saluti,", "de": "Mit freundlichen Grüßen", "es": "Atentamente,"}
MONTHS = {
    "en": "January February March April May June July August September October November December",
    "it": "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
}


class LetterWriter(Protocol):
    def write_cover_letter(self, cv_facts: str, job_text: str, notes: str, language: str) -> dict[str, Any]: ...

    def rewrite_paragraph(
        self, paragraphs: list[str], index: int, instruction: str, cv_facts: str, job_text: str, notes: str, language: str
    ) -> dict[str, Any]: ...


def letter_facts(master: ParsedCV, tailored_source: str | None, year: int) -> str:
    """CV facts the letter may use: the applied tailored CV when there is one, else the master CV."""
    cv = parse(tailored_source) if tailored_source else master
    return cv.candidate_summary(year)


def cv_name(source: str) -> str:
    """Candidate name from the moderncv preamble (\\name{first}{last} or \\firstname / \\familyname)."""
    match = re.search(r"\\name\{([^}]*)\}\{([^}]*)\}", source)
    if match:
        parts = match.groups()
    else:
        parts = tuple(found.group(1) for found in (re.search(rf"\\{command}\{{([^}}]*)\}}", source) for command in ("firstname", "familyname")) if found)
    return " ".join(latex_to_text(part) for part in parts if part.strip())


def draft(cv_text: str, job: Job, notes: str, language: str, writer: LetterWriter, decisions: DecisionClient | None) -> dict[str, Any]:
    """Return {"requirements", "paragraphs", "grounding_error"} ready for a CoverLetter."""
    raw = writer.write_cover_letter(cv_text, job_text(job), notes, LANGUAGES[language])
    paragraphs = [
        _paragraph(str(item.get("text", "")), item.get("requirements_covered", []), str(item.get("purpose", "")))
        for item in raw["paragraphs"]
        if str(item.get("text", "")).strip()
    ]
    grounding_error = ""
    if decisions is None:
        grounding_error = "Decision model unavailable; paragraphs were not fact-checked."
    else:
        try:
            for paragraph, probability in zip(paragraphs, check_paragraphs(cv_text, job, notes, [p["final"] for p in paragraphs], decisions)):
                _set_grounding(paragraph, probability)
        except DecisionError as error:
            grounding_error = str(error)
    return {"requirements": [str(item) for item in raw.get("requirements", [])], "paragraphs": paragraphs, "grounding_error": grounding_error}


def regenerate(
    paragraphs: list[dict[str, Any]], index: int, instruction: str, cv_text: str, job: Job, notes: str, language: str,
    writer: LetterWriter, decisions: DecisionClient | None,
) -> tuple[dict[str, Any], str]:
    """New version of paragraph `index` (others untouched) and a grounding error message, if any."""
    current = [paragraph["final"] for paragraph in paragraphs]
    raw = writer.rewrite_paragraph(current, index, instruction, cv_text, job_text(job), notes, LANGUAGES[language])
    paragraph = _paragraph(str(raw["text"]), raw.get("requirements_covered", []), paragraphs[index]["purpose"])
    paragraph["note"] = str(raw.get("note", "")).strip()
    if decisions is None:
        return paragraph, "Decision model unavailable; paragraph was not fact-checked."
    try:
        [probability] = check_paragraphs(cv_text, job, notes, [paragraph["final"]], decisions)
    except DecisionError as error:
        return paragraph, str(error)
    _set_grounding(paragraph, probability)
    return paragraph, ""


def check_paragraphs(cv_text: str, job: Job, notes: str, texts: list[str], decisions: DecisionClient) -> list[float]:
    listed = "\n\n".join(f"[p_{index}]\n{text}" for index, text in enumerate(texts))
    state = (
        f"CANDIDATE CV:\n{cv_text}\n\nCANDIDATE NOTES:\n{notes.strip() or '(none)'}\n\n"
        f"JOB POSTING:\n{job_text(job)[:CHECK_JOB_CHARS]}\n\nCOVER LETTER PARAGRAPHS:\n{listed}"
    )
    questions = {
        f"p_{index}": (
            f"Is every claim about the candidate in [p_{index}] (skills, technologies, years of experience, employers, clients, "
            "sectors, roles, achievements, numbers) stated in the CANDIDATE CV or the CANDIDATE NOTES? "
            "Statements describing the company or the role do not count as claims about the candidate."
        )
        for index in range(len(texts))
    }
    return check_claims(state, questions, decisions)


def covered_requirements(requirements: list[str], paragraphs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(covered, missing) by the kept paragraphs, using the writer's own requirement labels."""
    covered_names = {name.lower() for paragraph in paragraphs if paragraph["status"] == "kept" for name in paragraph["requirements"]}
    covered = [item for item in requirements if item.lower() in covered_names]
    return covered, [item for item in requirements if item.lower() not in covered_names]


def assemble(paragraphs: list[dict[str, Any]], job: Job, name: str, language: str, today: date) -> str:
    months = MONTHS[language].split()
    lines = [f"{today.day} {months[today.month - 1]} {today.year}", ""]
    if job.title:
        lines += [SUBJECTS[language].format(title=job.title) + (f" - {job.company}" if job.company else ""), ""]
    lines += [GREETINGS[language], ""]
    for paragraph in paragraphs:
        if paragraph["status"] == "kept" and paragraph["final"].strip():
            lines += [paragraph["final"].strip(), ""]
    lines += [SIGNOFFS[language], name]
    return "\n".join(lines).strip() + "\n"


def _paragraph(text: str, requirements: Any, purpose: str) -> dict[str, Any]:
    text = text.strip()
    return {
        "purpose": purpose,
        "text": text,
        "requirements": [str(item) for item in requirements or []],
        "grounded": None,
        "status": "kept",
        "final": text,
    }


def _set_grounding(paragraph: dict[str, Any], probability: float) -> None:
    paragraph["grounded"] = probability
    paragraph["status"] = "kept" if probability >= KEEP_THRESHOLD else "dropped"

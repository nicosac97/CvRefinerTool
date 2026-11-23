"""Update the master CV: full-source saves with a backup, and a quick "add certification" helper."""

import re
from datetime import datetime
from pathlib import Path

from cvrefine.config import settings
from cvrefine.cv.parser import ParseError, _mask_comments, _read_arguments, _slug, parse, validate_latex

_SECTION_RE = re.compile(r"\\section(?![A-Za-z])")
_LATEX_SPECIALS = {"&": r"\&", "%": r"\%", "_": r"\_", "#": r"\#", "$": r"\$"}


def history_dir() -> Path:
    return settings.cv_dir / "history"


def save_master(source: str) -> Path | None:
    """Validate, back up the current master, then overwrite it. Returns the backup path (None if no master existed).

    Raises ParseError if the new source can't be parsed, so a broken file never replaces a working one.
    """
    source = source.replace("\r\n", "\n")
    parse(source)
    backup = None
    if settings.cv_file.exists():
        history_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = history_dir() / f"{settings.cv_file.stem}-{stamp}.tex"
        backup.write_text(settings.cv_file.read_text(encoding="utf-8"), encoding="utf-8")
    settings.cv_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cv_file.write_text(source, encoding="utf-8")
    return backup


def backups() -> list[Path]:
    if not history_dir().exists():
        return []
    return sorted(history_dir().glob("*.tex"), reverse=True)


def escape(text: str) -> str:
    """Plain text to LaTeX: escape specials not already escaped."""
    return re.sub(r"(?<!\\)[&%_#$]", lambda match: _LATEX_SPECIALS[match.group(0)], text.strip())


def _cventry(fields: list[str]) -> str:
    fields = [escape(field) for field in fields]
    for field in fields:
        error = validate_latex(field)
        if error:
            raise ParseError(error)
    return "\\cventry{%s}{%s}{%s}{%s}{%s}{%s}\n" % tuple(fields)


def _insert_in_section(source: str, words: tuple[str, ...], entry: str, label: str) -> str:
    """Insert entry right after the \\section line of the first section whose name contains one of words."""
    masked = _mask_comments(source)
    for match in _SECTION_RE.finditer(masked):
        spans = _read_arguments(masked, match.end(), 1)
        if spans is None:
            continue
        start, end = spans[0]
        if not any(word in _slug(source[start:end]) for word in words):
            continue
        line_end = source.find("\n", end)
        if line_end == -1:
            return source + "\n" + entry
        return source[: line_end + 1] + entry + source[line_end + 1 :]
    raise ParseError(f"No section with '{label}' in its name. Add one in the editor first.")


def add_certification(source: str, name: str, period: str = "", note: str = "") -> str:
    """Insert a \\cventry as the first item of the Certifications section."""
    if not name.strip():
        raise ParseError("Certification name is required")
    entry = _cventry([period, name, note, "", "", ""])
    return _insert_in_section(source, ("certif",), entry, "Certifications")


# --- Work experience -----------------------------------------------------------

EXPERIENCE_WORDS = ("experience", "work", "employment")
# A period is ongoing when it ends with a dash, optionally followed by a word like "Now".
_ONGOING_RE = re.compile(r"(?:--|–|—|-)\s*(?:now|present|current|today|ongoing|oggi|in corso|heute|actualidad)?\s*$", re.IGNORECASE)


def _entries(source: str):
    """Yield (section name, argument spans) for every \\cventry, skipping comments."""
    masked = _mask_comments(source)
    section = ""
    for match in re.finditer(r"\\(section|cventry)(?![A-Za-z])", masked):
        count = 1 if match.group(1) == "section" else 6
        spans = _read_arguments(masked, match.end(), count)
        if spans is None:
            continue
        if match.group(1) == "section":
            section = source[spans[0][0] : spans[0][1]].strip()
        else:
            yield section, spans


def ongoing_jobs(source: str) -> list[dict]:
    """Work entries whose period has no end date yet (e.g. '2022--Now')."""
    jobs = []
    for section, spans in _entries(source):
        if not any(word in _slug(section) for word in EXPERIENCE_WORDS):
            continue
        period = source[spans[0][0] : spans[0][1]]
        if not _ONGOING_RE.search(period):
            continue
        title, employer = (source[start:end].strip() for start, end in spans[1:3])
        jobs.append({"offset": spans[0][0], "period": period, "label": " - ".join(part for part in (title, employer) if part)})
    return jobs


def end_job(source: str, offset: int, period: str, end: str) -> str:
    """Replace the ongoing marker of the job at offset, e.g. '2022--Now' + '2026' -> '2022--2026'.

    offset and period come from ongoing_jobs; a mismatch means the file changed since and nothing is edited.
    """
    end = escape(end)
    if not end:
        raise ParseError("End date is required")
    error = validate_latex(end)
    if error:
        raise ParseError(error)
    job = next((job for job in ongoing_jobs(source) if job["offset"] == offset and job["period"] == period), None)
    if job is None:
        raise ParseError("That job is no longer ongoing in the CV (file changed?). Reload the page.")
    marker = _ONGOING_RE.search(period)
    new_period = period[: marker.start()].rstrip() + "--" + end
    return source[:offset] + new_period + source[offset + len(period) :]


def add_job(source: str, period: str, title: str, employer: str, city: str = "", description: str = "") -> str:
    """Insert a \\cventry as the first item of the work experience section."""
    if not title.strip() or not period.strip():
        raise ParseError("Period and job title are required")
    # Blank lines inside a \cventry argument break LaTeX ("Runaway argument"), so keep it one line.
    entry = _cventry([period, title, employer, city, "", " ".join(description.split())])
    return _insert_in_section(source, EXPERIENCE_WORDS, entry, "Experience")

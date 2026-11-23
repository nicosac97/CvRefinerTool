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


def add_certification(source: str, name: str, period: str = "", note: str = "") -> str:
    """Insert a \\cventry as the first item of the Certifications section."""
    if not name.strip():
        raise ParseError("Certification name is required")
    fields = [escape(period), escape(name), escape(note)]
    for field in fields:
        error = validate_latex(field)
        if error:
            raise ParseError(error)
    masked = _mask_comments(source)
    for match in _SECTION_RE.finditer(masked):
        spans = _read_arguments(masked, match.end(), 1)
        if spans is None:
            continue
        start, end = spans[0]
        if "certif" not in _slug(source[start:end]):
            continue
        line_end = source.find("\n", end)
        insert_at = len(source) if line_end == -1 else line_end + 1
        entry = "\\cventry{%s}{%s}{%s}{}{}{}\n" % tuple(fields)
        if line_end == -1:
            entry = "\n" + entry
        return source[:insert_at] + entry + source[insert_at:]
    raise ParseError("No section with 'Certifications' in its name. Add one in the editor first.")

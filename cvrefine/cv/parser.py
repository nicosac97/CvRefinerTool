"""Split a moderncv LaTeX source into blocks with exact source spans.

Only the span of the editable argument is ever replaced, so everything else in the file
(preamble, contact details, layout, comments) stays byte-identical.
"""

import re
from dataclasses import dataclass, field

# Command name -> number of mandatory brace arguments.
COMMANDS = {"title": 1, "section": 1, "cvitem": 2, "cventry": 6}
EDITABLE_SECTION_WORDS = ("experience", "work", "project", "skill")
_COMMAND_RE = re.compile(r"\\(" + "|".join(COMMANDS) + r")(?![A-Za-z])")
_FORBIDDEN_RE = re.compile(r"\\(input|include|write|immediate|openout|openin|read|catcode|def|edef|gdef|let|newcommand|renewcommand|usepackage|documentclass|begin|end|section)(?![A-Za-z])")


@dataclass
class Block:
    id: str
    kind: str
    section: str
    label: str
    text: str
    start: int
    end: int
    editable: bool
    facts: str = ""


@dataclass
class ParsedCV:
    source: str
    blocks: list[Block] = field(default_factory=list)

    def get(self, block_id: str) -> Block | None:
        return next((block for block in self.blocks if block.id == block_id), None)

    @property
    def editable(self) -> list[Block]:
        return [block for block in self.blocks if block.editable]

    def plain_text(self) -> str:
        """Readable facts, one line per block, for scoring and grounding checks."""
        lines = []
        for block in self.blocks:
            prefix = f"{block.section}: " if block.section else ""
            lines.append(prefix + latex_to_text(block.facts))
        return "\n".join(lines)

    def candidate_summary(self, current_year: int) -> str:
        """plain_text plus a computed experience line, so the decision model can judge seniority."""
        years = experience_years(self, current_year)
        if years is None:
            return self.plain_text()
        return f"Professional experience: about {years} years (since {current_year - years})\n{self.plain_text()}"


_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d\d(?!\d)")
EXPERIENCE_SECTION_WORDS = ("experience", "work", "employment")


def experience_years(cv: ParsedCV, current_year: int) -> int | None:
    """Years since the earliest start date among work entries, or None if no entry has one."""
    starts = []
    for block in cv.blocks:
        section_slug = block.id.split(":")[0]
        if block.kind != "cventry" or not any(word in section_slug for word in EXPERIENCE_SECTION_WORDS):
            continue
        match = _YEAR_RE.search(block.facts.split("|")[0])
        if match:
            starts.append(int(match.group(0)))
    return max(0, current_year - min(starts)) if starts else None


class ParseError(ValueError):
    pass


def parse(source: str) -> ParsedCV:
    masked = _mask_comments(source)
    cv = ParsedCV(source)
    section = ""
    counters: dict[str, int] = {}
    for match in _COMMAND_RE.finditer(masked):
        name = match.group(1)
        spans = _read_arguments(masked, match.end(), COMMANDS[name])
        if spans is None:
            raise ParseError(f"Malformed \\{name} at offset {match.start()}")
        args = [source[start:end] for start, end in spans]
        if name == "section":
            section = args[0].strip()
            continue
        facts = " | ".join(arg.strip() for arg in args if arg.strip())
        section_slug = _slug(section)
        editable = any(word in section_slug for word in EDITABLE_SECTION_WORDS)
        if name == "title":
            block = Block("title", name, "", "Headline", args[0], *spans[0], True, facts)
        elif name == "cvitem" and not section:
            block = Block("summary", name, "", "Summary", args[1], *spans[1], True, facts)
        elif name == "cvitem":
            index = counters.setdefault(section_slug, 0)
            counters[section_slug] += 1
            block_id = f"{section_slug}:{_slug(args[0]) or index}"
            block = Block(block_id, name, section, args[0].strip() or section, args[1], *spans[1], editable, facts)
        else:
            index = counters.setdefault(section_slug, 0)
            counters[section_slug] += 1
            label = " - ".join(part.strip() for part in (args[1], args[2]) if part.strip())
            block = Block(f"{section_slug}:{index}", name, section, label, args[5], *spans[5], editable, facts)
        if cv.get(block.id):
            block.id = f"{block.id}-{match.start()}"
        cv.blocks.append(block)
    return cv


def apply_edits(cv: ParsedCV, replacements: dict[str, str]) -> str:
    """Replace the editable span of each named block. Unknown or locked ids raise."""
    targets = []
    for block_id, text in replacements.items():
        block = cv.get(block_id)
        if block is None or not block.editable:
            raise ParseError(f"Block {block_id!r} is unknown or not editable")
        error = validate_latex(text)
        if error:
            raise ParseError(f"Block {block_id!r}: {error}")
        targets.append((block.start, block.end, text))
    output = cv.source
    for start, end, text in sorted(targets, reverse=True):
        output = output[:start] + text + output[end:]
    return output


def validate_latex(text: str) -> str | None:
    """Reject text that would break the document or run unwanted commands."""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "unbalanced braces"
        elif char == "%":
            return "unescaped % (write \\%)"
        index += 1
    if depth:
        return "unbalanced braces"
    forbidden = _FORBIDDEN_RE.search(text)
    if forbidden:
        return f"command \\{forbidden.group(1)} is not allowed in CV text"
    return None


def latex_to_text(text: str) -> str:
    text = text.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_").replace("\\#", "#").replace("---", "-").replace("--", "-")
    text = re.sub(r"\\[A-Za-z]+\*?", "", text)
    return re.sub(r"[{}]", "", text).strip()


def _mask_comments(source: str) -> str:
    """Blank out comments while keeping every offset unchanged."""
    out = []
    for line in source.splitlines(keepends=True):
        match = re.search(r"(?<!\\)%", line)
        if match:
            ending = line[len(line.rstrip("\r\n")):]
            body = line[: len(line) - len(ending)]
            line = body[: match.start()] + " " * (len(body) - match.start()) + ending
        out.append(line)
    return "".join(out)


def _read_arguments(text: str, position: int, count: int) -> list[tuple[int, int]] | None:
    spans = []
    while len(spans) < count:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return None
        if text[position] == "[" and not spans:
            close = text.find("]", position)
            if close < 0:
                return None
            position = close + 1
            continue
        if text[position] != "{":
            return None
        end = _matching_brace(text, position)
        if end is None:
            return None
        spans.append((position + 1, end))
        position = end + 1
    return spans


def _matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", latex_to_text(text).lower()).strip("-")

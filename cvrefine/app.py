from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from cvrefine.config import settings
from cvrefine.cv.parser import ParsedCV, ParseError, apply_edits, parse, validate_latex
from cvrefine.cv.render import render_pdf, write_output
from cvrefine.cv.letter import assemble as assemble_letter
from cvrefine.cv.letter import covered_requirements, cv_name, letter_facts
from cvrefine.cv.letter import draft as draft_letter
from cvrefine.cv.letter import regenerate as regenerate_letter_paragraph
from cvrefine.cv.tailor import GROUNDED_OK, GROUNDED_WARN, LANGUAGES, detect_language, keyword_coverage, propose
from cvrefine.db import CANDIDATE_KEY, engine, get_session, get_setting, init_db, set_setting
from cvrefine.llm.decisions import DecisionError, get_decision_client
from cvrefine.llm.writer import Writer, WriterError
from cvrefine.models import JOB_STATUSES, WORK_MODE_STRICTNESS, WORK_MODES, CoverLetter, CriteriaProfile, Job, ScanRun, Tailoring
from cvrefine.pipeline.scan import ScanManager
from cvrefine.pipeline.scoring import MAX_DESCRIPTION_CHARS, score_job
from cvrefine.sources.manual import FetchError, fetch_posting
from cvrefine.sources.registry import ALL_SOURCES, DEFAULT_SOURCES

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

SessionDep = Annotated[Session, Depends(get_session)]
JOBS_PAGE_SIZE = 200
AREA_MAX_JOBS = 5
MAX_NOTES_CHARS = 2000


scans = ScanManager(lambda: Session(engine))
SCHEDULER_TICK_MINUTES = 10


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scans.start_due, "interval", minutes=SCHEDULER_TICK_MINUTES, id="scan-due", next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="CVRefineTool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _jobs_error(message: str) -> RedirectResponse:
    return _redirect(f"/jobs?error={quote(message)}")


def _get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _score(session: Session, job: Job) -> None:
    """Score and persist. A backend failure is recorded on the job instead of failing the request."""
    profile = session.get(CriteriaProfile, job.profile_id) if job.profile_id else None
    try:
        score_job(get_decision_client(), get_setting(session, CANDIDATE_KEY), profile, job)
    except DecisionError as error:
        job.score, job.breakdown, job.uncertain, job.score_error = None, [], False, str(error)
    session.add(job)
    session.commit()


@app.get("/")
def index() -> RedirectResponse:
    return _redirect("/monitor")


# --- Monitor -----------------------------------------------------------------


@app.get("/monitor", response_class=HTMLResponse)
def monitor(request: Request, session: SessionDep, edit: int = 0):
    profiles = session.exec(select(CriteriaProfile)).all()
    runs = {
        profile.id: session.exec(select(ScanRun).where(ScanRun.profile_id == profile.id).order_by(ScanRun.started_at.desc()).limit(3)).all()
        for profile in profiles
    }
    running = {profile.id for profile in profiles if scans.is_running(profile.id)}
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "tab": "monitor",
            "profiles": profiles,
            "runs": runs,
            "running": running,
            "editing": session.get(CriteriaProfile, edit) if edit else None,
            "candidate_summary": get_setting(session, CANDIDATE_KEY),
            "work_modes": WORK_MODES,
            "strictness_options": WORK_MODE_STRICTNESS,
            "sources": ALL_SOURCES,
            "default_sources": DEFAULT_SOURCES,
        },
    )


@app.post("/candidate")
def save_candidate(session: SessionDep, summary: Annotated[str, Form()] = ""):
    set_setting(session, CANDIDATE_KEY, summary.strip())
    return _redirect("/monitor")


def _int_field(value: object, default: int | None, low: int = 0, high: int = 10**9) -> int | None:
    text_value = str(value or "").strip()
    return min(high, max(low, int(text_value))) if text_value.isdigit() else default


async def _save_profile(request: Request, session: Session, profile: CriteriaProfile) -> RedirectResponse:
    form = await request.form()
    work_mode = str(form.get("work_mode", "any"))
    if work_mode not in WORK_MODES:
        raise HTTPException(422, "Unknown work mode")
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "Name is required")
    strictness = str(form.get("work_mode_strictness", "strict"))
    if strictness not in WORK_MODE_STRICTNESS:
        raise HTTPException(422, "Unknown work mode strictness")
    profile.name, profile.work_mode, profile.work_mode_strictness = name, work_mode, strictness
    for field_name in ("keywords", "locations", "seniority", "must_have", "nice_to_have", "excluded_companies", "custom_criteria"):
        setattr(profile, field_name, str(form.get(field_name, "")).strip())
    profile.country = str(form.get("country", "")).strip().lower() or "italy"
    profile.min_salary = _int_field(form.get("min_salary"), None)
    profile.scan_interval_hours = _int_field(form.get("scan_interval_hours"), 0, 0, 24 * 7)
    profile.max_age_days = _int_field(form.get("max_age_days"), 14, 1, 90)
    profile.sources = ",".join(name for name in form.getlist("sources") if name in ALL_SOURCES)
    session.add(profile)
    session.commit()
    return _redirect("/monitor")


@app.post("/profiles")
async def create_profile(request: Request, session: SessionDep):
    return await _save_profile(request, session, CriteriaProfile(name=""))


@app.post("/profiles/{profile_id}")
async def update_profile(profile_id: int, request: Request, session: SessionDep):
    profile = session.get(CriteriaProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return await _save_profile(request, session, profile)


@app.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: int, session: SessionDep):
    profile = session.get(CriteriaProfile, profile_id)
    if profile:
        for job in session.exec(select(Job).where(Job.profile_id == profile_id)).all():
            job.profile_id = None
            session.add(job)
        for run in session.exec(select(ScanRun).where(ScanRun.profile_id == profile_id)).all():
            session.delete(run)
        session.delete(profile)
        session.commit()
    return _redirect("/monitor")


@app.post("/profiles/{profile_id}/scan")
def scan_profile(profile_id: int, session: SessionDep):
    if session.get(CriteriaProfile, profile_id) is None:
        raise HTTPException(404, "Profile not found")
    scans.start(profile_id)
    return _redirect("/monitor")


# --- Jobs --------------------------------------------------------------------


@app.get("/jobs", response_class=HTMLResponse)
def jobs(
    request: Request,
    session: SessionDep,
    status: str = "",
    error: str = "",
    profile: str = "",
    source: str = "",
    area: str = "",
    min_score: str = "",
):
    query = select(Job)
    if status in JOB_STATUSES:
        query = query.where(Job.status == status)
    else:
        query = query.where(Job.status != "dismissed")
    if profile.isdigit():
        query = query.where(Job.profile_id == int(profile))
    if source:
        query = query.where(Job.source == source)
    if area:
        query = query.where(Job.area == area)
    if min_score.isdigit():
        query = query.where(Job.score >= int(min_score))
    rows = sorted(session.exec(query).all(), key=lambda job: (job.score is None, -(job.score or 0)))
    profiles = session.exec(select(CriteriaProfile)).all()
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "tab": "jobs",
            "jobs": rows[:JOBS_PAGE_SIZE],
            "total": len(rows),
            "page_size": JOBS_PAGE_SIZE,
            "profiles": profiles,
            "profile_names": {item.id: item.name for item in profiles},
            "filters": {"status": status, "profile": profile, "source": source, "area": area, "min_score": min_score},
            "sources": sorted(set(session.exec(select(Job.source).distinct()).all())),
            "areas": _areas(session),
            "statuses": JOB_STATUSES,
            "error": error,
            "has_candidate": bool(get_setting(session, CANDIDATE_KEY)),
        },
    )


def _areas(session: Session) -> list[str]:
    return sorted(area for area in session.exec(select(Job.area).distinct()).all() if area)


@app.post("/jobs/{job_id}/area")
def set_area(job_id: int, session: SessionDep, area: Annotated[str, Form()] = ""):
    job = _get_job(session, job_id)
    job.area = area.strip()[:80]
    session.add(job)
    session.commit()
    return _redirect(f"/jobs/{job_id}")


@app.post("/jobs/manual")
def add_manual_job(
    session: SessionDep,
    url: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    profile_id: Annotated[str, Form()] = "",
):
    url, description = url.strip(), description.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return _jobs_error("URL must start with http:// or https://")
    if not description:
        if not url:
            return _jobs_error("Paste a description or give a URL.")
        try:
            fetched_title, description = fetch_posting(url)
        except FetchError as fetch_error:
            return _jobs_error(str(fetch_error))
        title = title.strip() or fetched_title
    job = Job(
        source="manual",
        url=url,
        title=title.strip() or "Untitled job",
        company=company.strip(),
        location=location.strip(),
        description=description,
        profile_id=int(profile_id) if profile_id.isdigit() else None,
    )
    _score(session, job)
    return _redirect(f"/jobs/{job.id}")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, session: SessionDep, error: str = ""):
    job = _get_job(session, job_id)
    profile = session.get(CriteriaProfile, job.profile_id) if job.profile_id else None
    source = ALL_SOURCES.get(job.source)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "tab": "jobs",
            "job": job,
            "profile": profile,
            "areas": _areas(session),
            "attribution": source.attribution if source else "",
            "error": error,
            **_letter_context(session, [job], _letter_source(session, job.id, None)),
        },
    )


def _letter_context(session: Session, jobs: list[Job], tailoring: Tailoring | None) -> dict:
    """What the optional cover letter form needs: target jobs, the CV it will use, previous letters."""
    job_ids = [job.id for job in jobs]
    letters = session.exec(select(CoverLetter).where(CoverLetter.job_id.in_(job_ids)).order_by(CoverLetter.created_at.desc())).all()
    return {"letter_jobs": jobs, "letter_tailoring": tailoring, "letters": letters, "languages": LANGUAGES, "job_titles_by_id": {job.id: job.title for job in jobs}}


@app.post("/jobs/{job_id}/status")
def set_status(job_id: int, session: SessionDep, status: Annotated[str, Form()]):
    if status not in JOB_STATUSES:
        raise HTTPException(422, "Unknown status")
    job = _get_job(session, job_id)
    job.status = status
    session.add(job)
    session.commit()
    return _redirect("/jobs" if status == "dismissed" else f"/jobs/{job_id}")


@app.post("/jobs/{job_id}/feedback")
def set_feedback(job_id: int, session: SessionDep, value: Annotated[int, Form()]):
    if value not in (-1, 1):
        raise HTTPException(422, "Feedback must be 1 or -1")
    job = _get_job(session, job_id)
    job.feedback = None if job.feedback == value else value
    session.add(job)
    session.commit()
    return _redirect(f"/jobs/{job_id}")


@app.post("/jobs/{job_id}/rescore")
def rescore(job_id: int, session: SessionDep):
    _score(session, _get_job(session, job_id))
    return _redirect(f"/jobs/{job_id}")


# --- Tailor ------------------------------------------------------------------


def _load_cv() -> tuple[ParsedCV | None, str]:
    if not settings.cv_file.exists():
        return None, f"No CV at {settings.cv_file}"
    try:
        return parse(settings.cv_file.read_text(encoding="utf-8")), ""
    except (OSError, ParseError) as error:
        return None, f"Could not parse {settings.cv_file.name}: {error}"


def _get_tailoring(session: Session, tailoring_id: int) -> Tailoring:
    tailoring = session.get(Tailoring, tailoring_id)
    if tailoring is None:
        raise HTTPException(404, "Tailoring not found")
    return tailoring


@app.post("/candidate/from-cv")
def candidate_from_cv(session: SessionDep):
    cv, _ = _load_cv()
    if cv:
        set_setting(session, CANDIDATE_KEY, cv.candidate_summary(datetime.now(timezone.utc).year))
    return _redirect("/monitor")


@app.get("/tailor", response_class=HTMLResponse)
def tailor(request: Request, session: SessionDep, error: str = ""):
    cv, cv_error = _load_cv()
    shortlisted = session.exec(select(Job).where(Job.status == "shortlisted")).all()
    sessions = session.exec(select(Tailoring).order_by(Tailoring.created_at.desc())).all()
    job_titles = {job.id: job.title for job in session.exec(select(Job)).all()}
    area_counts = {area: len(_area_jobs(session, area)) for area in _areas(session)}
    return templates.TemplateResponse(
        request,
        "tailor.html",
        {
            "tab": "tailor",
            "cv": cv,
            "cv_error": cv_error,
            "cv_name": settings.cv_file_name,
            "shortlisted": shortlisted,
            "sessions": sessions,
            "job_titles": job_titles,
            "area_counts": area_counts,
            "error": error,
        },
    )


def _area_jobs(session: Session, area: str) -> list[Job]:
    """The area's best-scored, non-dismissed jobs."""
    jobs = session.exec(select(Job).where(Job.area == area, Job.status != "dismissed")).all()
    return sorted(jobs, key=lambda job: -(job.score or 0))[:AREA_MAX_JOBS]


def _area_target(area: str, jobs: list[Job]) -> Job:
    """Combine several postings into one tailoring target, giving each an equal share of the context."""
    share = MAX_DESCRIPTION_CHARS // max(1, len(jobs)) - 200
    parts = [f"--- Posting {index + 1}: {job.title} at {job.company or 'unknown'} ---\n{job.description[:share]}" for index, job in enumerate(jobs)]
    return Job(title=f"Job area '{area}': {len(jobs)} similar postings, tailor for what they share", description="\n\n".join(parts))


def _create_tailoring(session: Session, target: Job, job_id: int, area: str = "") -> RedirectResponse:
    cv, cv_error = _load_cv()
    if cv is None:
        return _redirect(f"/tailor?error={quote(cv_error)}")
    try:
        decisions = get_decision_client()
    except DecisionError:
        decisions = None
    try:
        result = propose(cv, target, Writer(), decisions)
    except WriterError as error:
        return _redirect(f"/tailor?error={quote(str(error))}")
    tailoring = Tailoring(job_id=job_id, area=area, **result)
    if decisions is None:
        tailoring.grounding_error = "Decision model unavailable; edits were not checked."
    session.add(tailoring)
    session.commit()
    return _redirect(f"/tailor/{tailoring.id}")


@app.post("/tailor/job/{job_id}")
def start_tailoring(job_id: int, session: SessionDep):
    job = _get_job(session, job_id)
    return _create_tailoring(session, job, job.id)


@app.post("/tailor/area")
def start_area_tailoring(session: SessionDep, area: Annotated[str, Form()]):
    jobs = _area_jobs(session, area)
    if not jobs:
        return _redirect(f"/tailor?error={quote('No open jobs in area ' + area)}")
    return _create_tailoring(session, _area_target(area, jobs), jobs[0].id, area)


@app.get("/tailor/{tailoring_id}", response_class=HTMLResponse)
def review_tailoring(tailoring_id: int, request: Request, session: SessionDep):
    tailoring = _get_tailoring(session, tailoring_id)
    job = session.get(Job, tailoring.job_id)
    cv, _ = _load_cv()
    coverage_before = keyword_coverage(cv.source, tailoring.keywords) if cv else []
    coverage_after = None
    tex_path = Path(tailoring.output_dir) / settings.cv_file_name if tailoring.output_dir else None
    if tex_path and tex_path.exists():
        coverage_after = keyword_coverage(tex_path.read_text(encoding="utf-8"), tailoring.keywords)
    pdf_ready = bool(tex_path and tex_path.with_suffix(".pdf").exists())
    letter_jobs = _area_jobs(session, tailoring.area) if tailoring.area else [job] if job else []
    return templates.TemplateResponse(
        request,
        "tailor_review.html",
        {
            "tab": "tailor",
            "t": tailoring,
            "job": job,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "tex_ready": bool(tex_path and tex_path.exists()),
            "pdf_ready": pdf_ready,
            "grounded_ok": GROUNDED_OK,
            "grounded_warn": GROUNDED_WARN,
            **_letter_context(session, letter_jobs, tailoring if tailoring.applied_at else None),
        },
    )


@app.post("/tailor/{tailoring_id}")
async def apply_tailoring(tailoring_id: int, request: Request, session: SessionDep):
    tailoring = _get_tailoring(session, tailoring_id)
    form = await request.form()
    cv, cv_error = _load_cv()
    if cv is None:
        return _redirect(f"/tailor?error={quote(cv_error)}")

    edits = [dict(edit) for edit in tailoring.edits]
    replacements, problems = {}, []
    for index, edit in enumerate(edits):
        edit["status"] = "accepted" if form.get(f"decision_{index}") == "accepted" else "rejected"
        edit["final"] = str(form.get(f"text_{index}", edit["final"])).strip()
        if edit["status"] != "accepted":
            continue
        latex_error = validate_latex(edit["final"])
        block = cv.get(edit["block_id"])
        if latex_error or block is None:
            problems.append(f"{edit['label']}: {latex_error or 'block no longer in CV'}")
            edit["status"] = "rejected"
        elif block.text != edit["original"]:
            problems.append(f"{edit['label']}: master CV changed since proposal, edit skipped")
            edit["status"] = "rejected"
        else:
            replacements[edit["block_id"]] = edit["final"]
    tailoring.edits = edits

    out_dir = settings.tailored_dir / f"job{tailoring.job_id}-t{tailoring.id}"
    stale_pdf = out_dir / Path(settings.cv_file_name).with_suffix(".pdf").name
    stale_pdf.unlink(missing_ok=True)
    tex_path = write_output(apply_edits(cv, replacements), out_dir)
    _, render_error = await run_in_threadpool(render_pdf, tex_path)
    tailoring.output_dir = str(out_dir)
    tailoring.render_error = "\n".join([*problems, render_error]).strip()
    tailoring.applied_at = datetime.now(timezone.utc)
    session.add(tailoring)
    session.commit()
    return _redirect(f"/tailor/{tailoring.id}")


# --- Cover letters (optional, only on request) ------------------------------


def _get_letter(session: Session, letter_id: int) -> CoverLetter:
    letter = session.get(CoverLetter, letter_id)
    if letter is None:
        raise HTTPException(404, "Cover letter not found")
    return letter


def _letter_source(session: Session, job_id: int, tailoring_id: int | None) -> Tailoring | None:
    """The applied tailoring to write from: the given one, else the job's latest applied one."""
    if tailoring_id:
        tailoring = session.get(Tailoring, tailoring_id)
        return tailoring if tailoring and tailoring.applied_at else None
    query = select(Tailoring).where(Tailoring.job_id == job_id, Tailoring.applied_at.is_not(None)).order_by(Tailoring.applied_at.desc())
    return session.exec(query).first()


def _letter_facts(cv: ParsedCV, tailoring: Tailoring | None) -> str:
    tex_path = Path(tailoring.output_dir) / settings.cv_file_name if tailoring and tailoring.output_dir else None
    tailored = tex_path.read_text(encoding="utf-8") if tex_path and tex_path.exists() else None
    return letter_facts(cv, tailored, datetime.now(timezone.utc).year)


def _optional_decisions():
    try:
        return get_decision_client()
    except DecisionError:
        return None


@app.post("/letters")
def create_letter(
    session: SessionDep,
    job_id: Annotated[int, Form()],
    tailoring_id: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "auto",
    notes: Annotated[str, Form()] = "",
):
    job = _get_job(session, job_id)
    cv, cv_error = _load_cv()
    if cv is None:
        return _redirect(f"/jobs/{job.id}?error={quote(cv_error)}")
    tailoring = _letter_source(session, job.id, int(tailoring_id) if tailoring_id.isdigit() else None)
    decisions = _optional_decisions()
    if language not in LANGUAGES:
        try:
            language = detect_language(job, decisions) if decisions else "en"
        except DecisionError:
            language = "en"
    notes = notes.strip()[:MAX_NOTES_CHARS]
    try:
        result = draft_letter(_letter_facts(cv, tailoring), job, notes, language, Writer(), decisions)
    except WriterError as error:
        return _redirect(f"/jobs/{job.id}?error={quote(str(error))}")
    letter = CoverLetter(job_id=job.id, tailoring_id=tailoring.id if tailoring else None, language=language, notes=notes, **result)
    session.add(letter)
    session.commit()
    return _redirect(f"/letters/{letter.id}")


@app.get("/letters/{letter_id}", response_class=HTMLResponse)
def review_letter(letter_id: int, request: Request, session: SessionDep, error: str = ""):
    letter = _get_letter(session, letter_id)
    job = session.get(Job, letter.job_id)
    covered, missing = covered_requirements(letter.requirements, letter.paragraphs)
    output = Path(letter.output_path) if letter.output_path else None
    return templates.TemplateResponse(
        request,
        "letter_review.html",
        {
            "tab": "jobs",
            "letter": letter,
            "job": job,
            "language_name": LANGUAGES.get(letter.language, letter.language),
            "covered": covered,
            "missing": missing,
            "saved_text": output.read_text(encoding="utf-8") if output and output.exists() else "",
            "grounded_ok": GROUNDED_OK,
            "grounded_warn": GROUNDED_WARN,
            "error": error,
        },
    )


def _letter_from_form(letter: CoverLetter, form) -> None:
    paragraphs = [dict(paragraph) for paragraph in letter.paragraphs]
    for index, paragraph in enumerate(paragraphs):
        paragraph["status"] = "kept" if form.get(f"decision_{index}") == "kept" else "dropped"
        paragraph["final"] = str(form.get(f"text_{index}", paragraph["final"])).strip()
    letter.paragraphs = paragraphs


@app.post("/letters/{letter_id}")
async def save_letter(letter_id: int, request: Request, session: SessionDep):
    letter = _get_letter(session, letter_id)
    job = _get_job(session, letter.job_id)
    _letter_from_form(letter, await request.form())
    cv, cv_error = _load_cv()
    name = cv_name(cv.source) if cv else ""
    tailoring = session.get(Tailoring, letter.tailoring_id) if letter.tailoring_id else None
    out_dir = Path(tailoring.output_dir) if tailoring and tailoring.output_dir else settings.tailored_dir / f"job{job.id}-letter{letter.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"cover_letter-{letter.id}.md"
    output.write_text(assemble_letter(letter.paragraphs, job, name, letter.language, datetime.now(timezone.utc).date()), encoding="utf-8")
    letter.output_path, letter.saved_at = str(output), datetime.now(timezone.utc)
    session.add(letter)
    session.commit()
    suffix = f"?error={quote(cv_error + ' (letter saved without your name)')}" if cv is None else ""
    return _redirect(f"/letters/{letter.id}{suffix}")


@app.post("/letters/{letter_id}/paragraph/{index}")
async def regenerate_paragraph(letter_id: int, index: int, request: Request, session: SessionDep):
    letter = _get_letter(session, letter_id)
    if not 0 <= index < len(letter.paragraphs):
        raise HTTPException(404, "Paragraph not found")
    job = _get_job(session, letter.job_id)
    form = await request.form()
    _letter_from_form(letter, form)  # keep edits made to the other paragraphs
    cv, cv_error = _load_cv()
    if cv is None:
        session.add(letter)
        session.commit()
        return _redirect(f"/letters/{letter.id}?error={quote(cv_error)}")
    tailoring = session.get(Tailoring, letter.tailoring_id) if letter.tailoring_id else None
    instruction = str(form.get(f"instruction_{index}", ""))[:MAX_NOTES_CHARS]
    try:
        paragraph, grounding_error = regenerate_letter_paragraph(
            letter.paragraphs, index, instruction, _letter_facts(cv, tailoring), job, letter.notes, letter.language, Writer(), _optional_decisions()
        )
    except WriterError as error:
        session.add(letter)
        session.commit()
        return _redirect(f"/letters/{letter.id}?error={quote(str(error))}")
    paragraphs = list(letter.paragraphs)
    paragraphs[index] = paragraph
    letter.paragraphs = paragraphs
    letter.grounding_error = grounding_error
    session.add(letter)
    session.commit()
    return _redirect(f"/letters/{letter.id}#p{index}")


@app.get("/letters/{letter_id}/file")
def letter_file(letter_id: int, session: SessionDep):
    letter = _get_letter(session, letter_id)
    path = Path(letter.output_path) if letter.output_path else None
    if path is None or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=f"cover-letter-job{letter.job_id}.md", media_type="text/markdown")


@app.get("/tailor/{tailoring_id}/file/{kind}")
def tailored_file(tailoring_id: int, kind: str, session: SessionDep):
    tailoring = _get_tailoring(session, tailoring_id)
    suffix = {"tex": ".tex", "pdf": ".pdf"}.get(kind)
    if suffix is None or not tailoring.output_dir:
        raise HTTPException(404)
    path = (Path(tailoring.output_dir) / settings.cv_file_name).with_suffix(suffix)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=f"cv-job{tailoring.job_id}{suffix}")

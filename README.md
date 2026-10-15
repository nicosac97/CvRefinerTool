# CvRefinerTool

Personal tool to search for jobs and tailor my CV when good matches come out.

It runs locally as a small web app with three tabs:

1. **Monitor**: save search criteria, scan job boards, and score every new posting against your profile.
2. **Jobs**: browse ranked results, shortlist or dismiss them, group them into "areas", and label good or bad matches.
3. **Tailor**: get proposed edits to your LaTeX CV for one job or a whole area, then accept, edit or reject each one. Optionally, write a cover letter for a specific job.

All data stays on your machine in `data/`. The app sends only the text it needs to OpenRouter: CV facts without contact details, job postings, and your optional notes.

---

## How it works

```
job boards ──► normalize ──► dedupe ──► hard filters ──► Jev scoring ──► Jobs list
                                                                            │
                         master CV (LaTeX, never modified) ◄────────────────┤
                                    │                                       │
                     writer model proposes edits ──► Jev fact check ──► review ──► tailored .tex / .pdf
                                    │
                     (optional) cover letter ──► Jev fact check per paragraph ──► review ──► .md
```

There are two kinds of model, both reached through [OpenRouter](https://openrouter.ai):

| Role | Default model | Used for |
|---|---|---|
| Decision model | `typesafe/jev-1.13` ([Jev](https://openrouter.ai/typesafe/jev-1.13)) | Typed answers with probabilities: job scoring, work-mode detection, duplicate checks, posting language, and fact-checking every CV edit and letter paragraph |
| Writer model | `anthropic/claude-sonnet-5.5` | Text: CV edit proposals, requirement and keyword extraction, cover letters |

Jev doesn't write text. It answers three kinds of question: yes/no with a probability, pick one of several labels, and rate on a scale. Each answer comes with its probabilities. It's cheap: scoring one job costs about $0.00004, so scoring hundreds of jobs per scan costs cents.

### Scoring

For every new job, Jev receives your candidate summary and the posting. It answers:

| Question | Type | Effect |
|---|---|---|
| Work mode (remote / hybrid / onsite / unclear) | choice | Multiplier when the profile wants a specific mode. The penalty is set per profile: strict (down to 20%), soft (60%) or off |
| Skills fit (5 levels) | score | 70% of the base score: the probability that you cover *most or all* required skills |
| Seniority fit (3 levels) | score | 30% of the base score |
| Location compatible | yes/no | Multiplier, floor 0.2 (only if the profile has locations) |
| Must-have stack | yes/no | Multiplier, floor 0.5 (only if the profile has must-haves) |
| Each custom criterion | yes/no | Multiplier, floor 0.5 |

```
score = 100 × (0.7 × P(most/all skills) + 0.3 × seniority/2) × work-mode × location × must-have × custom…
```

The job page shows every answer, its confidence, and its effect on the score. Read scores as a **ranking**, not a percentage: with a strict profile, even good matches often score 30–50.

### Sources

| Source | How | Notes |
|---|---|---|
| RemoteOK | JSON API | Fetched at most every 3h; attribution link required |
| Remotive | JSON API | At most every 6h (their limit is 4 calls a day); attribution link required |
| Arbeitnow | JSON API | 3 pages, every 3h |
| We Work Remotely | RSS | DevOps/sysadmin, back-end and full-stack categories |
| Indeed | [python-jobspy](https://github.com/speedyapply/JobSpy) | Per-profile search; country is set per profile |
| Glassdoor | python-jobspy | Often blocked (403); the block is reported as a source error |
| LinkedIn | python-jobspy | **Off by default.** Scraping goes against LinkedIn's terms of service |
| Manual | Paste text or a URL | Always works |

Remote boards are fetched broadly, cached, and filtered locally by each profile's keywords. Duplicates across sites are caught by normalized company and title, fuzzy title matching, and a Jev "same position?" check for ambiguous pairs. One failing source never stops a scan.

### CV tailoring

- Your CV must be a **moderncv** LaTeX file. The parser reads `\title`, `\section`, `\cvitem` and `\cventry`.
- **Editable blocks:** the headline, the summary, and entries in sections whose name contains *experience*, *work*, *project* or *skill*. Everything else (education, certifications, languages, preamble, contact details) is locked.
- **What the writer may do:** rephrase, reorder, emphasize or shorten. It must never add skills, employers, dates or metrics. Skill items never move between proficiency lines.
- **Rejected automatically:** edits that break LaTeX, target locked blocks, or grow the text by more than 30%.
- **Fact check:** Jev asks of every edit, "is every fact also stated in the original CV?" Edits below 65% start as rejected, and red badges mark likely fabrications. In live tests, faithful edits scored 0.70–0.93 and fabricated ones 0.01–0.27.
- **Applying:** writes a copy to `data/cv/tailored/job<id>-t<id>/` with your CV's assets. If a LaTeX engine is installed, it compiles a PDF as well.
- **Areas:** you can tailor for an area, a group of similar jobs (for example "AWS platform"). The writer then targets the requirements of the area's top 5 jobs together.

### Cover letters (optional)

Many applications don't ask for a letter, so nothing runs until you click **Write cover letter** on a job page or a tailoring page.

- **Facts:** the letter uses your tailored CV if that job has an applied tailoring, otherwise the master CV, plus optional **notes** with true facts the CV doesn't contain (why this company, availability, a referral).
- **Language:** detected from the posting (English, Italian, German, Spanish), or chosen by you.
- **Fact check:** Jev checks each paragraph against your CV and notes. In tests, invented claims scored 0.02–0.05 and faithful paragraphs 0.52–0.95. Paragraphs below 0.4 start as dropped, and 0.4–0.65 gets a "check facts" badge.
- **Regenerate:** rewrites one paragraph, with an optional instruction. If you ask the writer to invent something, it refuses and tells you in a note; the refusal never appears in the letter.
- **Saving:** the date, subject, greeting, sign-off and your name (read from `\firstname`/`\familyname` or `\name`) are added locally. The letter is saved as Markdown, with copy and download buttons.

---

## Setup

Requirements:
- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- An [OpenRouter](https://openrouter.ai) API key.
- Optional: a LaTeX engine for PDF output. Any of these works, checked in this order: [tectonic](https://tectonic-typesetting.github.io/), `latexmk` or `pdflatex`. Without one, you get the `.tex` file only.

```powershell
git clone https://github.com/nicosac97/CvRefinerTool.git
cd CvRefinerTool
uv sync
copy .env.example .env      # cp on macOS/Linux
```

Put your key in `.env`:

```
CVREFINE_OPENROUTER_API_KEY=sk-or-v1-...
```

The app reads **only** `CVREFINE_OPENROUTER_API_KEY`. A machine-wide `OPENROUTER_API_KEY` used by other projects is ignored, so the two never mix. `.env` is gitignored.

Put your CV in `data/cv/`:

```
data/cv/main.tex        # moderncv source (never modified by the tool)
data/cv/cvimage.jpeg    # any assets it uses (photo, .cls/.sty), copied next to tailored output
```

Check the key and the Jev connection (optional; makes one live call costing a fraction of a cent):

```powershell
uv run python -m scripts.jev_smoke
```

## Run

From the project folder:

```powershell
uv run uvicorn cvrefine.app:app --reload
```

Open http://127.0.0.1:8000.

Steps:
1. **Monitor → Candidate profile**: click *Replace with text extracted from CV*. This builds the summary used for scoring, including a computed "about N years" experience line. Edit it if needed.
2. **Monitor → Criteria profiles**: add a profile with keywords, work mode and penalty, locations, must-have and nice-to-have stack, minimum salary, excluded companies, custom criteria (one per line), sources, country, maximum job age, and an automatic scan interval. Use **Edit** to change it later.
3. Click **Scan now**. The page refreshes while the scan runs and then shows per-source stats: fetched, matched, new, duplicates, filtered, error.
4. **Jobs**: filter by profile, source, area or minimum score. Open a job to see the score breakdown, shortlist it, set an area, label it Good or Bad match, and click **Tailor CV** or **Write cover letter**.
5. **Tailor**: review the edits, then **Apply accepted edits** and download `.tex` or `.pdf`.

Automatic scans run only while the app is running. A scheduler checks every 10 minutes for profiles that are due.

## Configuration

All settings use the `CVREFINE_` prefix and can go in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `CVREFINE_OPENROUTER_API_KEY` | (empty) | Required for scoring, tailoring and letters |
| `CVREFINE_DECISION_BACKEND` | `jev` | `jev`, or `chat` to use the writer model with JSON output as a fallback (self-reported probabilities, less reliable) |
| `CVREFINE_DECISION_MODEL` | `typesafe/jev-1.13` | Decision model id |
| `CVREFINE_WRITER_MODEL` | `anthropic/claude-sonnet-5.5` | Any OpenRouter chat model with structured JSON output |
| `CVREFINE_DATA_DIR` | `./data` | Database, CV and outputs. Point it elsewhere for a throwaway test |
| `CVREFINE_CV_FILE_NAME` | `main.tex` | CV file name inside `data/cv/` |

## Tuning scores

Label jobs with **Good match** or **Bad match** on their detail page. After about 10–20 labels, run:

```powershell
uv run python -m scripts.eval_scores
```

It prints the mean score for good and bad jobs, the AUC (how often a good job outscores a bad one), the best score cut-off by F1, and how many "uncertain" jobs that cut-off misclassifies.

## Costs

The prices below are approximate, for the default models:

| Action | Calls | Cost |
|---|---|---|
| Score one job | 1 Jev | ~$0.00004 |
| Full scan (~100 new jobs) | ~100 Jev | ~$0.005–0.01 |
| Tailor CV for one job | 1 writer + 1 Jev | ~$0.02–0.04 |
| Cover letter | 1 writer + 2 Jev | ~$0.02 |
| Regenerate one paragraph | 1 writer + 1 Jev | ~$0.007 |

## Project layout

```
cvrefine/
  app.py              FastAPI routes and the scan scheduler
  config.py           Settings (.env, CVREFINE_ prefix)
  db.py, models.py    SQLite via SQLModel; adds new columns to old databases on start
  llm/decisions.py    Decision client interface: Jev (typesafe-sdk) and chat fallback
  llm/writer.py       Writer model: CV edits, cover letters, paragraph rewrites
  pipeline/           normalize, scan (fetch, dedupe, filter, score), scoring rules
  sources/            remote boards, jobspy sites, manual URL fetch, registry
  cv/parser.py        moderncv parser with exact source spans; LaTeX validation
  cv/tailor.py        edit proposals, fact checks, language detection
  cv/letter.py        cover letter drafting, regeneration, assembly
  cv/render.py        tailored output and PDF compile
  templates/, static/ Jinja2 pages, no JavaScript build step
scripts/              jev_smoke.py (live check), eval_scores.py (threshold tuning)
tests/                pytest suite; model calls are faked
data/                 your CV, database and outputs (gitignored)
```

## Tests

```powershell
uv run pytest
```

The tests use fake decision and writer clients plus an in-memory database, so they need no API key and make no network calls.

## Privacy and terms

- `.env` (API key) and `data/` (CV, database, tailored CVs, letters) are gitignored.
- Contact details (address, phone, email, links) are never sent to the models; scoring and tailoring use the CV *facts* only. The CV body itself does go to OpenRouter and the model providers behind it.
- Job postings are treated as untrusted input: the writer is told never to follow instructions found inside them.
- The master CV is never modified. Edits replace only the exact source span of an editable block, in a copy.
- Glassdoor and LinkedIn scraping goes against their terms of service, and both sites block it often. LinkedIn is off unless you tick it on a profile. The tool runs guest searches only, at low volume: no login and no proxy rotation.
- RemoteOK and Remotive require a link back to the original posting. The Jobs list and the job page show one.

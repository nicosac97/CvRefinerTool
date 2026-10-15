"""Write a tailored CV next to copies of its assets and compile it to PDF when a LaTeX engine exists."""

import shutil
import subprocess
from pathlib import Path

from cvrefine.config import settings

TIMEOUT_SECONDS = 300


def write_output(source: str, out_dir: Path) -> Path:
    """Copy the CV folder's assets (photo, .cls/.sty) and write the tailored source. Master is never touched."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for asset in settings.cv_dir.iterdir():
        if asset.is_file() and asset.name != settings.cv_file_name:
            shutil.copy2(asset, out_dir / asset.name)
    tex_path = out_dir / settings.cv_file_name
    tex_path.write_text(source, encoding="utf-8")
    return tex_path


def find_engine() -> list[str] | None:
    if shutil.which("tectonic"):
        return ["tectonic", "--keep-logs"]
    if shutil.which("latexmk"):
        return ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error"]
    if shutil.which("pdflatex"):
        return ["pdflatex", "-interaction=nonstopmode", "-halt-on-error"]
    return None


def render_pdf(tex_path: Path) -> tuple[Path | None, str]:
    """Return (pdf path, error message). Error is empty on success."""
    engine = find_engine()
    if engine is None:
        return None, "No LaTeX engine found (tectonic, latexmk or pdflatex on PATH). The .tex is ready; install one to get a PDF."
    try:
        result = subprocess.run([*engine, tex_path.name], cwd=tex_path.parent, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"{engine[0]} timed out after {TIMEOUT_SECONDS}s"
    pdf_path = tex_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        return None, f"{engine[0]} failed:\n" + "\n".join(tail)
    return pdf_path, ""

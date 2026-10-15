"""Manual job entry: pasted description, or a URL whose main text is extracted."""

import httpx
import trafilatura

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CVRefineTool/0.1"


class FetchError(RuntimeError):
    pass


def fetch_posting(url: str) -> tuple[str, str]:
    """Return (title, description text) for a job posting URL."""
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise FetchError(f"Could not fetch {url}: {error}") from error
    text = trafilatura.extract(response.text, include_comments=False) or ""
    if not text.strip():
        raise FetchError(f"No readable text at {url} (login wall or script-rendered page). Paste the description instead.")
    metadata = trafilatura.extract_metadata(response.text)
    return (metadata.title if metadata and metadata.title else "", text)

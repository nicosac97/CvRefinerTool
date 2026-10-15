from cvrefine.sources.base import FeedSource, SearchSource
from cvrefine.sources.jobspy_source import JobSpySite
from cvrefine.sources.remote_boards import Arbeitnow, RemoteOK, Remotive, WeWorkRemotely

LINKEDIN_WARNING = "Scraping LinkedIn breaks its terms of service; it rate-limits and blocks quickly. Use sparingly."

FEEDS: dict[str, FeedSource] = {source.name: source for source in (RemoteOK(), Remotive(), Arbeitnow(), WeWorkRemotely())}
SEARCHES: dict[str, SearchSource] = {
    source.name: source
    for source in (
        JobSpySite("indeed", "Indeed"),
        JobSpySite("glassdoor", "Glassdoor", "Glassdoor blocks scrapers often; failures are expected."),
        JobSpySite("linkedin", "LinkedIn", LINKEDIN_WARNING),
    )
}
ALL_SOURCES = {**FEEDS, **SEARCHES}
DEFAULT_SOURCES = ("remoteok", "remotive", "arbeitnow", "weworkremotely", "indeed")

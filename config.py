"""Configuration for the Agoda Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT         port the API listens on (default 8000)
    AGODA_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                 (default: none — direct). Agoda's autocomplete, search,
                 hotel and review APIs answered plain requests from cloud
                 servers and home connections alike, so you very likely
                 don't need this. Set it only if you start seeing failures
                 at high volume.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request). A retry
# opens a fresh connection.
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

AGODA_PROXY = os.environ.get("AGODA_PROXY") or None


def agoda_proxy():
    """The exit every Agoda request leaves through. None = your own connection."""
    return os.environ.get("AGODA_PROXY") or None

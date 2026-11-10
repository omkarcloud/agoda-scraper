"""Cache TTL per /agoda/* endpoint (cache.py, keyed on the validated
params). Search and details carry live availability and prices for a
stay, so they are the short ones; the id mappings and reviews are stable.
"""
from datetime import timedelta

# Destination / hotel ids never change; the suggestion ranking barely does.
# Also serves the name -> destination step of hotels/search.
AUTOCOMPLETE_CACHE = timedelta(days=30)
# A hotel page's slug -> property id mapping (one page fetch) is permanent.
HOTEL_ID_CACHE = timedelta(days=30)
# Search results: live prices and availability for the dates.
SEARCH_CACHE = timedelta(hours=6)
# The property record with its room grid for a stay: rates are the most
# perishable thing here; the static content rides along.
DETAILS_CACHE = timedelta(hours=3)
# A review page only grows; a new review waits at most a day.
REVIEWS_CACHE = timedelta(hours=24)


def reviews_ttl(data, result):
    """REVIEWS_CACHE for a page with reviews; an EMPTY page is never kept —
    it may be the site's transient empty answer (fetch.EMPTY_PAGE_RETRIES),
    and a day-long cached empty page would hide every review of the hotel."""
    return REVIEWS_CACHE if (result or {}).get("reviews") else None
# The review summary (score, grades, per-source counts, tags) moves with
# the same cadence; details + reviews share one row per hotel.
REVIEW_SUMMARY_CACHE = timedelta(hours=24)

"""The /agoda/* endpoint functions (route_glue's route_table calls them
with the schema's loaded kwargs).

Shared, cached pieces: the autocomplete payload (also the name ->
destination step of hotels/search), the hotel slug -> property id mapping
(one page fetch, 30 days) and the review summary (HotelReviews: score,
grades, per-source counts, topic tags AND the hotel's page URL — the only
id -> link mapping the site offers), which details and reviews both read,
so a client hitting both on one hotel pays for that call once.
"""
import math

from cache import cached_call
from agoda import cache_config as ttl
from agoda import parsers, refs
from agoda.fetch import (AgodaNotFound, fetch_autocomplete, fetch_details, fetch_hotel_id, fetch_review_summary,
                         fetch_reviews, fetch_search)

NS = "agoda"


def _nights(checkin, checkout):
    from datetime import date
    return (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days


def _stay(checkin, checkout, adults, children, rooms):
    return {"checkin": checkin, "checkout": checkout, "nights": _nights(checkin, checkout),
            "adults": adults, "children": children or [], "rooms": rooms}


# ---- places/autocomplete -----------------------------------------------------------------

def _suggest(query, language):
    return parsers.parse_autocomplete(fetch_autocomplete(query, language))


def _suggestions(query, language):
    return cached_call(f"{NS}.autocomplete", {"query": query, "language": language}, _suggest, ttl.AUTOCOMPLETE_CACHE)


def places_autocomplete(query, language):
    """Find Agoda destinations and hotels as you type: cities, areas,
    regions, landmarks and properties, each with its `location` key for
    /hotels/search, hotel count, country, coordinates and agoda.com link."""
    results = _suggestions(query, language)
    return {"query": query, "language": language, "count": len(results), "results": results}


# ---- the shared id mappings -----------------------------------------------------------------

def _path_to_id(path):
    return fetch_hotel_id(path)


def _hotel_id(hotel):
    """The resolved `hotel` param (digits or a hotel page path) -> int id."""
    if hotel.isdigit():
        return int(hotel)
    return cached_call(f"{NS}.hotel_id", {"path": hotel}, _path_to_id, ttl.HOTEL_ID_CACHE)


def _review_summary(hotel_id, language):
    return parsers.parse_review_summary(fetch_review_summary(hotel_id, refs.REVIEW_SOURCES["all"], language))


# .v2 (2026-11-10): summary tags gained their `id` (the topic filter needs
# it), details embed those tags, and v1 review rows held cached EMPTY pages
# (fetch.EMPTY_PAGE_RETRIES) — so none of the v1 rows is read again.
def _summary(hotel_id, language):
    """The parsed HotelReviews summary (raises AgodaNotFound for an unknown id)."""
    return cached_call(f"{NS}.review_summary.v2", {"hotel_id": hotel_id, "language": language}, _review_summary,
                       ttl.REVIEW_SUMMARY_CACHE)


# ---- hotels/search ----------------------------------------------------------------------------

def _destination(location, language):
    """The resolved `location` param -> {kind, id, name, city_id}; a name
    goes through the site's own autocomplete."""
    kind = location.get("kind")
    if kind == "name":
        rows = [r for r in _suggestions(location["name"], language) if r.get("type") in refs.SEARCHABLE_KINDS]
        row = refs.best_suggestion(rows, location["name"])
        if row is None:
            raise AgodaNotFound(f"no Agoda destination matches {location['name']!r}")
        return {"kind": row["type"], "id": row["id"], "name": row.get("name"), "city_id": row.get("city_id")}
    if kind == "hotel_path":
        return {"kind": "hotel", "id": _hotel_id(location["path"]), "name": None, "city_id": None}
    # a search link carries the city next to an area key (areaSearch wants both)
    return {"kind": kind, "id": location.get("id"), "name": location.get("name"), "city_id": location.get("city_id")}


def _filters(data):
    """The public filter params -> the site's idsFilters / rangeFilters / textFilters."""
    ids, ranges, texts = [], [], []

    def add_ids(key, values):
        values = [v for v in values if v is not None]
        if values:
            ids.append({"filterKey": key, "ids": values})

    stars = [s for s in data.get("stars") or [] if s != "luxe"]
    if stars:
        ranges.append({"filterKey": "StarRating", "ranges": [{"from": int(s), "to": int(s) + 1} for s in stars]})
    if "luxe" in (data.get("stars") or []):
        add_ids("StarRatingWithLuxury", [refs.STAR_LUXE])
    if data.get("min_score"):
        ranges.append({"filterKey": "ReviewScore", "ranges": [{"from": int(data["min_score"])}]})
    lo, hi = data.get("min_price"), data.get("max_price")
    if lo is not None or hi is not None:
        price = {}
        if lo is not None:
            price["from"] = float(lo)
        if hi is not None:
            price["to"] = float(hi)
        ranges.append({"filterKey": "Price", "ranges": [price]})
    types = data.get("property_type") or []
    add_ids("ProductType", [refs.PRODUCT_TYPES[t] for t in types if t in refs.PRODUCT_TYPES])
    add_ids("AccommodationType", [refs.ACCOMMODATION_TYPES[t] for t in types if t in refs.ACCOMMODATION_TYPES])
    add_ids("HotelFacilities", [refs.AMENITIES[a] for a in data.get("amenities") or []])
    add_ids("GroupedBedTypes", [refs.BED_TYPES[b] for b in data.get("bed_type") or []])
    payment = [refs.PAYMENT_OPTIONS[k] for k in ("free_cancellation", "pay_later", "pay_at_hotel") if data.get(k)]
    add_ids("PaymentOptions", payment)
    if data.get("breakfast"):
        add_ids("RoomBenefits", [refs.ROOM_BENEFITS["breakfast"]])
    if data.get("name"):
        texts.append({"filterKey": "Name", "text": data["name"]})
    return ids, ranges, texts


def _search(**data):
    language, currency = data["language"], data["currency"]
    destination = _destination(data["location"], language)
    ids, ranges, texts = _filters(data)
    kind, object_id = destination["kind"], destination["id"]
    pinned = None
    if kind == "hotel":
        # a hotel as the destination: search its city with the site's own
        # name filter and put that property first
        record = cached_call(f"{NS}.hotel_city", {"hotel_id": object_id, "language": language}, _hotel_city, ttl.HOTEL_ID_CACHE)
        pinned = object_id
        kind, object_id = "city", record["city_id"]
        texts = [{"filterKey": "Name", "text": record["english_name"]}]
        destination = {**destination, "name": record["name"], "city_id": record["city_id"]}
    criteria = dict(checkin=data["checkin"], checkout=data["checkout"], adults=data["adults"],
                    child_ages=data.get("children"), rooms=data["rooms"], currency=currency,
                    sort=refs.SORTS[data["sort"]], ids_filters=ids, range_filters=ranges, text_filters=texts,
                    page=data["page"], page_size=data["limit"], city_id=destination.get("city_id"))
    parsed = parsers.parse_search(fetch_search(kind, object_id, locale=language, **criteria))
    results = parsed["results"]
    if data["sort"] in ("price_low_to_high", "price_high_to_low"):
        # the site's price sorts rank properties it has no live price for
        # (sold out, or pricing.isReady false) as the cheapest: 21-25 of the
        # first 100 Tokyo rows on 2026-11-10. Priced rows first, each group in
        # the site's own order.
        results = ([r for r in results if (r.get("price") or {}).get("per_night") is not None]
                   + [r for r in results if (r.get("price") or {}).get("per_night") is None])
    if pinned is not None:
        results.sort(key=lambda r: 0 if r.get("id") == pinned else 1)
    total = parsed["total_count"]
    organic = sum(1 for r in results if not r.get("is_sponsored"))
    if isinstance(total, int) and total < (data["page"] - 1) * data["limit"] + organic:
        # the site's total can trail its own list (2026-11-10: an area search
        # with filters answered totalFilteredHotels 0 next to 3 hotels): an
        # impossible total is no total, so paging follows the page instead
        total = None
    if isinstance(total, int):
        total_pages = math.ceil(total / data["limit"])
        pagination = {"total_pages": total_pages, "total_count": total}
    else:
        # no total from the site: the next link follows the page being full
        # (route_glue.paginate reads has_next; a null total_pages stays null)
        # (sponsored extras ride on top of `limit` organic rows, so count those)
        pagination = {"total_pages": None, "total_count": None, "has_next": organic >= data["limit"]}
    dest = parsed["destination"] or {}
    return {
        "location": {
            "type": destination["kind"],
            "id": destination["id"],
            "name": destination.get("name") or dest.get("name"),
            "city": dest.get("city"),
            "city_id": dest.get("city_id") or destination.get("city_id"),
            "country": dest.get("country"),
            "latitude": dest.get("latitude"),
            "longitude": dest.get("longitude"),
            "link": refs.search_link(destination["kind"], destination["id"], data["checkin"], data["checkout"],
                                     data["adults"], data.get("children"), data["rooms"]),
        },
        "stay": _stay(data["checkin"], data["checkout"], data["adults"], data.get("children"), data["rooms"]),
        "currency": currency,
        "language": language,
        "sort": data["sort"],
        "is_complete": parsed["is_complete"],
        "results": results,
        "price_histogram": parsed["price_histogram"],
        "filters": parsed["filters"],
        "pagination": {"page": data["page"], "items_per_page": data["limit"], **pagination},
    }


def _hotel_city(hotel_id, language):
    """A hotel's city id + names from a dateless details record (the
    English name feeds the site's name filter, which matches English)."""
    record = parsers.parse_details(fetch_details(hotel_id, locale=language), hotel_id)
    return {"city_id": record["location"]["city_id"], "name": record["name"],
            "english_name": record["english_name"] or record["name"]}


def hotels_search(**data):
    """Search hotels and homes in a city, area, region or near a landmark
    for your dates and guests: live nightly and total prices with and
    without taxes, discounts, stars, guest scores, location, cancellation
    and payment terms and photos, plus the price histogram and every filter
    with its count. Filter by stars, guest score, price, property type,
    amenities, bed type, free cancellation, pay later, pay at the hotel,
    breakfast or name. Up to 100 properties per page."""
    return cached_call(f"{NS}.search", data, _search, ttl.SEARCH_CACHE)


# ---- hotels/details -----------------------------------------------------------------------------

def _details(hotel, checkin, checkout, adults, children, rooms, currency, language):
    hotel_id = _hotel_id(hotel)
    data = fetch_details(hotel_id, checkin, _nights(checkin, checkout), adults, children, rooms, currency, language)
    try:
        summary = _summary(hotel_id, language)
    except AgodaNotFound:
        summary = None    # a property without a review record still has a page
    record = parsers.parse_details(data, hotel_id, summary=summary)
    record["stay"] = _stay(checkin, checkout, adults, children, rooms)
    record["currency"] = currency
    record["language"] = language
    return record


def hotel_details(**data):
    """Everything about one hotel for your stay: name, stars, description,
    address and coordinates, guest score with sub-scores and per-source
    review counts, every room with its live rate plans (price with and
    without taxes, cancellation, pay later, breakfast, beds, size, rooms
    left), facilities, policies, check-in and check-out times, nearby
    places and all photos."""
    return cached_call(f"{NS}.details.v2", data, _details, ttl.DETAILS_CACHE)


# ---- hotels/reviews -----------------------------------------------------------------------------

def _topic(hotel_id, topic, language, summary):
    """The `topic` param -> the hotel's review tag ({id, name, ...}). Tag
    names follow the site language, so a name not found in `language` is
    looked up in the English summary too. ValueError (-> 400) names the
    hotel's topics when nothing matches."""
    tag = refs.review_topic(summary.get("tags"), topic)
    if tag is None and language != refs.DEFAULT_LOCALE:
        english = refs.review_topic(_summary(hotel_id, refs.DEFAULT_LOCALE).get("tags"), topic)
        tag = english and (refs.review_topic(summary.get("tags"), english["id"]) or english)
    if tag is None:
        names = [t["name"] for t in summary.get("tags") or []]
        if not names:
            raise ValueError(f"Hotel {hotel_id} has no review topics on Agoda, so its reviews cannot be filtered by topic.")
        raise ValueError(f"Unknown topic {topic!r} for hotel {hotel_id}. Use one of: {', '.join(names)}.")
    return tag


def _reviews(hotel, sort, source, review_language, topic, page, limit, language):
    hotel_id = _hotel_id(hotel)
    summary = _summary(hotel_id, language)          # 404 for an unknown id
    tag = _topic(hotel_id, topic, language, summary) if topic else None
    provider_ids = refs.REVIEW_SOURCES[source]
    language_ids = [refs.REVIEW_LANGUAGES[review_language]] if review_language else None
    parsed = parsers.parse_reviews(fetch_reviews(hotel_id, page=page, size=limit, sort=refs.REVIEW_SORTS[sort],
                                                 provider_ids=provider_ids, language_ids=language_ids,
                                                 topic_ids=[tag["id"]] if tag else None, locale=language))
    # the site pages every source separately and interleaves them on one
    # page (`limit` rows from each), so the sources not asked for are
    # dropped here and the totals are the site's own per-source counts of
    # matching comments (providerList.totalIndex)
    reviews = [r for r in parsed["reviews"] if r.get("source_id") in provider_ids]
    totals = {pid: parsed["totals"].get(pid) for pid in provider_ids}
    known = [t for t in totals.values() if isinstance(t, int)]
    per_page = limit * len(provider_ids)
    if known and len(known) == len(provider_ids):
        total = sum(known)
        total_pages = math.ceil(max(known) / limit) if max(known) else 0
        has_next = page < total_pages
    else:
        total, total_pages = None, None
        has_next = len(reviews) >= per_page
    return {
        "id": hotel_id,
        "name": summary.get("name"),
        "link": summary.get("link") or parsed.get("link"),
        "source": source,
        "sort": sort,
        "review_language": review_language,
        "topic": tag["name"] if tag else None,
        "language": language,
        "summary": {k: v for k, v in summary.items() if k not in ("name", "link", "has_reviews")},
        "matching_reviews": {refs.PROVIDER_NAMES.get(pid, str(pid)): count for pid, count in totals.items()},
        "reviews": reviews,
        "sort_options": parsed["sort_options"],
        "languages": parsed["languages"],
        "room_types": parsed["room_types"],
        "pagination": {"page": page, "items_per_page": per_page, "has_next": has_next,
                       "total_pages": total_pages, "total_count": total},
    }


def hotel_reviews(**data):
    """A hotel's guest reviews from Agoda and Booking.com: score, title,
    text, pros and cons, stay dates, room, traveler type, country and the
    hotel's reply, plus the score summary and topic tags. Filter by source,
    review language or topic (Pool, Room view, Service ...); sort by recent,
    rating or helpful."""
    return cached_call(f"{NS}.reviews.v2", data, _reviews, ttl.reviews_ttl)

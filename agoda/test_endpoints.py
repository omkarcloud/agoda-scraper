"""Live endpoint smoke tests: one call per /agoda/* route against a running
service, with example values proven to return data (2026-11-09, through the
local route layer on the direct exit). The listing tooling reads the first
call per path as that route's working example.

Skipped unless AGODA_BASE points at a running service:

    python run.py                                  # standalone: http://127.0.0.1:8000
    ONLY_SCRAPER=agoda python run.py --dev         # inside the scrapers service: :6002
    AGODA_BASE=http://127.0.0.1:8000 python -m pytest agoda/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("AGODA_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set AGODA_BASE to run live endpoint tests")

MARINA_BAY_SANDS = 185945


def call(path, status=200, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=300)
    assert resp.status_code == status, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


# ---- hotels ------------------------------------------------------------------------------

def test_hotel_details():
    hotel = call("/agoda/hotels/details", hotel="185945")
    assert hotel["id"] == MARINA_BAY_SANDS and hotel["name"] == "Marina Bay Sands" and hotel["stars"] == 5.0
    assert hotel["link"] == "https://www.agoda.com/marina-bay-sands/hotel/singapore-sg.html"
    assert hotel["location"]["city"] == "Singapore" and hotel["rating"]["count"] > 10000
    assert hotel["rooms"] and hotel["rooms"][0]["offers"][0]["price"]["per_night"]
    assert hotel["images"] and hotel["facilities"] and hotel["policies"]
    linked = call("/agoda/hotels/details", hotel="https://www.agoda.com/marina-bay-sands/hotel/singapore-sg.html",
                  currency="EUR", language="de")
    assert linked["id"] == MARINA_BAY_SANDS and linked["price"]["currency"] == "EUR"
    call("/agoda/hotels/details", status=404, hotel="999999999")
    call("/agoda/hotels/details", status=400, hotel="185945", adults="11")


def test_places_autocomplete():
    body = call("/agoda/places/autocomplete", query="Tokyo")
    first = body["results"][0]
    assert (first["type"], first["id"], first["location"]) == ("city", 5085, "city:5085")
    assert first["country_code"] == "JP" and first["hotel_count"] > 1000
    assert call("/agoda/places/autocomplete", query="zzqqxxyyvv")["count"] == 0


def test_hotels_search():
    body = call("/agoda/hotels/search", location="Tokyo")
    assert (body["location"]["type"], body["location"]["id"]) == ("city", 5085)
    assert body["count"] > 1000 and body["results"] and body["next"]
    assert any(r["price"] and r["price"]["per_night"] for r in body["results"])
    # "Las Vegas" is Nevada, not the Argentine town of the same name
    vegas = call("/agoda/hotels/search", location="Las Vegas", stars="4,5", sort="guest_rating", limit="5")
    assert vegas["location"]["id"] == 17072 and len(vegas["results"]) <= 10
    by_key = call("/agoda/hotels/search", location="city:5085", free_cancellation="true", currency="JPY", limit="3")
    assert by_key["currency"] == "JPY" and by_key["results"]
    call("/agoda/hotels/search", status=404, location="city:999999999")
    call("/agoda/hotels/search", status=400, location="Tokyo", stars="7")


def test_hotel_reviews():
    body = call("/agoda/hotels/reviews", hotel="185945")
    assert body["id"] == MARINA_BAY_SANDS and body["count"] > 1000 and len(body["reviews"]) == 20
    assert all(r["source"] == "Agoda" for r in body["reviews"]) and body["summary"]["score"]
    booking = call("/agoda/hotels/reviews", hotel="185945", source="booking", sort="highest_rating", limit="5")
    assert booking["reviews"] and all(r["source"] == "Booking.com" for r in booking["reviews"])
    # a topic from summary.tags narrows the Agoda reviews (Pool: ~640 of ~3,900 on 2026-11-10)
    pool = call("/agoda/hotels/reviews", hotel="185945", topic="Pool", limit="10")
    assert pool["topic"] == "Pool" and 0 < pool["count"] < body["count"] and pool["reviews"]
    call("/agoda/hotels/reviews", status=400, hotel="185945", topic="Casino Royale")
    call("/agoda/hotels/reviews", status=404, hotel="999999999")
    call("/agoda/hotels/reviews", status=400, hotel="185945", sort="newest")

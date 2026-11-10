"""Offline tests for the /agoda/* request schemas.

    python -m pytest agoda/test_schemas.py -q
"""
import os
import sys
from datetime import date, timedelta

import pytest
from marshmallow import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agoda import refs, schemas  # noqa: E402


def _in(days):
    return (date.today() + timedelta(days=days)).isoformat()


# ---- places/autocomplete ----------------------------------------------------------------

def test_autocomplete_schema():
    assert schemas.AutocompleteSchema().load({"query": " bangkok "}) == {"query": "bangkok", "language": "en-us"}
    assert schemas.AutocompleteSchema().load({"query": "paris", "language": "FR"})["language"] == "fr-fr"
    with pytest.raises(ValidationError):
        schemas.AutocompleteSchema().load({})
    with pytest.raises(ValidationError) as e:
        schemas.AutocompleteSchema().load({"query": "x", "language": "xx-yy"})
    assert "language" in e.value.messages


# ---- hotels/search ---------------------------------------------------------------------

def test_search_schema_defaults():
    loaded = schemas.SearchSchema().load({"location": "Bangkok"})
    assert loaded["location"] == {"kind": "name", "id": None, "name": "Bangkok", "path": None}
    assert loaded["checkin"] == _in(30) and loaded["checkout"] == _in(31)
    assert loaded["adults"] == 2 and loaded["children"] is None and loaded["rooms"] == 1
    assert loaded["currency"] == "USD" and loaded["language"] == "en-us" and loaded["sort"] == "recommended"
    assert loaded["page"] == 1 and loaded["limit"] == refs.SEARCH_PAGE_SIZE
    for key in ("stars", "min_score", "min_price", "max_price", "property_type", "amenities", "bed_type",
                "free_cancellation", "pay_later", "pay_at_hotel", "breakfast", "name"):
        assert loaded[key] is None, key


def test_search_schema_location_forms():
    assert schemas.SearchSchema().load({"location": "9395"})["location"]["kind"] == "city"
    assert schemas.SearchSchema().load({"location": "area:27713"})["location"] == {"kind": "area", "id": 27713, "name": None, "path": None}
    assert schemas.SearchSchema().load({"location": "https://www.agoda.com/search?city=9395"})["location"]["id"] == 9395
    assert schemas.SearchSchema().load({"location": "https://www.agoda.com/bella-b_4/hotel/bangkok-th.html"})["location"]["kind"] == "hotel_path"
    with pytest.raises(ValidationError) as e:
        schemas.SearchSchema().load({"location": "https://www.booking.com/searchresults.html"})
    assert "location" in e.value.messages


def test_search_schema_stay():
    loaded = schemas.SearchSchema().load({"location": "Bangkok", "checkin": _in(10)})
    assert loaded["checkout"] == _in(11)
    loaded = schemas.SearchSchema().load({"location": "Bangkok", "checkout": _in(10)})
    assert loaded["checkin"] == _in(9)
    loaded = schemas.SearchSchema().load({"location": "Bangkok", "checkin": _in(10), "checkout": _in(13)})
    assert (loaded["checkin"], loaded["checkout"]) == (_in(10), _in(13))
    with pytest.raises(ValidationError) as e:
        schemas.SearchSchema().load({"location": "Bangkok", "checkin": _in(10), "checkout": _in(10)})
    assert "checkout" in e.value.messages
    with pytest.raises(ValidationError):
        schemas.SearchSchema().load({"location": "Bangkok", "checkin": _in(10), "checkout": _in(45)})
    with pytest.raises(ValidationError):
        schemas.SearchSchema().load({"location": "Bangkok", "checkin": "2020-01-01"})
    with pytest.raises(ValidationError):
        schemas.SearchSchema().load({"location": "Bangkok", "checkin": "10/12/2026"})


def test_search_schema_filters():
    loaded = schemas.SearchSchema().load({
        "location": "city:9395", "adults": "3", "children": "4, 9", "rooms": "2", "currency": "eur", "language": "ja",
        "sort": "Price_Low_To_High", "stars": "4,5,luxe", "min_score": "8", "min_price": "20", "max_price": "150",
        "property_type": "hotels,resort", "amenities": "pool,gym", "bed_type": "king", "free_cancellation": "yes",
        "pay_later": "1", "pay_at_hotel": "false", "breakfast": "true", "name": "Marriott", "page": "3", "limit": "100"})
    assert loaded["adults"] == 3 and loaded["children"] == [4, 9] and loaded["rooms"] == 2
    assert loaded["currency"] == "EUR" and loaded["language"] == "ja-jp" and loaded["sort"] == "price_low_to_high"
    assert loaded["stars"] == ["4", "5", "luxe"] and loaded["min_score"] == 8
    assert loaded["min_price"] == 20.0 and loaded["max_price"] == 150.0
    assert loaded["property_type"] == ["hotels", "resort"] and loaded["amenities"] == ["pool", "gym"] and loaded["bed_type"] == ["king"]
    assert loaded["free_cancellation"] is True and loaded["pay_later"] is True and loaded["pay_at_hotel"] is False
    assert loaded["breakfast"] is True and loaded["name"] == "Marriott" and loaded["page"] == 3 and loaded["limit"] == 100


def test_search_schema_children_keep_duplicates():
    # twins: two children aged 5, not one
    loaded = schemas.SearchSchema().load({"location": "Bangkok", "children": "5,5, 9"})
    assert loaded["children"] == [5, 5, 9]
    assert schemas.SearchSchema().load({"location": "Bangkok", "children": " , "})["children"] is None
    with pytest.raises(ValidationError) as e:
        schemas.SearchSchema().load({"location": "Bangkok", "children": "1,2,3,4,5,6,7"})
    assert "children" in e.value.messages


@pytest.mark.parametrize("params,field", [
    ({"location": "Bangkok", "stars": "6"}, "stars"),
    ({"location": "Bangkok", "min_score": "5"}, "min_score"),
    ({"location": "Bangkok", "property_type": "castle"}, "property_type"),
    ({"location": "Bangkok", "amenities": "helipad"}, "amenities"),
    ({"location": "Bangkok", "children": "19"}, "children"),
    ({"location": "Bangkok", "adults": "0"}, "adults"),
    ({"location": "Bangkok", "rooms": "10"}, "rooms"),
    ({"location": "Bangkok", "limit": "101"}, "limit"),
    ({"location": "Bangkok", "page": "201"}, "page"),
    ({"location": "Bangkok", "sort": "cheapest"}, "sort"),
    ({"location": "Bangkok", "currency": "dollars"}, "currency"),
    # a well-formed code Agoda does not sell in answered an empty 200 search
    ({"location": "Bangkok", "currency": "XYZ"}, "currency"),
    ({"location": "Bangkok", "currency": "ISK"}, "currency"),
    ({"location": "Bangkok", "min_price": "100", "max_price": "50"}, "max_price"),
    ({"location": "Bangkok", "city": "9395"}, "city"),
    ({"location": "Bangkok", "url": "https://www.agoda.com/search?city=9395"}, "url"),
])
def test_search_schema_rejects(params, field):
    with pytest.raises(ValidationError) as e:
        schemas.SearchSchema().load(params)
    assert field in e.value.messages


# ---- hotels/details --------------------------------------------------------------------

def test_details_schema():
    loaded = schemas.HotelDetailsSchema().load({"hotel": "9059812"})
    assert loaded["hotel"] == "9059812" and loaded["checkin"] == _in(30) and loaded["checkout"] == _in(31)
    assert loaded["adults"] == 2 and loaded["rooms"] == 1 and loaded["currency"] == "USD" and loaded["language"] == "en-us"
    loaded = schemas.HotelDetailsSchema().load({
        "hotel": "https://www.agoda.com/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html?checkIn=2026-12-10",
        "checkin": _in(5), "checkout": _in(7), "children": "3", "currency": "thb", "language": "th-th"})
    assert loaded["hotel"] == "/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html"
    assert loaded["children"] == [3] and loaded["currency"] == "THB" and loaded["language"] == "th-th"


@pytest.mark.parametrize("params,field", [
    ({}, "hotel"),
    ({"hotel": "bella b"}, "hotel"),
    ({"hotel": "https://www.booking.com/hotel/th/bella.html"}, "hotel"),
    ({"hotel": "9059812", "id": "9059812"}, "id"),
    ({"hotel": "9059812", "checkin": "2020-01-01"}, "checkin"),
    ({"hotel": "9059812", "adults": "11"}, "adults"),
])
def test_details_schema_rejects(params, field):
    with pytest.raises(ValidationError) as e:
        schemas.HotelDetailsSchema().load(params)
    assert field in e.value.messages


# ---- hotels/reviews --------------------------------------------------------------------

def test_reviews_schema():
    loaded = schemas.HotelReviewsSchema().load({"hotel": "9059812"})
    assert loaded == {"hotel": "9059812", "sort": "recent", "source": "agoda", "review_language": None, "topic": None,
                      "page": 1, "limit": refs.REVIEWS_PAGE_SIZE, "language": "en-us"}
    loaded = schemas.HotelReviewsSchema().load({"hotel": "hotel:9059812", "sort": "Highest_Rating", "source": "ALL",
                                                "review_language": "th", "page": "4", "limit": "100",
                                                "language": "de"})
    assert loaded["hotel"] == "9059812" and loaded["sort"] == "highest_rating" and loaded["source"] == "all"
    assert loaded["review_language"] == "th" and loaded["page"] == 4
    assert loaded["limit"] == 100 and loaded["language"] == "de-de"
    # topics are resolved against the hotel's own tags in api.py; the schema only trims them
    assert schemas.HotelReviewsSchema().load({"hotel": "9059812", "topic": "  Room  view "})["topic"] == "Room view"


@pytest.mark.parametrize("params,field", [
    ({"hotel": "9059812", "sort": "newest"}, "sort"),
    ({"hotel": "9059812", "source": "tripadvisor"}, "source"),
    ({"hotel": "9059812", "review_language": "klingon"}, "review_language"),
    ({"hotel": "9059812", "limit": "101"}, "limit"),
    ({"hotel": "9059812", "page": "0"}, "page"),
    ({"hotel": "9059812", "topic": "x" * 101}, "topic"),
    # the site's topic chips filter Agoda's own reviews only
    ({"hotel": "9059812", "topic": "Pool", "source": "booking"}, "topic"),
    ({"hotel": "9059812", "topic": "Pool", "source": "all"}, "topic"),
    # the free-text keyword is gone (the site ignores it): unknown params are rejected
    ({"hotel": "9059812", "query": "pool"}, "query"),
])
def test_reviews_schema_rejects(params, field):
    with pytest.raises(ValidationError) as e:
        schemas.HotelReviewsSchema().load(params)
    assert field in e.value.messages


def test_every_field_is_documented():
    for schema in (schemas.AutocompleteSchema, schemas.SearchSchema, schemas.HotelDetailsSchema, schemas.HotelReviewsSchema):
        for name, field in schema().fields.items():
            assert field.metadata.get("description"), f"{schema.__name__}.{name} has no description"

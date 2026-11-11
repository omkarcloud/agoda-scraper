"""Offline tests for the Agoda parsers and reference resolvers.

    python -m pytest agoda/test_parsers.py -q

Fixtures are real www.agoda.com payloads captured 2026-11-09 through
agoda/fetch.py (agoda/fixtures/): autocomplete rows (a city, a hotel name,
a nonsense string, a Japanese locale), search pages (a city page, a filtered
page, a past-the-end page, an area, a landmark, a region, a top area and a
name-filtered Japanese / THB page), details records (a dated stay, a
dateless one, the unknown-id shell, a German / EUR record with children),
review pages (recent, Booking.com sort, both sources, a Thai-language
filter — captured with a keyword the site ignores, see fetch.py —, past the
end, unknown id) and two review summaries. No network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agoda import parsers as P, refs  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def bangkok():
    return P.parse_search(fixture("search_city_bangkok_page1.json"))


@pytest.fixture(scope="module")
def details():
    return P.parse_details(fixture("details_bella_b_stay.json"), 9059812,
                           summary=P.parse_review_summary(fixture("review_summary_bella_b.json")))


# ---- refs: resolvers ------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("9059812", "9059812"),
    (" 9059812 ", "9059812"),
    ("hotel:9059812", "9059812"),
    ("https://www.agoda.com/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html", "/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html"),
    ("https://www.agoda.com/en-gb/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html?checkIn=2026-12-10",
     "/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html"),
    ("https://www.agoda.com/bella-b-all-suite-bangkok-sha-extra-plus/reviews/bangkok-th.html",
     "/bella-b-all-suite-bangkok-sha-extra-plus/hotel/bangkok-th.html"),
    ("https://www.agoda.com/search?hotel=286830&selectedproperty=286830&city=9395", "286830"),
    ("https://www.agoda.com/x/hotel/x.html?hotelId=9059812", "9059812"),
    ("//www.agoda.com/partners/partnersearch.aspx?hid=9059812", "9059812"),
])
def test_resolve_hotel(value, expected):
    assert refs.resolve_hotel(value) == expected


@pytest.mark.parametrize("value", ["", "bella b", "https://booking.com/hotel/th/x.html", "https://www.agoda.com/city/bangkok-th.html"])
def test_resolve_hotel_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_hotel(value)


@pytest.mark.parametrize("value,kind,ident,name", [
    ("9395", "city", 9395, None),
    ("city:9395", "city", 9395, None),
    ("area:27713", "area", 27713, None),
    ("toparea=7", "top_area", 7, None),
    ("poi:141553", "landmark", 141553, None),
    ("landmark:141553", "landmark", 141553, None),
    ("region:4157", "region", 4157, None),
    ("hotel:286830", "hotel", 286830, None),
    ("https://www.agoda.com/search?city=9395&checkIn=2026-12-10", "city", 9395, None),
    ("https://www.agoda.com/search?asq=xyz&area=27713&tick=1", "area", 27713, None),
    ("https://www.agoda.com/search?poi=141553", "landmark", 141553, None),
    ("https://www.agoda.com/search?region=4157", "region", 4157, None),
    ("https://www.agoda.com/search?toparea=7", "top_area", 7, None),
    ("https://www.agoda.com/search?hotel=286830&selectedproperty=286830", "hotel", 286830, None),
    ("https://www.agoda.com/city/chiang-mai-th.html", "name", None, "chiang mai"),
    ("Bangkok", "name", None, "Bangkok"),
    ("Terminal 21", "name", None, "Terminal 21"),
])
def test_resolve_location(value, kind, ident, name):
    out = refs.resolve_location(value)
    assert (out["kind"], out["id"], out["name"]) == (kind, ident, name)


def test_resolve_location_hotel_link():
    out = refs.resolve_location("https://www.agoda.com/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html")
    assert out["kind"] == "hotel_path" and out["path"] == "/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html"


def test_resolve_location_narrowest_key_wins():
    # the site's own links carry the city next to the area / hotel key
    out = refs.resolve_location("https://www.agoda.com/search?city=9395&area=27713&checkIn=2026-12-10")
    assert (out["kind"], out["id"], out["city_id"]) == ("area", 27713, 9395)
    out = refs.resolve_location("https://www.agoda.com/search?city=9395&selectedproperty=9059812")
    assert (out["kind"], out["id"], out["city_id"]) == ("hotel", 9059812, 9395)
    assert "city_id" not in refs.resolve_location("https://www.agoda.com/search?city=9395")
    row = P._row_kind_and_id({"ResultUrl": "/search?city=9395&area=27713&tick=1"})
    assert row == ("area", 27713)


@pytest.mark.parametrize("value", ["", "https://booking.com/searchresults.html?city=1", "https://www.agoda.com/flights", "x" * 101])
def test_resolve_location_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_location(value)


@pytest.mark.parametrize("value,expected", [
    ("", "en-us"), ("en-us", "en-us"), ("EN_GB", "en-gb"), ("fr", "fr-fr"), ("ja", "ja-jp"), ("zh", "zh-cn"), ("pt-BR", "pt-br"),
])
def test_normalize_locale(value, expected):
    assert refs.normalize_locale(value) == expected


def test_normalize_locale_rejects():
    with pytest.raises(ValueError):
        refs.normalize_locale("xx-yy")


def test_links():
    assert refs.hotel_link("/bella-b/reviews/bangkok-th.html") == "https://www.agoda.com/bella-b/hotel/bangkok-th.html"
    assert refs.hotel_link("") is None
    assert refs.search_link("city", 9395, "2026-12-10", "2026-12-12", 2, [4, 9], 1) == \
        "https://www.agoda.com/search?city=9395&checkIn=2026-12-10&checkOut=2026-12-12&adults=2&children=2&childAges=4,9&rooms=1"
    assert refs.search_link("landmark", 141553) == "https://www.agoda.com/search?poi=141553"
    assert refs.search_link("name", None) is None
    assert refs.absolute_link("//pix8.agoda.net/x.jpg") == "https://pix8.agoda.net/x.jpg"
    assert refs.location_key("area", 27713) == "area:27713" and refs.location_key("name", 1) is None


# ---- value helpers ---------------------------------------------------------------------------

def test_value_helpers():
    assert P._text("<b>Hi</b><br>there &amp; more​") == "Hi\nthere & more"
    assert P._date("2026-09-16T08:16:48+07:00") == "2026-09-16"
    assert P._date("junk") is None
    assert P._money(0) is None and P._money("34.56") == 34.56 and P._money(None) is None
    assert P._num("1,234") == 1234 and P._num("12.5 km") == 12.5 and P._num(True) is None
    assert P._num("7,7") == 7.7 and P._num("34,56") == 34.56 and P._num("1.234,56") == 1234.56 and P._num("8,0") == 8.0
    assert P._num("23 %") == 23 and P._num("") is None and P._num("n/a") is None
    assert P._distance_km("1.37 kilometers from city center") == 1.37
    assert P._distance_km("300 meters to") == 0.3
    assert P._coord(0) is None and P._coord(13.72) == 13.72
    assert P._attraction("Wat Pho Temple - 3.15 km from property") == {"name": "Wat Pho Temple", "distance_km": 3.15}
    assert P._clean("  a   b ") == "a b" and P._clean("") is None


# ---- places/autocomplete --------------------------------------------------------------------

def test_autocomplete_city_rows():
    rows = P.parse_autocomplete(fixture("autocomplete_bangkok.json"))
    assert len(rows) == 6
    city = next(r for r in rows if r["type"] == "city")
    assert city["id"] == 9395 and city["name"] == "Bangkok" and city["location"] == "city:9395"
    assert city["country_code"] == "TH" and city["hotel_count"] == 12048
    assert city["link"] == "https://www.agoda.com/search?city=9395"
    assert {r["type"] for r in rows} <= set(refs.SEARCHABLE_KINDS)
    # the echo row of the typed text is gone
    assert all(r["name"] != "bangkok" for r in rows)


def test_autocomplete_hotel_rows():
    rows = P.parse_autocomplete(fixture("autocomplete_bella_b.json"))
    first = rows[0]
    assert first["type"] == "hotel" and first["id"] == 9059812 and first["location"] == "hotel:9059812"
    assert first["name"] == "Palette Bella B All Suites Bangkok Hotel" and first["city_id"] == 9395
    assert first["hotel_count"] is None
    assert first["link"] == "https://www.agoda.com/search?selectedproperty=9059812"


def test_autocomplete_area_and_region_rows():
    rows = P.parse_autocomplete(fixture("autocomplete_sukhumvit.json"))
    area = next(r for r in rows if r["type"] == "area")
    assert area["id"] == 27713 and area["city_id"] == 9395 and area["hotel_count"] == 5736
    region = next(r for r in rows if r["type"] == "region")
    assert region["id"] == 2188 and region["name"] == "BTS Sukhumvit Line"


def test_autocomplete_no_match_and_locale():
    assert P.parse_autocomplete(fixture("autocomplete_nomatch.json")) == []
    assert P.parse_autocomplete({}) == []
    rows = P.parse_autocomplete(fixture("autocomplete_bangkok_ja.json"))
    assert next(r for r in rows if r["type"] == "city")["name"] == "バンコク"


def test_best_suggestion_prefers_the_named_city():
    # the site lists two Bangkok districts above the city itself
    rows = P.parse_autocomplete(fixture("autocomplete_bangkok.json"))
    assert rows[0]["type"] == "region"
    best = refs.best_suggestion(rows, "  bangkok ")
    assert (best["type"], best["id"]) == ("city", 9395)
    # no name match: the broadest kind wins, then the site's order
    assert refs.best_suggestion(rows, "Bangkok Thailand")["type"] == "city"
    assert refs.best_suggestion([], "Bangkok") is None


def test_best_suggestion_drops_the_state_qualifier():
    # live rows for "Las Vegas" (2026-11-09): the Argentine town is an exact
    # name match, Nevada only matches once "(NV)" is dropped — the site's
    # order then picks Nevada
    rows = [{"type": "city", "id": 17072, "name": "Las Vegas (NV)"},
            {"type": "city", "id": 724103, "name": "Las Vegas"},
            {"type": "city", "id": 17811, "name": "Las Vegas (NM)"},
            {"type": "area", "id": 61158, "name": "North Las Vegas"},
            {"type": "area", "id": 61160, "name": "The Strip"}]
    assert refs.best_suggestion(rows, "Las Vegas")["id"] == 17072
    assert refs.best_suggestion(rows, "las vegas (nm)")["id"] == 17811
    assert refs.best_suggestion(rows, "The Strip")["id"] == 61160


# ---- hotels/search -----------------------------------------------------------------------------

def test_search_page_shape(bangkok):
    assert bangkok["destination"] == {"name": "Bangkok", "city": "Bangkok", "city_id": 9395, "country": "Thailand",
                                      "country_id": 106, "latitude": 13.739198, "longitude": 100.553263}
    assert bangkok["total_count"] == 6643 and bangkok["is_complete"] is True
    assert len(bangkok["results"]) == 49          # 45 + sponsored extras
    assert len(bangkok["price_histogram"]) == 36
    assert bangkok["price_histogram"][0] == {"max_price_per_night": 10, "count": 99}
    assert set(bangkok["filters"]) >= {"stars", "guest_rating", "property_types", "amenities", "payment_options", "areas"}
    stars = {s["id"]: s for s in bangkok["filters"]["stars"]}
    assert stars[5]["name"] == "5 stars" and stars[1]["name"] == "1 star" and stars[101]["name"] == "Agoda Luxe"
    assert stars[3]["count"] == 2788


def test_search_record(bangkok):
    rec = next(r for r in bangkok["results"] if r["id"] == 555582)
    assert rec["name"] == "Marigold Sukhumvit" and rec["link"] == "https://www.agoda.com/marigold-sukhumvit/hotel/bangkok-th.html"
    assert rec["type"] == "Hotel" and rec["stars"] == 4.5 and rec["is_available"] is True and rec["is_sold_out"] is False
    loc = rec["location"]
    assert loc["city_id"] == 9395 and loc["area"] == "Bang Na" and loc["country_code"] == "TH"
    assert loc["latitude"] == pytest.approx(13.652, abs=0.001) and loc["distance_from_city_center_km"] == 7.09
    assert loc["landmarks"]["transportation"][0] == {"name": "Samrong BTS Station", "distance_m": 786.29}
    rating = rec["rating"]
    assert rating["score"] == 8.4 and rating["count"] == 5444 and rating["recommendation_percent"] == 89
    assert rating["grades"]["service"] == 8.9 and rating["score_by_traveler_type"]["couple"] == 8.6
    assert rating["snippet"]["reviewer"] == "Sasa" and rating["snippet"]["date"] == "2026-08-25"
    assert rating["topics"][0]["topic"] == "Location" and len(rating["topics"][0]["snippets"]) == 3
    price = rec["price"]
    assert price == {"currency": "USD", "per_night": 26.06, "per_night_with_taxes": 31.39, "total": 52.12,
                     "total_with_taxes": 62.78, "before_discount_per_night": 104.24, "discount_percent": 75,
                     "promotions": None, "sold_out_average_per_night": None}
    assert rec["cheapest_room"]["available_rooms"] == 1 and rec["cheapest_room"]["size_sqm"] == 32.0
    assert rec["cancellation"] == {"type": "FreeCancellation", "is_free": True, "free_until": "2026-12-03"}
    assert rec["payment"]["is_pay_later"] is True and rec["payment"]["is_pay_at_hotel"] is False
    assert "Swimming pool" in rec["highlights"]["favorite_features"]
    assert rec["family"]["has_children_free_policy"] is True
    assert rec["engagement"]["bookings_last_24h"] == 23 and rec["engagement"]["last_booking"].startswith("2026-11-09T")
    assert rec["spoken_languages"] == ["en", "th"]
    assert len(rec["images"]) == 10 and rec["images"][0]["link"].startswith("https://pix8.agoda.net/")
    assert rec["home"] is None


def test_search_sold_out_record(bangkok):
    rec = next(r for r in bangkok["results"] if r["is_sold_out"])
    assert rec["is_available"] is False and rec["price"]["sold_out_average_per_night"] == 61.65
    assert rec["price"]["per_night"] is None and rec["cancellation"] is None and rec["payment"] is None


def test_search_sponsored_flag(bangkok):
    assert sum(1 for r in bangkok["results"] if r["is_sponsored"]) >= 1


def test_search_filtered_page():
    page = P.parse_search(fixture("search_city_bangkok_filtered.json"))
    assert page["total_count"] == 528 and len(page["results"]) == 23
    assert all((r["stars"] or 0) >= 4 for r in page["results"] if not r["is_sponsored"])
    first = page["results"][0]
    assert first["name"] == "Samsen sam Place" and first["price"]["per_night"] == 13.59


def test_search_past_end_is_empty_not_error():
    page = P.parse_search(fixture("search_city_singapore_past_end.json"))
    assert page["results"] == [] and page["total_count"] == 738 and page["destination"]["name"] == "Singapore"


@pytest.mark.parametrize("name,dest,total", [
    ("search_area_sukhumvit.json", "Sukhumvit", 1589),
    ("search_landmark_terminal21.json", "Terminal 21", 353),
    ("search_region_victoria.json", "Victoria, Singapore", 18),
    ("search_toparea_singapore.json", "Singapore top guest-rated area", 194),
])
def test_search_sibling_operations(name, dest, total):
    page = P.parse_search(fixture(name))
    assert page["destination"]["name"] == dest and page["total_count"] == total
    assert len(page["results"]) == 11 and all(r["id"] for r in page["results"])


def test_search_localized_currency_and_name_filter():
    page = P.parse_search(fixture("search_city_bangkok_name_ja_thb.json"))
    assert page["destination"]["name"] == "バンコク" and page["total_count"] == 15
    first = page["results"][0]
    assert first["name"].startswith("バンコク・マリオット") and first["english_name"] == "Bangkok Marriott Hotel Sukhumvit"
    assert first["price"]["currency"] == "THB"


def test_search_handles_partial_payloads():
    assert P.parse_search({})["results"] == []
    assert P.parse_search(None)["destination"] is None
    rec = P._search_record({"propertyId": 1, "content": {"informationSummary": {"displayName": "X"}}, "pricing": None})
    assert rec["id"] == 1 and rec["name"] == "X" and rec["price"] is None and rec["rating"]["score"] is None


# ---- hotels/details ----------------------------------------------------------------------------

def test_details_identity_and_location(details):
    assert details["id"] == 9059812 and details["name"] == "Palette Bella B All Suites Bangkok Hotel"
    assert details["link"] == "https://www.agoda.com/bella-b-all-suite-bangkok-sha-extra-plus/hotel/bangkok-th.html"
    assert details["type"] == "Hotel" and details["stars"] == 4.0 and details["is_home"] is False
    loc = details["location"]
    assert loc["postal_code"] == "10600" and loc["area"] == "Thonburi" and loc["city_id"] == 9395 and loc["country"] == "Thailand"
    assert loc["latitude"] == pytest.approx(13.7211, abs=0.001) and loc["distance_from_city_center_km"] == 1.37
    assert details["description"].startswith("Entertainment Facilities at Bella B")
    assert details["check_in_from"] == "02:00 PM" and details["check_out_until"] == "11:00 AM"
    assert details["languages_spoken"] == ["English", "Thai", "Chinese [Mandarin]"]


def test_details_rating_merges_summary(details):
    rating = details["rating"]
    assert rating["score"] == 7.7 and rating["count"] == 2719 and rating["score_text"] == "Very good"
    assert rating["grades"]["value_for_money"] == {"score": 8.0, "city_average": 7.8}
    assert rating["score_distribution"][0] == {"band": "exceptional", "label": "9+ Exceptional", "count": 847}
    assert rating["sources"][1] == {"source": "Booking.com", "source_id": 3038, "count": 498}
    assert rating["tags"][0]["name"] == "Cleanliness" and rating["tags"][0]["mentions"] == 230
    assert len(rating["recent_scores"]["Agoda"]) == 10


def test_details_content_blocks(details):
    assert details["price"]["currency"] == "USD" and details["price"]["cheapest_per_night"] == 35.06
    assert details["price"]["cheaper_than_city_average_percent"] == 23
    groups = {g["group"]: g["items"] for g in details["facilities"]}
    assert "Fitness center" in groups["Take a look at these facilities:"] and "English" in groups["Languages spoken"]
    useful = {g["group"]: g["items"] for g in details["useful_info"]}
    assert {"title": "Number of rooms", "value": "74"} in useful["The property"]
    policies = details["policies"]
    assert policies["children"][0]["is_infant"] is True and policies["children"][0]["age_range"] == "Infant 0-2 year(s)"
    assert policies["other"][0].startswith("Please be informed that cooking")
    assert details["highlights"][0]["text"] == "250 meters to public transportation"
    assert details["highlights"][0]["details"][0].startswith("Krung Thonburi Golden Line Station")
    nearby = details["nearby"]
    assert nearby["essentials"][0]["category"] == "Airports" and nearby["essentials"][0]["places"][0]["distance_km"] == 23.84
    assert nearby["landmarks"][0]["places"][0]["name"] == "Asiatique - The Riverfront"
    assert nearby["attractions"][0] == {"name": "Yaowarat Road (Bangkok Chinatown)", "distance_km": 2.18}
    assert details["awards"]["is_agoda_preferred"] is True
    assert details["engagement"]["bookings_today"] == 17
    assert len(details["images"]) == 159 and details["images"][0]["category"] == "Facilities"
    assert details["images"][0]["link"].startswith("https://pix8.agoda.net/hotelImages/9059812/")
    assert details["image_categories"][0] == {"category": "Room photos", "count": 36}
    assert details["videos"][0]["link"] == "https://pix5.agoda.net/video/9059812.mp4"
    assert details["taxes_and_fees"] == "Tax 7%, Service charge 10%"
    assert details["stay"] == {"checkin": "2026-12-10", "checkout": "2026-12-12", "nights": 2, "adults": 2, "children": 0, "rooms": 1}


def test_details_rooms_and_offers(details):
    rooms = details["rooms"]
    assert len(rooms) == 6
    room = rooms[0]
    assert room["name"] == "Premier Deluxe Twin" and room["size_sqm"] == 30.0 and room["view"] == "City view"
    assert room["beds"] == "2 single beds" and room["bed_layouts"][0]["beds"][0]["quantity"] == 2
    assert room["max_occupancy"] == 2 and room["cheapest_price"] == 35.06 and room["before_discount_price"] == 69.75
    assert "Air conditioning" in room["amenities"] and room["facility_groups"][0]["group"] == "Bathroom and toiletries"
    assert room["images"][0]["link"].startswith("https://pix8.agoda.net/") and room["images"][0]["caption"] == "Bedroom"
    offer = room["offers"][0]
    assert offer["rate_plan_id"] == 2938730
    assert offer["price"]["per_night"] == 35.06 and offer["price"]["per_night_with_taxes"] == 41.27 and offer["price"]["total"] == 82.53
    assert offer["price"]["discount_percent"] == 50 and offer["price"]["taxes_and_fees"] == ["Tax 7%", "Service charge 10%"]
    assert offer["is_free_cancellation"] is True and offer["cancellation"]["code"] == "1D100P_100P"
    assert offer["is_pay_later"] is True and offer["pay_later"] == "No payment until December 7, 2026"
    assert offer["payment_model"] == "merchant" and offer["supplier"] == {"id": 28100, "name": "Agoda.com", "is_agoda": True}
    assert offer["benefits"] == ["Parking", "Free WiFi"] and offer["promotion"]["description"] == "Super Saver"
    assert offer["price_breakdown"]["price_summaries"][0] == {"item": "Price per night", "amount": 35.0}
    assert offer["occupancy"] == {"adults": 2, "children": 0, "max_guests": 2, "free_children": 1, "max_extra_beds": 0}


def test_details_dateless_has_no_rooms():
    record = P.parse_details(fixture("details_bella_b_dateless.json"), 9059812)
    assert record["rooms"] == [] and record["stay"] is None and record["link"] is None
    assert record["name"] == "Palette Bella B All Suites Bangkok Hotel" and record["price"]["cheapest_per_night"] is None


def test_details_unknown_shell_is_empty():
    record = P.parse_details(fixture("details_unknown.json"), 999999999)
    assert record["name"] is None and record["rooms"] == [] and record["images"] is None
    assert P.parse_details({}, 1)["id"] == 1 and P.parse_details(None, 1)["rooms"] == []


def test_details_localized_currency_children():
    record = P.parse_details(fixture("details_ritz_singapore_eur_de.json"), 10626,
                             summary=P.parse_review_summary(fixture("review_summary_ritz_de.json")))
    assert record["name"] == "The Ritz-Carlton, Millenia Singapore"
    assert record["link"] == "https://www.agoda.com/the-ritz-carlton-millenia-singapore/hotel/singapore-sg.html"
    assert record["price"]["currency"] == "EUR" and record["price"]["cheapest_per_night"] == 370.83
    assert record["stay"]["children"] == 2 and record["check_in_from"] == "03:00 PM"
    assert record["facilities"][0]["group"] == "Beliebte Einrichtungen"
    assert record["rooms"][0]["offers"][0]["price"]["taxes_and_fees"][0] == "Steuern 9%"
    # feature id 15 is both the bed line and "Nichtraucher" (non-smoking)
    assert record["rooms"][0]["beds"] == "2 Einzelbetten" and "Nichtraucher" in record["rooms"][0]["features"]
    assert record["rooms"][1]["beds"] == "1 Kingsize-Bett"


# ---- hotels/reviews ------------------------------------------------------------------------------

def test_reviews_page():
    page = P.parse_reviews(fixture("reviews_bella_b_page1.json"))
    assert len(page["reviews"]) == 40                       # 20 Agoda + 20 Booking.com rows, interleaved
    assert page["totals"] == {332: 955, 3038: 287}
    assert page["link"] == "https://www.agoda.com/bella-b-all-suite-bangkok-sha-extra-plus/hotel/bangkok-th.html"
    assert page["sort_options"][0] == {"id": 1, "name": "Most recent"}
    assert {"id": 1, "name": "English", "code": "en"} in page["languages"] and len(page["room_types"]) == 6
    review = page["reviews"][0]
    assert review["id"] == 1202941810 and review["score"] == 7.6 and review["score_text"] == "Very good"
    assert review["date"] == "2026-11-03" and review["stay"] == {"checkin": "2026-05-31", "checkout": "2026-06-03",
                                                                "month": "May 2026", "nights": 3, "room": "Premier Deluxe Double"}
    assert review["reviewer"] == {"name": "panyachalee", "country": "Thailand", "country_code": "TH",
                                  "traveler_type": "Solo traveler", "review_count": None, "is_expert": False}
    assert review["source"] == "Agoda" and review["source_id"] == 332
    booking = next(r for r in page["reviews"] if r["source_id"] == 3038)
    assert booking["source"] == "Booking.com"


def test_reviews_translated_fields():
    page = P.parse_reviews(fixture("reviews_bella_b_all_sources.json"))
    translated = next(r for r in page["reviews"] if r["is_translated"])
    assert translated["original"]["language"] == "th" and translated["original"]["text"]
    assert translated["text"] and translated["text"] != translated["original"]["text"]
    with_reply = next(r for r in page["reviews"] if r["response"])
    assert with_reply["response"]["responder"] == "Palette Bella B All Suites Bangkok Hotel" and with_reply["response"]["date"]
    with_images = next(r for r in page["reviews"] if r["images"])
    assert with_images["images"][0]["link"].startswith("https://pix6.agoda.net/")


def test_reviews_filtered_totals():
    page = P.parse_reviews(fixture("reviews_bella_b_thai_pool.json"))
    assert page["totals"] == {332: 266, 3038: 16} and len(page["reviews"]) == 10


def test_reviews_past_end_and_unknown():
    past = P.parse_reviews(fixture("reviews_bella_b_past_end.json"))
    assert past["reviews"] == [] and past["link"].endswith("/hotel/bangkok-th.html")
    unknown = P.parse_reviews(fixture("reviews_unknown.json"))
    assert unknown["reviews"] == [] and unknown["link"] is None and unknown["totals"] == {}
    assert P.parse_reviews({})["reviews"] == []


def test_review_summary():
    summary = P.parse_review_summary(fixture("review_summary_bella_b.json"))
    assert summary["score"] == 7.7 and summary["count"] == 2719 and summary["has_reviews"] is True
    assert summary["name"] == "Palette Bella B All Suites Bangkok Hotel"
    assert summary["link"] == "https://www.agoda.com/bella-b-all-suite-bangkok-sha-extra-plus/hotel/bangkok-th.html"
    assert [s["source"] for s in summary["sources"]] == ["Agoda", "Booking.com", "Other sites"]
    assert summary["sources"][0]["count"] == 2719
    assert summary["tags"][1] == {"id": 24, "name": "Location", "mentions": 213, "positive_percent": 76.0,
                                  "negative_percent": 23.0, "rating": "Excellent"}
    assert summary["grades"]["cleanliness"]["score"] == 7.8 and summary["best_grade"]["name"] == "Value for money"
    assert P.parse_review_summary({})["score"] is None


# ---- review topics + the empty-page guard -----------------------------------------------

def test_review_topic_resolves_a_name_or_an_id():
    tags = P.parse_review_summary(fixture("review_summary_bella_b.json"))["tags"]
    assert refs.review_topic(tags, "pool")["id"] == 28
    assert refs.review_topic(tags, "  Room   COMFORT ")["id"] == 31
    assert refs.review_topic(tags, "28")["name"] == "Pool" and refs.review_topic(tags, 24)["name"] == "Location"
    assert refs.review_topic(tags, "Breakfast") is None and refs.review_topic(None, "Pool") is None
    assert refs.review_topic(tags, "") is None


def test_topic_falls_back_to_the_english_names(monkeypatch):
    from agoda import api
    german = P.parse_review_summary(fixture("review_summary_ritz_de.json"))
    english = {"tags": [{"id": 34, "name": "Room view"}, {"id": 9, "name": "Breakfast"}]}
    monkeypatch.setattr(api, "_summary", lambda hotel_id, language: english if language == "en-us" else german)
    # an English name on a German page answers with the German tag
    tag = api._topic(1, "Room view", "de-de", german)
    assert (tag["id"], tag["name"]) == (34, "Aussicht vom Zimmer")
    assert api._topic(1, "frühstück", "de-de", german)["id"] == 9
    with pytest.raises(ValueError, match="Use one of: Service, Aussicht vom Zimmer"):
        api._topic(1, "Casino", "de-de", german)
    with pytest.raises(ValueError, match="no review topics"):
        api._topic(1, "Pool", "en-us", {"tags": None})


def test_review_body_sends_a_topic_as_the_sites_tag_filter():
    from agoda import fetch
    body = fetch._review_body(185945, 2, 10, 7, [332], [1], [28])
    assert body["searchFilters"] == [{"id": refs.REVIEW_TOPIC_FILTER, "value": [28]}] and body["searchKeyword"] == ""
    assert body["filters"] == {"language": [1], "room": []} and body["providerIds"] == [332] and body["page"] == 2
    assert fetch._review_body(185945)["searchFilters"] == []


def test_fetch_reviews_asks_an_empty_page_again(monkeypatch):
    from agoda import fetch
    empty = {"comments": [], "providerList": []}
    full = {"comments": [{"hotelReviewId": 1}], "providerList": [{"id": 332, "totalIndex": 1}]}
    answers, calls = [empty, empty, full], []
    monkeypatch.setattr(fetch, "_post_reviews", lambda *a, **k: calls.append(1) or answers.pop(0))
    monkeypatch.setattr(fetch, "_drop_session", lambda: None)
    assert fetch.fetch_reviews(185945) is full and len(calls) == 3
    # a genuinely empty page (past the end) is believed once the retries are spent
    answers[:], calls[:] = [empty] * (fetch.EMPTY_PAGE_RETRIES + 1), []
    assert fetch.fetch_reviews(185945, page=999) == empty and len(calls) == fetch.EMPTY_PAGE_RETRIES + 1


def test_room_level_filters_turn_on_the_sites_pricing_feature():
    from agoda import fetch
    flags = lambda ids: fetch.search_variables("city", 9395, "2026-12-12", "2026-12-13", ids_filters=ids)[
        "PricingSummaryRequest"]["pricing"]["featureFlag"]
    assert "RoomAmenityFilter" in flags([{"filterKey": "GroupedBedTypes", "ids": [5]}])
    assert "RoomAmenityFilter" in flags([{"filterKey": "RoomAmenities", "ids": [25]}])
    assert "RoomAmenityFilter" not in flags([{"filterKey": "HotelFacilities", "ids": [93]}])
    assert "RoomAmenityFilter" not in flags(None)


def _search_page(monkeypatch, payload, **params):
    """hotels/search through api._search with the site's answer stubbed."""
    from agoda import api, schemas
    monkeypatch.setattr(api, "fetch_search", lambda *a, **k: payload)
    return api._search(**schemas.SearchSchema().load({"location": "city:9395", **params}))


def test_an_impossible_site_total_is_no_total(monkeypatch):
    # 2026-11-10: an area search with filters answered totalFilteredHotels 0
    # next to hotels; paging must follow the page, not stop after it
    page = fixture("search_city_bangkok_page1.json")
    organic = sum(1 for p in page["properties"] if not (p.get("sponsoredDetail") or {}).get("isShowSponsoredFlag"))
    page["searchResult"]["searchInfo"]["totalFilteredHotels"] = 0
    out = _search_page(monkeypatch, page, limit=str(organic))
    assert out["results"] and out["pagination"]["total_count"] is None
    assert out["pagination"]["has_next"] is True and out["pagination"]["total_pages"] is None
    # the site's own page: 45 bookable rows (6 of them sponsored) + 4 sold-out extras is a FULL page
    out = _search_page(monkeypatch, page)
    assert out["pagination"]["items_per_page"] == 45 and len(out["results"]) == 49
    assert out["pagination"]["total_count"] is None and out["pagination"]["has_next"] is True
    page["properties"] = page["properties"][:3]
    out = _search_page(monkeypatch, page, limit="45")
    assert out["pagination"]["has_next"] is False


def test_sold_out_extras_do_not_make_a_total_impossible(monkeypatch):
    # Victoria: 10 bookable rows + 1 sold-out extra on a page of 10; a total
    # of exactly 10 is the whole answer, not a contradiction (and no next page)
    page = fixture("search_region_victoria.json")
    assert sum(1 for r in P.parse_search(page)["results"] if not r["is_sold_out"]) == 10
    page["searchResult"]["searchInfo"]["totalFilteredHotels"] = 10
    out = _search_page(monkeypatch, page, limit="10")
    assert len(out["results"]) == 11
    assert out["pagination"]["total_count"] == 10 and out["pagination"]["total_pages"] == 1


def test_a_page_past_the_end_keeps_the_site_total(monkeypatch):
    # Singapore: 738 hotels, 17 pages of 45; page 18 and beyond list nothing
    past = fixture("search_city_singapore_past_end.json")
    for page in ("18", "200"):
        out = _search_page(monkeypatch, past, page=page)
        assert out["results"] == [] and out["pagination"]["total_count"] == 738
        assert out["pagination"]["total_pages"] == 17 and "has_next" not in out["pagination"]


def test_price_sorts_put_unpriced_properties_last(monkeypatch):
    page = fixture("search_city_bangkok_page1.json")
    page["properties"][0]["pricing"] = {"isAvailable": False, "isReady": False, "offers": []}
    first = page["properties"][0]["propertyId"]
    site_order = [r["id"] for r in _search_page(monkeypatch, page)["results"]]
    assert site_order[0] == first                                     # recommended keeps the site's order
    cheapest = _search_page(monkeypatch, page, sort="price_low_to_high")["results"]
    priced = [bool((r["price"] or {}).get("per_night")) for r in cheapest]
    assert priced == sorted(priced, reverse=True) and not priced[-1]  # every priced row before every unpriced one
    for group in (True, False):                                       # each group in the site's own order
        ids = [r["id"] for r, p in zip(cheapest, priced) if p is group]
        assert ids == [i for i in site_order if i in ids]
    assert first in [r["id"] for r, p in zip(cheapest, priced) if not p]


def test_empty_review_pages_are_never_cached():
    from agoda import cache_config
    assert cache_config.reviews_ttl({}, {"reviews": []}) is None
    assert cache_config.reviews_ttl({}, {"reviews": [{"id": 1}]}) == cache_config.REVIEWS_CACHE

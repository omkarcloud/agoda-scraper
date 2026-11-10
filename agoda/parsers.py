"""Raw Agoda JSON -> clean dicts for every /agoda/* endpoint (pure
functions, no I/O).

Sources (agoda/fetch.py): the GetUnifiedSuggestResult autocomplete rows, the
citySearch / areaSearch / landmarkSearch GraphQL result, the GetSecondaryData
property JSON (hotelInfo / aboutHotel / mosaicInitData / reviews /
essentialInfo / roomGridData ...), the ReviewComments page and the
HotelReviews summary.

Value conventions (shared with the other scraper packages):
  * keys    snake_case; `link` not `url`; booleans as questions
  * money   numbers plus one `currency` per block, never formatted strings
  * dates   ISO: "2026-09-16T08:16:48+07:00" -> "2026-09-16"; timestamps
            keep their offset where the site gives one
  * missing null (never "", 0-as-unknown, or an omitted key); presentation
            strings (icons, CSS classes, "Booked 18 times today" as text),
            tracking ids (searchId, uid, encryptedReviewData, booking form
            arguments) and the site's A/B flags are dropped

Fixtures captured 2026-11-09 (agoda/fixtures/, Bangkok cityId 9395 and
Palette Bella B All Suites, property 9059812): autocomplete rows for a city,
a hotel name and a nonsense string; a citySearch page, an areaSearch page, a
landmarkSearch page and a past-the-end page; a dated and a dateless details
record plus the unknown-id shell; a reviews page, a Booking.com-source page,
a past-the-end page, the unknown-id page and the two review summaries.
"""
import html as _html
import re
from datetime import datetime

from agoda.refs import (LANGUAGE_IDS, PROVIDER_NAMES, TRAVELER_TYPES, URL_KEY_TO_KIND, URL_KEYS_BY_SPECIFICITY,
                        absolute_link, hotel_link, location_key, search_link)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_DISTANCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(km|kilometers?|m|meters?)\b", re.I)
_ROOM_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m²", re.I)
_PERCENT_RE = re.compile(r"(\d+)\s*%")
_LANGUAGE_BY_ID = {v: k for k, v in LANGUAGE_IDS.items()}

# the filter groups of the search aggregation worth returning, with readable names
FILTER_GROUPS = {
    "StarRatingWithLuxury": "stars", "ReviewScore": "guest_rating", "ProductType": "product_types",
    "AccommodationType": "property_types", "HotelFacilities": "amenities", "RoomAmenities": "room_amenities",
    "RoomBenefits": "room_benefits", "GroupedBedTypes": "bed_types", "PaymentOptions": "payment_options",
    "HotelAreaId": "areas", "LandmarkIds": "landmarks", "HotelChainId": "chains", "AtmosphereIds": "atmospheres",
    "TripPurpose": "trip_purposes", "CityCenterDistance": "city_center_distance",
    "AffordableCategory": "budget_categories", "NoOfBedrooms": "bedrooms", "NoOfBeds": "beds",
    "NoOfBathrooms": "bathrooms", "ReviewTopicIds": "review_topics",
    "MetroSubwayStationLandmarkIds": "metro_stations", "TrainStationLandmarkIds": "train_stations",
    "BusStationLandmarkIds": "bus_stations", "BeachAccessTypeIds": "beach_access",
}
_GRADE_NAMES = {"overall": "overall", "cleanliness": "cleanliness", "facilities": "facilities", "location": "location",
                "roomComfort": "room_comfort", "staffPerformance": "service", "valueForMoney": "value_for_money"}
_PAGE_TYPE_KIND = {5: "city", 6: "area", 15: "area", 14: "region", 12: "landmark", 7: "hotel"}
_BREAKDOWN_KEYS = {"hotelRates": "hotel_rates", "agodaPromotions": "agoda_promotions", "priceSummaries": "price_summaries"}


# ---- value helpers -------------------------------------------------------------------------

def _clean(value):
    """Collapse whitespace; '' / placeholders -> None."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    value = _WS_RE.sub(" ", _html.unescape(value)).strip()
    return value or None


def _text(value):
    """HTML fragment -> plain text (tags dropped, <br> as line breaks)."""
    if not value or not isinstance(value, str):
        return None
    value = re.sub(r"<\s*br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</\s*(p|li|div|h[1-6])\s*>", "\n", value, flags=re.I)
    value = _TAG_RE.sub("", value)
    value = _html.unescape(value).replace("​", "")
    lines = [" ".join(line.split()) for line in value.split("\n")]
    text = "\n".join(line for line in lines if line)
    return text or None


def _num(value):
    """Number as given; '34.56' / '1,234' / '12.5 km' and the decimal-comma
    renderings of the non-English locales ('7,7', '1.234,56') -> float/int;
    anything else -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        match = re.search(r"-?\d[\d.,]*", text)
        if not match:
            return None
        raw = match.group(0)
        if "," in raw:
            head, _, tail = raw.rpartition(",")
            if "." not in tail and 1 <= len(tail) <= 2 and (head.count(".") <= 1 or "." not in tail):
                # "7,7" / "34,56" / "1.234,56": the comma is the decimal mark
                raw = head.replace(".", "").replace(",", "") + "." + tail
            else:
                raw = raw.replace(",", "")
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return None
    return None


def _int(value):
    number = _num(value)
    return int(number) if isinstance(number, (int, float)) else None


def _money(value):
    """A price: numbers only, 0 means 'no price' on every Agoda price field."""
    number = _num(value)
    if number is None or number <= 0:
        return None
    return round(float(number), 2)


def _bool(value):
    return value if isinstance(value, bool) else None


def _date(value):
    """'2026-09-16T08:16:48+07:00' / '2026-12-10T00:00:00' -> '2026-09-16'."""
    if not value or not isinstance(value, str):
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else None


def _datetime(value):
    """Keep a full ISO timestamp (with its offset) when the site gives one."""
    if not value or not isinstance(value, str) or not re.match(r"\d{4}-\d{2}-\d{2}T", value):
        return None
    return value


def _coord(value):
    number = _num(value)
    return number if isinstance(number, (int, float)) and number != 0 else None


def _link(path):
    return absolute_link(path) if path else None


def _distance_km(text):
    """'1.37 kilometers from city center' / '300 meters to' -> km."""
    match = _DISTANCE_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    return round(value / 1000, 3) if match.group(2).lower().startswith("m") else value


def _first(items):
    return items[0] if isinstance(items, list) and items else None


# ---- places/autocomplete ---------------------------------------------------------------------

def _row_kind_and_id(row):
    """The suggestion's type from the key its ResultUrl searches with
    (city / area / toparea / region / poi / hotel), PageTypeId as fallback."""
    url = row.get("ResultUrl") or row.get("Url") or ""
    for key in URL_KEYS_BY_SPECIFICITY:     # a hotel / area link may carry its city too
        match = re.search(rf"[?&]{key}=(\d+)", url)
        if match:
            return URL_KEY_TO_KIND[key], int(match.group(1))
    kind = "hotel" if row.get("IsHotel") else _PAGE_TYPE_KIND.get(row.get("PageTypeId"))
    object_id = _int(row.get("ObjectId"))
    return kind, object_id


def parse_autocomplete(payload):
    """GetUnifiedSuggestResult -> [{type, id, name, ...}]; the echo row of the
    typed text (IsDefault) and rows without an id are skipped."""
    results = []
    for row in (payload or {}).get("ViewModelList") or []:
        if not isinstance(row, dict) or row.get("IsDefault") or row.get("IsTitle"):
            continue
        kind, object_id = _row_kind_and_id(row)
        if not kind or not object_id:
            continue
        hotel_count = _int(row.get("NoOfHotels"))
        results.append({
            "type": kind,
            "id": object_id,
            "name": _clean(row.get("Name")),
            "location": location_key(kind, object_id),
            "address": _text(row.get("ResultAddress")),
            "city_id": _int(row.get("CityId")) or None,
            "country_code": (_clean(row.get("CountryISO")) or "").upper() or None,
            "hotel_count": hotel_count if kind != "hotel" and hotel_count else None,
            "latitude": _coord(row.get("Latitude")),
            "longitude": _coord(row.get("Longtitude") or row.get("Longitude")),
            "is_popular": _bool(row.get("IsPopular")),
            "link": search_link(kind, object_id),
        })
    return results


# ---- hotels/search -------------------------------------------------------------------------------

def _grades(groups, group_id=0):
    """The demographics group's grades -> {overall, cleanliness, ...}."""
    for group in groups or []:
        if (group or {}).get("id") == group_id:
            out = {}
            for grade in group.get("grades") or []:
                key = _GRADE_NAMES.get(grade.get("id"))
                if key and isinstance(grade.get("score"), (int, float)):
                    out[key] = grade["score"]
            return out or None
    return None


def _grades_by_traveler(groups):
    out = {}
    for group in groups or []:
        gid = (group or {}).get("id")
        if gid in (None, 0):
            continue
        name = TRAVELER_TYPES.get(gid)
        overall = next((g.get("score") for g in group.get("grades") or [] if g.get("id") == "overall"), None)
        if name and isinstance(overall, (int, float)):
            out[name] = overall
    return out or None


def _search_rating(content, topics):
    reviews = (content or {}).get("reviews") or {}
    default = next((r for r in reviews.get("contentReview") or [] if (r or {}).get("isDefault")), None) \
        or _first(reviews.get("contentReview"))
    cumulative = (default or {}).get("cumulative") or reviews.get("cumulative") or {}
    summaries = (default or {}).get("summaries") or {}
    snippet = _first(summaries.get("snippets"))
    recommendation = _first(summaries.get("recommendationScores"))
    groups = ((default or {}).get("demographics") or {}).get("groups")
    return {
        "score": cumulative.get("score") if isinstance(cumulative.get("score"), (int, float)) and cumulative.get("score") else None,
        "count": _int(cumulative.get("reviewCount")) or None,
        "recommendation_percent": _int((recommendation or {}).get("recommendationScore")) or None,
        "grades": _grades(groups),
        "score_by_traveler_type": _grades_by_traveler(groups),
        "snippet": {
            "text": _clean(snippet.get("snippet")),
            "score": snippet.get("reviewRating") if isinstance(snippet.get("reviewRating"), (int, float)) else None,
            "reviewer": _clean(snippet.get("reviewer")),
            "country": _clean(snippet.get("countryName")),
            "country_code": _clean(snippet.get("countryCode")),
            "traveler_type": _clean(snippet.get("demographicName")),
            "date": _date(snippet.get("date")),
        } if snippet else None,
        "topics": [{"topic": _clean(t.get("topic")), "snippets": [s for s in (t.get("reviewSnippets") or []) if s]}
                   for t in ((topics or {}).get("topics") or []) if isinstance(t, dict)] or None,
    }


def _cheapest_room(pricing):
    offer = _first((pricing or {}).get("offers"))
    room_offer = _first((offer or {}).get("roomOffers"))
    return (room_offer or {}).get("room") or {}


def _search_price(pricing, sold_out):
    room = _cheapest_room(pricing)
    price_row = _first(room.get("pricing")) or {}
    price = price_row.get("price") or {}
    per_night = price.get("perRoomPerNight") or price.get("perNight") or {}
    per_book = price.get("perBook") or {}
    exclusive, inclusive = per_night.get("exclusive") or {}, per_night.get("inclusive") or {}
    promotions = [p for p in room.get("promotions") or [] if isinstance(p, dict)]
    discount = max((_num(((p.get("promotionDiscount") or {}).get("value"))) or 0 for p in promotions), default=0)
    currency = _clean(price_row.get("currency"))
    sold_out_price = _money(((sold_out or {}).get("soldOutPrice") or {}).get("averagePrice"))
    if not currency and not sold_out_price:
        return None
    display = _money(exclusive.get("display"))
    crossed = _money(exclusive.get("crossedOutPrice"))
    original = _money(exclusive.get("originalPrice"))
    before = crossed or (original if original and display and original > display else None)
    if not discount and before and display and before > display:
        discount = round((1 - display / before) * 100)
    return {
        "currency": currency,
        "per_night": display,
        "per_night_with_taxes": _money(inclusive.get("display")),
        "total": _money((per_book.get("exclusive") or {}).get("display")),
        "total_with_taxes": _money((per_book.get("inclusive") or {}).get("display")),
        "before_discount_per_night": before,
        "discount_percent": int(discount) if discount else None,
        "promotions": [_clean(p.get("description")) for p in promotions if isinstance(p, dict) and _clean(p.get("description"))] or None,
        "sold_out_average_per_night": sold_out_price,
    }


def _search_room(pricing, enrichment):
    room = _cheapest_room(pricing)
    info = (enrichment or {}).get("roomInformation") or {}
    if not room and not info:
        return None
    return {
        "available_rooms": _int(room.get("availableRooms")),
        "size_sqm": _num(info.get("cheapestRoomSizeSqm")) or None,
        "facilities": [_clean(f.get("propertyFacilityName")) for f in info.get("facilities") or []
                       if isinstance(f, dict) and _clean(f.get("propertyFacilityName"))] or None,
        "deals": [_clean(d) for d in ((room.get("discount") or {}).get("deals") or []) if _clean(d)] or None,
        "channel": _clean((room.get("discount") or {}).get("channelDiscount")),
        "has_extra_beds": _bool((room.get("capacity") or {}).get("extraBedsAvailable")),
        "supplier_id": _int(room.get("supplierId")),
    }


def _landmark_list(items):
    out = [{"name": _clean(i.get("landmarkName")), "distance_m": _num(i.get("distanceInM"))}
           for i in items or [] if isinstance(i, dict) and _clean(i.get("landmarkName"))]
    return out or None


def _search_record(prop):
    content = prop.get("content") or {}
    info = content.get("informationSummary") or {}
    address = info.get("address") or {}
    geo = info.get("geoInfo") or {}
    pricing = prop.get("pricing") or {}
    payment = pricing.get("payment") or {}
    cancellation = payment.get("cancellation") or {}
    enrichment = prop.get("enrichment") or {}
    history = enrichment.get("bookingHistory") or {}
    booking_count = history.get("bookingCount") or {}
    local = content.get("localInformation") or {}
    landmarks = local.get("landmarks") or {}
    highlight = content.get("highlight") or {}
    family = content.get("familyFeatures") or {}
    nha = content.get("nonHotelAccommodation") or {}
    unit = _first(nha.get("masterRooms")) or {}
    sponsored = prop.get("sponsoredDetail") or {}
    supplier = pricing.get("supplierInfo") or {}
    renovation = ((info.get("remarks") or {}).get("renovationInfo")) or {}
    character = info.get("hotelCharacter") or {}
    engagement = content.get("propertyEngagement") or {}
    page_path = (info.get("propertyLinks") or {}).get("propertyPage")
    stars = _num(info.get("rating"))
    return {
        "id": _int(prop.get("propertyId")),
        "name": _clean(info.get("displayName")) or _clean(info.get("localeName")) or _clean(info.get("defaultName")),
        "english_name": _clean(info.get("defaultName")),
        "link": _link(page_path),
        "type": _clean((info.get("accommodation") or {}).get("accommodationName")) or _clean(info.get("propertyType")),
        "type_id": _int(info.get("accommodationType")),
        "stars": stars if stars else None,
        "is_official_star_rating": _bool(info.get("isGovernmentVerifiedStars")),
        "is_available": _bool(pricing.get("isAvailable")),
        "is_sold_out": prop.get("propertyResultType") == "SoldOutProperty",
        "is_sponsored": bool(sponsored.get("sponsoredType") and sponsored.get("sponsoredType") != "NoSponsored"),
        "is_sustainable": _bool(info.get("isSustainableTravel")),
        "award_year": _int(info.get("awardYear")) or None,
        "location": {
            "country": _clean((address.get("country") or {}).get("name")),
            "country_code": _clean(address.get("countryCode")),
            "city": _clean((address.get("city") or {}).get("name")),
            "city_id": _int((address.get("city") or {}).get("id")),
            "area": _clean((address.get("area") or {}).get("name")),
            "area_id": _int((address.get("area") or {}).get("id")),
            "latitude": _coord(geo.get("latitude")),
            "longitude": _coord(geo.get("longitude")),
            # the site's figure is in metres (7092.6 for a Bang Na hotel 7 km out)
            "distance_from_city_center_km": round(_num((highlight.get("cityCenter") or {}).get("distanceFromCityCenter")) / 1000, 2)
            if _num((highlight.get("cityCenter") or {}).get("distanceFromCityCenter")) else None,
            "has_nearby_public_transportation": _bool(highlight.get("hasNearbyPublicTransportation")),
            "has_airport_transfer": _bool(local.get("hasAirportTransfer")),
            "landmarks": {
                "transportation": _landmark_list(landmarks.get("transportation")),
                "top": _landmark_list(landmarks.get("topLandmark")),
                "beach": _landmark_list(landmarks.get("beach")),
            } if landmarks else None,
        },
        "rating": _search_rating(content, prop.get("contentTopics")),
        "price": _search_price(pricing, prop.get("soldOut")),
        "cheapest_room": _search_room(pricing, enrichment),
        "cancellation": {
            "type": _clean(cancellation.get("cancellationType")),
            "is_free": cancellation.get("cancellationType") == "FreeCancellation" if cancellation.get("cancellationType") else None,
            "free_until": _date(cancellation.get("freeCancellationDate")),
        } if cancellation else None,
        "payment": {
            "is_pay_later": _bool((payment.get("payLater") or {}).get("isEligible")),
            "is_pay_at_hotel": _bool((payment.get("payAtHotel") or {}).get("isEligible")),
            "is_no_credit_card": _bool((payment.get("noCreditCard") or {}).get("isEligible")),
            "is_easy_cancel": _bool(pricing.get("isEasyCancel")),
            "is_insider_deal": _bool(pricing.get("isInsiderDeal")),
            "free_children": _int((pricing.get("childPolicy") or {}).get("freeChildren")),
        } if payment or pricing.get("isAvailable") else None,
        "supplier": {"id": _int(supplier.get("id")), "name": _clean(supplier.get("name")),
                     "is_agoda": _bool(supplier.get("isAgodaBand"))} if supplier else None,
        "highlights": {
            "favorite_features": [_clean(f.get("title")) for f in ((highlight.get("favoriteFeatures") or {}).get("features") or [])
                                  if isinstance(f, dict) and _clean(f.get("title"))] or None,
            "unique_attributes": [_clean(u.get("summary")) for u in highlight.get("uniqueAttributes") or []
                                  if isinstance(u, dict) and _clean(u.get("summary"))] or None,
            "selling_points": [{"type": _clean(t.get("tspType")), "value": _num(t.get("value"))}
                               for t in enrichment.get("topSellingPoint") or [] if isinstance(t, dict)] or None,
            "character": [_clean(x.get("name")) for x in [*(character.get("hotelTag") or []), *(character.get("hotelView") or [])]
                          if isinstance(x, dict) and _clean(x.get("name"))] or None,
            "is_popular": _bool(enrichment.get("isPopular")),
            "is_top_rated_in_city": _bool(enrichment.get("isTopRatedInCity")),
        },
        "family": {
            "has_children_free_policy": _bool(family.get("hasChildrenFreePolicy")),
            "has_family_room": _bool(family.get("isFamilyRoom")),
            "has_more_than_one_bedroom": _bool(family.get("hasMoreThanOneBedroom")),
            "has_interconnecting_room": _bool(family.get("isInterConnectingRoom")),
            "has_infant_cot": _bool(family.get("isInfantCottageAvailable")),
            "has_kids_pool": _bool(family.get("hasKidsPool")),
            "has_kids_club": _bool(family.get("hasKidsClub")),
        } if family else None,
        "home": {
            "bedrooms": _int(unit.get("noOfBedrooms")),
            "bathrooms": _int(unit.get("noOfBathrooms")),
            "beds": _int(unit.get("noOfBeds")),
            "size_sqm": _num(unit.get("roomSizeSqm")) or None,
            "highlighted_facilities": [_clean(f) for f in unit.get("highlightedFacilities") or [] if _clean(f)] or None,
            "host_level": _clean((nha.get("hostLevel") or {}).get("name")),
            "host_property_count": _int((info.get("nhaSummary") or {}).get("hostPropertyCount")) or None,
            "supports_long_stay": _bool(nha.get("supportedLongStay")),
        } if unit or nha.get("hostLevel") else None,
        "engagement": {
            "bookings_last_24h": _int(booking_count.get("count")) if booking_count.get("timeFrame") == "PastTwentyFourHours" else None,
            "last_booking": _datetime(history.get("lastBooking") or engagement.get("lastBooking")),
            "people_looking": _int(engagement.get("peopleLooking")) or None,
        },
        "renovation": {"type": _clean(renovation.get("renovationType")), "year": _int(renovation.get("year"))} if renovation else None,
        "spoken_languages": [_LANGUAGE_BY_ID[l["id"]] for l in info.get("spokenLanguages") or []
                             if isinstance(l, dict) and l.get("id") in _LANGUAGE_BY_ID] or None,
        "images": [{"link": _link((_first(i.get("urls")) or {}).get("value")), "caption": _clean(i.get("caption"))}
                   for i in ((content.get("images") or {}).get("hotelImages") or [])
                   if isinstance(i, dict) and (_first(i.get("urls")) or {}).get("value")] or None,
    }


def _filters(aggregation):
    out = {}
    for group in (aggregation or {}).get("matrixGroupResults") or []:
        name = FILTER_GROUPS.get((group or {}).get("matrixGroup"))
        if not name:
            continue
        items = [{"id": _int(i.get("id")), "name": _clean(i.get("name")), "count": _int(i.get("count"))}
                 for i in group.get("matrixItemResults") or [] if isinstance(i, dict) and i.get("id") is not None]
        if name == "stars":
            for item in items:     # the site prints the star rows without a label
                if item["name"] is None and item["id"] is not None and item["id"] <= 5:
                    item["name"] = f"{item['id']} stars" if item["id"] != 1 else "1 star"
        if items:
            out[name] = items
    return out or None


def parse_search(data):
    """A search operation's result -> {destination, total_count, is_complete,
    results, price_histogram, filters}."""
    data = data or {}
    search_result = data.get("searchResult") or {}
    info = search_result.get("searchInfo") or {}
    obj = info.get("objectInfo") or {}
    bins = (search_result.get("histogram") or {}).get("bins") or []
    return {
        "destination": {
            "name": _clean(obj.get("objectName")),
            "city": _clean(obj.get("cityName")) or _clean(obj.get("cityEnglishName")),
            "city_id": _int(obj.get("cityId")) or None,
            "country": _clean(obj.get("countryEnglishName")),
            "country_id": _int(obj.get("countryId")) or None,
            "latitude": _coord(obj.get("mapLatitude")),
            "longitude": _coord(obj.get("mapLongitude")),
        } if obj else None,
        "total_count": _int(info.get("totalFilteredHotels")),
        "is_complete": _bool(info.get("isComplete")),
        "results": [_search_record(p) for p in data.get("properties") or [] if isinstance(p, dict)],
        "price_histogram": [{"max_price_per_night": _num((b.get("upperBound") or {}).get("perNightPerRoom")),
                             "count": _int(b.get("numOfElements"))} for b in bins if isinstance(b, dict)] or None,
        "filters": _filters(data.get("aggregation")),
    }


# ---- hotels/details --------------------------------------------------------------------------------

def _feature_groups(groups):
    out = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        items = [_clean(f.get("name")) for f in group.get("feature") or []
                 if isinstance(f, dict) and _clean(f.get("name")) and f.get("available", True)]
        if items:
            out.append({"group": _clean(group.get("name")), "items": items})
    return out or None


def _useful_info(groups):
    out = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        items = [{"title": _clean(i.get("title")), "value": _clean(i.get("description"))}
                 for i in group.get("items") or [] if isinstance(i, dict) and _clean(i.get("title"))]
        if items:
            out.append({"group": _clean(group.get("name")), "items": items})
    return out or None


def _useful_value(groups, item_id):
    for group in groups or []:
        for item in (group or {}).get("items") or []:
            if (item or {}).get("id") == item_id:
                return _clean(item.get("description"))
    return None


def _policies(about):
    policy = (about or {}).get("hotelPolicy") or {}
    guest = (about or {}).get("guestPolicies") or {}
    children = [{"age_range": _clean(c.get("policy")), "policy": _clean(c.get("description")),
                 "is_infant": _bool(c.get("isInfant"))} for c in policy.get("childPolicies") or [] if isinstance(c, dict)]
    guest_rules = []
    for key in ("infantPolicy", "childPolicy", "adultChildPolicy"):
        rule = guest.get(key) or {}
        if rule.get("ageDescription") or rule.get("policy"):
            guest_rules.append({"age_range": _clean(rule.get("ageDescription")), "policy": _clean(rule.get("policy")),
                                "notes": _clean(rule.get("additionalInfo"))})
    return {
        "children": children or None,
        "guest_rules": guest_rules or None,
        "minimum_guest_age": _clean(guest.get("minimumAgeOfGuest")),
        "extra_beds": _clean(guest.get("extraBedPolicy")) or _clean((_first(policy.get("extrabedPolicies")) or {}).get("description")),
        "notes": [_clean(n) for n in policy.get("policyNotes") or [] if _clean(n)] or None,
        "other": [_clean(n) for n in (about or {}).get("otherPolicies") or [] if _clean(n)] or None,
        "important_notes": [_clean(n) for n in (about or {}).get("importantNotes") or [] if _clean(n)] or None,
    }


def _images(mosaic):
    images = []
    for image in (mosaic or {}).get("images") or []:
        if not isinstance(image, dict) or not image.get("location"):
            continue
        images.append({
            "id": _int(image.get("id")),
            "link": _link(image.get("location")),
            "thumbnail": _link(image.get("locationWithSquareSize")) or None,
            "caption": _clean(image.get("title")),
            "category": _clean(image.get("group")),
            "is_room_image": _bool(image.get("isRoomImage")),
            "is_hd": _bool(image.get("isLocationHd")),
        })
    return images or None


def _review_summary_block(reviews):
    demographic = (reviews or {}).get("demographic") or {}
    grades = {}
    for grade in demographic.get("grades") or []:
        key = _GRADE_NAMES.get((grade or {}).get("id"))
        if key and isinstance(grade.get("score"), (int, float)):
            grades[key] = {"score": grade["score"], "city_average": _num(grade.get("cityAverage"))}
    distribution = [{"band": _clean(d.get("id")), "label": _clean(d.get("scoreText")), "count": _int(d.get("count"))}
                    for d in demographic.get("scoreDistribution") or [] if isinstance(d, dict)]
    # demographic.score is numeric; the top-level score is the locale's string ("7,7" in de-de)
    score = _num(demographic.get("score")) or _num((reviews or {}).get("score"))
    sub = demographic.get("subReview") or {}
    return {
        "score": score if score else None,
        "score_text": _clean((reviews or {}).get("scoreText")) or _clean(demographic.get("scoreText")),
        "count": _int((reviews or {}).get("reviewsCount")) or _int(demographic.get("count")) or None,
        "grades": grades or None,
        "score_distribution": distribution or None,
        "best_grade": {"name": _clean(sub.get("name")), "score": _num(sub.get("score")), "text": _clean(sub.get("text"))} if sub else None,
        "city_average_text": _clean(demographic.get("cityAverageText")),
    }


def _attraction(text):
    """'Yaowarat Road (Bangkok Chinatown) - 2.18 km from property' -> parts."""
    text = _clean(text)
    if not text:
        return None
    name, _, tail = text.rpartition(" - ")
    if not name:
        return {"name": text, "distance_km": None}
    return {"name": name, "distance_km": _distance_km(tail)}


def _nearby(data):
    essentials = []
    for category in ((data.get("essentialInfo") or {}).get("nearbyProperties") or []):
        if not isinstance(category, dict):
            continue
        places = [{"name": _clean(p.get("name")), "distance_km": _num(p.get("distance")),
                   "latitude": _coord(p.get("latitude")), "longitude": _coord(p.get("longitude"))}
                  for p in category.get("places") or [] if isinstance(p, dict) and _clean(p.get("name"))]
        if places:
            essentials.append({"category": _clean(category.get("categoryName")), "places": places})
    landmarks = []
    for category in (((data.get("aboutHotel") or {}).get("placesOfInterest") or {}).get("placesOfInterestProperties") or []):
        if not isinstance(category, dict):
            continue
        places = [{"id": _int(p.get("landmarkId")) or None, "name": _clean(p.get("name")), "type": _clean(p.get("landmarkTypeName")),
                   "distance_km": _num(p.get("distance")), "latitude": _coord(p.get("latitude")),
                   "longitude": _coord(p.get("longitude")), "image": _link(p.get("picturePath"))}
                  for p in category.get("places") or [] if isinstance(p, dict) and _clean(p.get("name"))]
        if places:
            landmarks.append({"category": _clean(category.get("categoryName")), "places": places})
    attractions = [_attraction(a.get("name")) for a in ((data.get("nearbyAttractionInfo") or {}).get("highLights") or [])
                   if isinstance(a, dict)]
    return {"essentials": essentials or None, "landmarks": landmarks or None,
            "attractions": [a for a in attractions if a] or None}


def _room_features(features):
    """A master room's features -> size / view / bed / extras. The site
    reuses feature id 15 for the bed line AND "Non-smoking" (told apart by
    the icon), so the bed is the first id-15 row with the bed icon."""
    size, view, bed, extras = None, None, None, []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        title = _clean(feature.get("title")) or _clean(feature.get("text"))
        if not title:
            continue
        fid = feature.get("id")
        symbol = feature.get("symbol") or ""
        if fid == 2 or title.lower().startswith("room size"):
            match = _ROOM_SIZE_RE.search(title)
            size = float(match.group(1)) if match else size
        elif fid == 1 or title.lower().endswith(" view"):
            view = title
        elif bed is None and (symbol == "ficon-bed" or (fid == 15 and not symbol)):
            bed = title
        else:
            extras.append(title)
    return size, view, bed, extras or None


def _offer(room, currency):
    pricing = room.get("pricing") or {}
    cancellation = room.get("cancellation") or {}
    pay_later = room.get("payLater") or {}
    promotion = room.get("promotion") or {}
    pulse = room.get("pulseCampaignInfo") or {}
    extra_bed = room.get("extraBedInfo") or {}
    exclusive, inclusive, total = room.get("exclusivePrice") or {}, room.get("inclusivePrice") or {}, room.get("totalPrice") or {}
    discount = _int(room.get("discountPercentage"))
    return {
        "id": _int(room.get("id")),
        "rate_plan_id": _int(room.get("ratePlanId")),
        "name": _clean(room.get("name")),
        "price": {
            "currency": _clean(room.get("currency")) or currency,
            "per_night": _money(exclusive.get("display")) or _money(pricing.get("displayPrice")),
            "per_night_with_taxes": _money(inclusive.get("display")),
            "total": _money(total.get("display")),
            "before_discount_per_night": _money(exclusive.get("crossedOut")) or _money(pricing.get("crossOutRatePrice")),
            "discount_percent": discount if discount else None,
            "extra_bed_per_night": _money(exclusive.get("extraBed")),
            "taxes_and_fees": [_clean(t) for t in room.get("taxesAndSurchargesList") or [] if _clean(t)] or None,
            "is_tax_free": _bool(pricing.get("isTaxFree")),
        },
        "occupancy": {
            "adults": _int(room.get("adults")),
            "children": _int(room.get("children")),
            "max_guests": _int(room.get("maxOccupancy")),
            "free_children": _int(room.get("maxFreeChildren")),
            "max_extra_beds": _int(extra_bed.get("maxExtraBed")),
        },
        "rooms_left": _int(room.get("availability")),
        "is_breakfast_included": _bool(room.get("isBreakfastIncluded")),
        "is_free_cancellation": _bool(room.get("isFreeCancellation")),
        "cancellation": {"title": _clean(cancellation.get("title")), "policy": _text(cancellation.get("description")),
                         "code": _clean(cancellation.get("extra"))} if cancellation else None,
        "is_pay_later": _bool(pay_later.get("isAvailable")),
        "pay_later": _clean(pay_later.get("description")) if pay_later.get("isAvailable") else None,
        "is_book_on_request": _bool(room.get("isBookOnRequest")),
        "payment_model": {1: "merchant", 2: "agency"}.get(room.get("paymentModel"), _clean(str(room.get("paymentModel"))) if room.get("paymentModel") is not None else None),
        "benefits": [_clean(b.get("title")) for b in room.get("benefits") or [] if isinstance(b, dict) and _clean(b.get("title"))] or None,
        "features": [_clean(f.get("title")) for f in room.get("features") or [] if isinstance(f, dict) and _clean(f.get("title"))] or None,
        "promotion": {"title": _clean(promotion.get("title")), "description": _clean(promotion.get("description"))}
        if promotion.get("isAvailable") else None,
        "campaign": _clean(pulse.get("campaignBadgeText")) if pulse else None,
        "supplier": {"id": _int(room.get("supplierId")), "name": _clean(room.get("supplierDisplayName")),
                     "is_agoda": _bool(room.get("isAgodaBrand"))},
        "price_breakdown": {
            _BREAKDOWN_KEYS.get(key, key): [{"item": _clean(i.get("itemText")), "amount": _num(i.get("amount"))}
                                            for i in rows if isinstance(i, dict)]
            for key, rows in ((room.get("corBreakdown") or {}).items()) if isinstance(rows, list) and rows
        } or None,
    }


def _master_room(master, currency):
    size, view, bed, extras = _room_features(master.get("features"))
    layouts = []
    for layout in master.get("bedroomLayouts") or []:
        for bedroom in (layout or {}).get("bedrooms") or []:
            beds = [{"name": _clean(b.get("name")), "quantity": _int(b.get("quantity"))} for b in (bedroom or {}).get("beds") or []
                    if isinstance(b, dict)]
            if beds:
                layouts.append({"bedroom": _clean(bedroom.get("title")), "beds": beds})
    facility_groups = []
    for group in master.get("facilityGroups") or []:
        items = [_clean(f.get("title")) for f in (group or {}).get("facilities") or [] if isinstance(f, dict) and _clean(f.get("title"))]
        if items:
            facility_groups.append({"group": _clean(group.get("name")), "items": items})
    return {
        "id": _int(master.get("id")),
        "name": _clean(master.get("name")),
        "alternate_name": _clean(master.get("hotelRoomTypeAlternatName")),
        "size_sqm": size,
        "view": view,
        "beds": bed or _clean((master.get("bedConfigurationSummary") or {}).get("title")),
        "bed_layouts": layouts or None,
        "max_occupancy": _int(master.get("maxOccupancy")),
        "max_extra_beds": _int(master.get("maxExtrabeds")),
        "is_recommended": _bool(master.get("isRecommendedRoom")),
        "is_dormitory": _bool(master.get("isDormitory")),
        "cheapest_price": _money(master.get("cheapestPrice")),
        "before_discount_price": _money(master.get("beforeDiscountPrice")),
        "currency": currency,
        "rooms_left": _int(master.get("firstRoomAvailability")) or None,
        "amenities": [_clean(a.get("title")) for a in master.get("amenities") or [] if isinstance(a, dict) and _clean(a.get("title"))] or None,
        "features": extras,
        "facility_groups": facility_groups or None,
        "images": [{"link": _link(u), "caption": _clean(c)} for u, c in
                   zip(master.get("images") or [], (master.get("captions") or []) + [None] * len(master.get("images") or []))
                   if u] or None,
        "urgency": _clean((master.get("urgencyMessage") or {}).get("title")) if (master.get("urgencyMessage") or {}).get("isAvailable") else None,
        "offers": [_offer(r, currency) for r in master.get("rooms") or [] if isinstance(r, dict)],
    }


def _stay(criteria):
    criteria = criteria or {}
    if not (criteria.get("checkInDate") or criteria.get("checkIn")) or not _int(criteria.get("los")):
        return None     # a dateless record: the site echoes today with los 0
    return {
        "checkin": _date(criteria.get("checkInDate") or criteria.get("checkIn")),
        "checkout": _date(criteria.get("checkOutDate") or criteria.get("checkOut")),
        "nights": _int(criteria.get("los")),
        "adults": _int(criteria.get("adults")),
        "children": _int(criteria.get("children")),
        "rooms": _int(criteria.get("rooms")),
    }


def parse_details(data, hotel_id, summary=None):
    """GetSecondaryData (+ a PARSED HotelReviews summary, parse_review_summary's
    dict, when given) -> the full property record. `rooms` carries the live
    room grid for the stay in hotelSearchCriteria (empty when the record was
    fetched dateless)."""
    data = data or {}
    info = data.get("hotelInfo") or {}
    about = data.get("aboutHotel") or {}
    address = info.get("address") or {}
    mosaic = data.get("mosaicInitData") or {}
    latlng = (data.get("mapParams") or {}).get("latlng") or [None, None]
    currency = _clean((data.get("currencyInfo") or {}).get("code")) or _clean((data.get("hotelSearchCriteria") or {}).get("currencyCode"))
    room_grid = data.get("roomGridData") or {}
    useful = about.get("usefulInfoGroups")
    star = info.get("starRating") or {}
    accolades = (info.get("awardsAndAccolades") or {}).get("advanceGuaranteeProgram") or {}
    top_value = info.get("topValue") or {}
    cheaper = data.get("priceCheaperThanSimilarHotels") or {}
    engagement = info.get("engagement") or {}
    features_you_love = data.get("featuresYouLove") or {}
    contact = ((data.get("contactHost") or {}).get("init") or {}).get("params") or {}
    inquiry = data.get("inquiryProperty") or {}
    summary_block = summary if isinstance(summary, dict) and summary else None
    rating = _review_summary_block(data.get("reviews"))
    if summary_block:
        rating.update({k: summary_block[k] for k in ("sources", "tags", "recent_scores") if summary_block.get(k) is not None})
    stars = _num(star.get("value"))
    return {
        "id": _int(data.get("hotelId")) or _int(hotel_id),
        "name": _clean(info.get("name")) or _clean(about.get("hotelName")),
        "english_name": _clean(info.get("englishName")) or _clean(inquiry.get("hotelNameEnglish")),
        "link": (summary_block or {}).get("link"),
        "type": _clean(info.get("accommodationType")),
        "is_home": _bool(info.get("isNHA")),
        "stars": stars if stars else None,
        "description": _text((about.get("hotelDesc") or {}).get("overview")),
        "former_name": _clean((about.get("hotelDesc") or {}).get("hotelFormerName")),
        "location": {
            "address": _clean(address.get("address")),
            "full_address": _clean(address.get("full")),
            "postal_code": _clean(address.get("postalCode")),
            "area": _clean(address.get("areaName")),
            "city": _clean(address.get("cityName")),
            "city_id": _int(address.get("cityId")),
            "country": _clean(address.get("countryName")),
            "country_id": _int(address.get("countryId")),
            "latitude": _coord(latlng[0] if len(latlng) > 0 else None),
            "longitude": _coord(latlng[1] if len(latlng) > 1 else None),
            "distance_from_city_center_km": _distance_km(info.get("locationHighlightMessage")),
            "is_address_obfuscated": _bool((data.get("mapParams") or {}).get("shouldObfuscateAddress")),
        },
        "rating": rating,
        "price": {
            "currency": currency,
            "cheapest_per_night": _money((mosaic.get("discount") or {}).get("cheapestPrice")) or _money(inquiry.get("cheapestPrice")),
            "cheaper_than_city_average_percent": _int(top_value.get("value")) if top_value.get("id") == 9 else _int(cheaper.get("percentage")),
            "cheaper_than_similar_text": _clean(top_value.get("text")),
        },
        "check_in_from": _useful_value(useful, 8),
        "check_out_until": _useful_value(useful, 9),
        "facilities": _feature_groups(about.get("featureGroups")),
        "languages_spoken": [_clean(x) for x in (_clean(contact.get("_supportedLanguages")) or _clean(inquiry.get("supportedLanguages")) or "").split(",") if _clean(x)] or None,
        "useful_info": _useful_info(useful),
        "policies": _policies(about),
        "highlights": [{"text": _clean(f.get("text")), "details": [_clean(i) for i in ((f.get("tooltip") or {}).get("items") or []) if _clean(i)] or None}
                       for f in features_you_love.get("features") or [] if isinstance(f, dict) and _clean(f.get("text"))] or None,
        "nearby": _nearby(data),
        "awards": {"is_agoda_preferred": bool(accolades.get("programText")), "program": _clean(accolades.get("programText")),
                   "description": _clean(accolades.get("description"))} if accolades else None,
        "engagement": {"bookings_today": _int(engagement.get("todayBooking")),
                       "people_looking": _int(engagement.get("peopleLooking")) or None},
        "images": _images(mosaic),
        "image_categories": [{"category": _clean(c.get("category")), "count": _int(c.get("count"))}
                             for c in mosaic.get("imageCategories") or [] if isinstance(c, dict)] or None,
        "videos": [{"link": _link(v.get("location")), "thumbnail": _link(v.get("thumbnailImage"))}
                   for v in mosaic.get("videos") or [] if isinstance(v, dict) and v.get("location")] or None,
        "supplier_id": _int(info.get("supplierId")),
        "stay": _stay(data.get("hotelSearchCriteria")),
        "rooms_fitting_party": _int(data.get("numberOfFitRoom")),
        "taxes_and_fees": _clean((room_grid.get("taxesAndSurcharges") or {}).get("title")),
        "rooms": [_master_room(m, currency) for m in room_grid.get("masterRooms") or [] if isinstance(m, dict)],
    }


# ---- hotels/reviews ---------------------------------------------------------------------------------

def _review(comment):
    reviewer = comment.get("reviewerInfo") or {}
    provider = _int(comment.get("providerId"))
    response_text = _clean(comment.get("responseText"))
    rating = _num(comment.get("rating"))
    return {
        "id": _int(comment.get("hotelReviewId")),
        "score": rating if rating is not None else None,
        "score_text": _clean(comment.get("ratingText")),
        "title": _clean(comment.get("reviewTitle")),
        "text": _clean(comment.get("reviewComments")),
        "positives": _clean(comment.get("reviewPositives")),
        "negatives": _clean(comment.get("reviewNegatives")),
        "date": _date(comment.get("reviewDate")),
        "is_translated": bool(_clean(comment.get("originalComment")) and _clean(comment.get("translateSource"))
                              and comment.get("translateSource") != comment.get("translateTarget")),
        "original": {
            "language": _clean(comment.get("translateSource")),
            "title": _clean(comment.get("originalTitle")),
            "text": _clean(comment.get("originalComment")),
        } if _clean(comment.get("originalComment")) or _clean(comment.get("originalTitle")) else None,
        "stay": {
            "checkin": _date(comment.get("checkInDate")),
            "checkout": _date(comment.get("checkOutDate")),
            "month": _clean(comment.get("checkInDateMonthAndYear")),
            "nights": _int(reviewer.get("lengthOfStay")) or None,
            "room": _clean(reviewer.get("roomTypeName")),
        },
        "reviewer": {
            "name": _clean(reviewer.get("displayMemberName")),
            "country": _clean(reviewer.get("countryName")),
            "country_code": (_clean(reviewer.get("flagName")) or "").upper() or None,
            "traveler_type": _clean(reviewer.get("reviewGroupName")),
            "review_count": _int(reviewer.get("reviewerReviewedCount")) or None,
            "is_expert": _bool(reviewer.get("isExpertReviewer")),
        },
        "helpful_votes": _int(comment.get("helpfulVotes")),
        "images": [{"link": _link(i.get("url")), "caption": _clean(i.get("caption"))} for i in comment.get("ugcImages") or []
                   if isinstance(i, dict) and i.get("url")] or None,
        "response": {
            "text": response_text,
            "responder": _clean(comment.get("responderName")),
            "date": _date(comment.get("responseDate")),
        } if response_text else None,
        "source": PROVIDER_NAMES.get(provider) or _clean(comment.get("reviewProviderText")),
        "source_id": provider,
    }


def parse_reviews(payload):
    """ReviewComments -> {reviews, sort_options, languages, room_types}."""
    payload = payload or {}
    filters = _first(payload.get("reviewFilterAndSort")) or {}
    languages, room_types = None, None
    for block in filters.get("filters") or []:
        if not isinstance(block, dict):
            continue
        items = [{"id": _int(i.get("id")), "name": _clean(i.get("name"))} for i in block.get("filterItems") or []
                 if isinstance(i, dict) and _clean(i.get("name"))]
        if block.get("type") == 2 or (block.get("name") or "").lower() == "language":
            languages = [{"code": _LANGUAGE_BY_ID.get(i["id"]), **i} for i in items] or None
        elif block.get("type") == 3 or "room" in (block.get("name") or "").lower():
            room_types = items or None
    # the page carries `pageSize` rows from EVERY provider, interleaved,
    # whatever providerIds asked for; providerList.totalIndex is each
    # provider's count of comments matching the filters (the real total)
    totals = {}
    for provider in payload.get("providerList") or []:
        if isinstance(provider, dict) and _int(provider.get("id")) is not None:
            totals[_int(provider["id"])] = _int(provider.get("totalIndex"))
    return {
        "reviews": [_review(c) for c in payload.get("comments") or [] if isinstance(c, dict)],
        "totals": totals,
        "sort_options": [{"id": _int(k), "name": _clean(v)} for k, v in (payload.get("reviewsSortOptions") or {}).items()] or None,
        "languages": languages,
        "room_types": room_types,
        "link": hotel_link(payload.get("reviewPageUrl")),
    }


def parse_review_summary(payload):
    """HotelReviews -> {score, grades, score_distribution, sources, tags, ...}."""
    payload = payload or {}
    score_block = payload.get("score") or {}
    demographic = _first(score_block.get("demographics")) or {}
    rating = _review_summary_block({"score": demographic.get("score"), "scoreText": demographic.get("scoreText"),
                                    "reviewsCount": demographic.get("count"), "demographic": demographic})
    tabs = [{"source": PROVIDER_NAMES.get(_int(t.get("providerId")), "Other sites"), "source_id": _int(t.get("providerId")),
             "count": _int(t.get("reviewCount"))} for t in ((payload.get("reviewTabs") or {}).get("reviewTabs") or [])
            if isinstance(t, dict)]
    tags = [{"id": _int(t.get("tagId")), "name": _clean(t.get("tagName")), "mentions": _int(t.get("mentionedNumber")),
             "positive_percent": _num(t.get("positivePercentage")), "negative_percent": _num(t.get("negativePercentage")),
             "rating": _clean(t.get("ratingType"))} for t in ((payload.get("reviewTagsV2") or {}).get("reviewTagsV2") or [])
            if isinstance(t, dict) and _clean(t.get("tagName"))]
    recent = {PROVIDER_NAMES.get(_int(r.get("providerId")), str(r.get("providerId"))): [s for s in r.get("recentReviewScores") or []
                                                                                      if isinstance(s, (int, float))]
              for r in payload.get("recentReviewScores") or [] if isinstance(r, dict)}
    return {
        **rating,
        "sources": tabs or None,
        "tags": tags or None,
        "recent_scores": recent or None,
        "name": _clean(payload.get("hotelName")),
        "link": hotel_link(score_block.get("reviewPageUrl") or ((payload.get("commentList") or {}).get("reviewPageUrl"))),
        "has_reviews": _bool(payload.get("hasReviewsData")),
    }



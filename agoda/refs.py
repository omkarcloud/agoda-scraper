"""Reference resolvers + the vocabulary tables for /agoda/*.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url` / `id` pair):

    hotel      a numeric Agoda property id (9059812), "hotel:9059812", OR any
               agoda.com hotel / reviews page link
               (/bella-b-all-suite-bangkok_4/hotel/bangkok-th.html)
    location   a destination name ("Bangkok", "Sukhumvit", "Terminal 21"), a
               typed key from /agoda/places/autocomplete ("city:9395",
               "area:27713", "landmark:141553", "region:4157",
               "top_area:7", "hotel:286830"), a bare number (= a city id) OR
               an agoda.com search / city / hotel link

Resolvers raise ValueError with a user-facing message (RefField turns it
into a 400). Everything that needs the network (a name -> destination, a
hotel link -> id) is left to api.py so the schemas stay offline.

The id vocabularies (accommodation types, facilities, bed types, review
languages, ...) are Agoda's own, read from the citySearch aggregation and
the ReviewComments filter block on 2026-11-06 (Bangkok, cityId 9395).
"""
import re
from urllib.parse import parse_qs, urlparse

SITE = "https://www.agoda.com"

# Agoda's review providers: its own guests and the Booking.com pool it shows
# on the same page (every other provider id is "additional reviews" the
# page folds into one tab; ReviewComments does not list them).
PROVIDER_AGODA = 332
PROVIDER_BOOKING = 3038
# public `source` value -> provider ids (spelled as literals so the listing
# tooling can read the choices; 332 = PROVIDER_AGODA, 3038 = PROVIDER_BOOKING)
REVIEW_SOURCES = {"agoda": [332], "booking": [3038], "all": [332, 3038]}
PROVIDER_NAMES = {PROVIDER_AGODA: "Agoda", PROVIDER_BOOKING: "Booking.com"}

# ---- site locales -----------------------------------------------------------------
# The storefront languages Agoda renders (the language menu, 2026-11-06). The
# GraphQL `locale`, the AG-LANGUAGE-LOCALE header and the autocomplete path
# take these; a bare language maps to the first locale listed for it.
LOCALES = (
    "en-us", "en-gb", "en-au", "en-ca", "en-in", "en-nz", "en-sg", "en-my", "en-ph", "en-za", "en-ie",
    "zh-cn", "zh-tw", "zh-hk", "ja-jp", "ko-kr", "th-th", "vi-vn", "id-id", "ms-my", "tl-ph",
    "fr-fr", "fr-ca", "de-de", "es-es", "es-mx", "es-ar", "it-it", "pt-pt", "pt-br", "nl-nl", "pl-pl",
    "ru-ru", "uk-ua", "tr-tr", "ar-ae", "he-il", "hi-in", "sv-se", "fi-fi", "da-dk", "nb-no", "cs-cz",
    "sk-sk", "hu-hu", "el-gr", "ro-ro", "bg-bg", "hr-hr", "sl-si",
)
DEFAULT_LOCALE = "en-us"
# The currencies Agoda sells in (GET /api/cronos/layout/currency/getlist,
# AllCurrencyList, 2026-11-10). Any other code answers an empty search (a
# 200 with no hotels), so the schemas reject it up front.
CURRENCIES = (
    "AED", "AFN", "AUD", "BDT", "BHD", "BRL", "CAD", "CHF", "CNY", "COP", "CZK", "DKK", "EGP", "EUR", "FJD", "GBP",
    "HKD", "HUF", "IDR", "ILS", "INR", "JOD", "JPY", "KHR", "KRW", "KWD", "KZT", "LAK", "LKR", "MVR", "MXN", "MYR",
    "NGN", "NOK", "NZD", "OMR", "PHP", "PKR", "PLN", "QAR", "RON", "RUB", "SAR", "SEK", "SGD", "THB", "TRY", "TWD",
    "UAH", "USD", "VND", "XPF", "ZAR",
)
# Agoda's numeric language ids (the autocomplete path's second segment and
# AG-Language-Id), from the review language filter + the site's own list.
LANGUAGE_IDS = {
    "en": 1, "fr": 2, "de": 3, "it": 4, "es": 5, "ja": 6, "zh-hk": 7, "zh-cn": 8, "ko": 9, "el": 10,
    "ru": 11, "pt-pt": 12, "nl": 13, "en-ca": 14, "en-gb": 16, "en-au": 18, "zh-tw": 20, "th": 22,
    "ms": 23, "vi": 24, "sv": 25, "id": 26, "pl": 27, "nb": 28, "da": 29, "fi": 30, "cs": 31, "tr": 32,
    "hu": 34, "ro": 37, "sl": 38, "ar": 40, "pt-br": 43, "uk": 50, "tl": 51,
}


def normalize_locale(value):
    """'fr', 'fr_FR', 'FR-fr' -> 'fr-fr'; raises ValueError for a locale the
    site does not render."""
    value = (value or "").strip().replace("_", "-").lower()
    if not value:
        return DEFAULT_LOCALE
    if value in LOCALES:
        return value
    lang = value.split("-")[0]
    for locale in LOCALES:
        if locale.split("-")[0] == lang:
            return locale
    raise ValueError(f"Unsupported language {value!r}. Use one of: {', '.join(LOCALES)}.")


def language_id(locale):
    """Agoda's numeric language id for a locale (1 = English when unknown)."""
    locale = (locale or DEFAULT_LOCALE).lower()
    return LANGUAGE_IDS.get(locale) or LANGUAGE_IDS.get(locale.split("-")[0]) or 1


# ---- search vocabularies ---------------------------------------------------------------

# public value -> (sortField, sortOrder) of searchCriteria.sorting
SORTS = {
    "recommended": ("Ranking", "Desc"),
    "price_low_to_high": ("Price", "Asc"),
    "price_high_to_low": ("Price", "Desc"),
    "guest_rating": ("AllGuestsReviewScore", "Desc"),
    "secret_deals": ("SecretDeal", "Asc"),
}

# filterKey "ProductType" (ids) — Agoda's hotels / homes split
PRODUCT_TYPES = {"hotels": 1, "homes": 2}
# filterKey "AccommodationType" (ids), names as the site prints them
ACCOMMODATION_TYPES = {
    "hotel": 34, "resort": 37, "apartment": 29, "serviced_apartment": 120, "guesthouse": 32,
    "bed_and_breakfast": 135, "guest_house": 141, "hostel": 33, "motel": 35, "inn": 111, "lodge": 112,
    "villa": 28, "resort_villa": 122, "entire_house": 131, "bungalow": 103, "homestay": 108,
    "country_house": 107, "chalet": 106, "townhouse": 147, "loft": 145, "capsule_hotel": 105,
    "love_hotel": 113, "ryokan": 38, "holiday_park": 110, "igloo": 144,
}
PROPERTY_TYPES = {**PRODUCT_TYPES, **ACCOMMODATION_TYPES}
# filterKey "HotelFacilities" (ids)
AMENITIES = {
    "pool": 93, "internet": 90, "parking": 80, "airport_transfer": 17, "gym": 92, "front_desk_24h": 116,
    "family_friendly": 89, "non_smoking": 15, "spa": 91, "restaurant": 11, "smoking_area": 83,
    "pets_allowed": 24, "nightclub": 10, "disabled_access": 16, "business_facilities": 88, "golf_course": 56,
}
# filterKey "RoomAmenities" (ids)
ROOM_AMENITIES = {
    "tv": 35, "air_conditioning": 25, "refrigerator": 86, "balcony": 43, "washing_machine": 87,
    "internet": 31, "coffee_maker": 47, "ironing": 33, "bathtub": 37, "kitchen": 42, "heating": 143,
    "private_pool": 50, "pets_in_room": 342,
}
# filterKey "GroupedBedTypes" (ids)
BED_TYPES = {"single": 1, "double": 2, "queen": 4, "king": 5, "bunk": 7}
# filterKey "RoomBenefits" (ids)
ROOM_BENEFITS = {"breakfast": 78322, "dinner": 78307, "lunch": 78306, "airport_transfer": 78308,
                 "early_check_in": 78310, "late_check_out": 78309, "free_shuttle": 78312}
# filterKey "PaymentOptions" (ids)
PAYMENT_OPTIONS = {"free_cancellation": 49499, "pay_later": 52770, "pay_at_hotel": 56649,
                   "pay_now": 84640, "no_credit_card": 80031}
# filterKey "ReviewScore" (range: from) — the site's own bands
MIN_SCORES = {"6": 6, "7": 7, "8": 8, "9": 9}
# filterKey "StarRatingWithLuxury" (ids): stars 1-5 plus Agoda Luxe (101)
STAR_LUXE = 101

# a search request's page size: the site renders 45 and caps requests at 100
SEARCH_PAGE_SIZE = 45
SEARCH_MAX_SIZE = 100

# ---- review vocabularies ---------------------------------------------------------------

# sorting codes of ReviewComments (reviewsSortOptions)
REVIEW_SORTS = {"recent": 1, "highest_rating": 2, "lowest_rating": 3, "helpful": 7}
REVIEWS_PAGE_SIZE = 20
REVIEWS_MAX_SIZE = 100
# the language a review was WRITTEN in (filters.language ids, 2026-11-06)
REVIEW_LANGUAGES = {
    "en": 1, "fr": 2, "de": 3, "it": 4, "es": 5, "ja": 6, "zh-hk": 7, "zh-cn": 8, "ko": 9, "el": 10,
    "ru": 11, "pt-pt": 12, "nl": 13, "en-ca": 14, "en-gb": 16, "en-au": 18, "zh-tw": 20, "th": 22,
    "ms": 23, "vi": 24, "sv": 25, "id": 26, "pl": 27, "nb": 28, "da": 29, "fi": 30, "cs": 31, "tr": 32,
    "hu": 34, "ro": 37, "sl": 38, "ar": 40, "pt-br": 43, "uk": 50, "tl": 51,
}
# Agoda's reviewer groups (reviewGroupId / demographicId)
TRAVELER_TYPES = {0: "all", 1: "business", 2: "couple", 3: "solo", 4: "family_with_young_children",
                  5: "family_with_teens", 6: "group"}
# searchFilters type of the site's review topic chips ("reviews that mention"
# Pool / Room view / Service ...): REVIEWTAGV2ID in the site's filter-type
# enum (TRAVELLER 1, LANGUAGE 2, ROOMTYPE 3, GUESTRATING 4, TIMEOFYEAR 5,
# WORDCLOUDID 6, FACILITYCLASSWORDSCLOUDID 7, REVIEWTAGV2ID 8, SOURCE 9).
# Only 8 narrows ReviewComments (2026-11-10: Pool on Marina Bay Sands = 641
# of 3,871, every row about the pool; 6 and 7 change nothing). The values
# are tagIds from the hotel's HotelReviews reviewTagsV2; Agoda reviews only.
REVIEW_TOPIC_FILTER = 8


def review_topic(tags, value):
    """The tag in a parsed review summary's `tags` that `value` names (its
    name in any case and spacing, or its id), or None."""
    wanted = _name_key(str(value or ""))
    if not wanted:
        return None
    for tag in tags or []:
        if wanted == str(tag.get("id")) or wanted == _name_key(tag.get("name")):
            return tag
    return None

# ---- autocomplete row types --------------------------------------------------------------
# The suggestion's ResultUrl carries the id under the key the site searches
# with; that key is the type (PageTypeId is less reliable: 15 covers areas
# AND airports, 14 regions AND transit lines).
URL_KEY_TO_KIND = {"city": "city", "area": "area", "toparea": "top_area", "region": "region",
                   "poi": "landmark", "landmark": "landmark", "hotel": "hotel", "selectedproperty": "hotel"}
# the order a link's keys are read in: the site's own search links carry the
# city NEXT TO the narrower key (?city=9395&area=27713, ?city=9395&selectedproperty=..),
# so the most specific key wins and the city only qualifies it
URL_KEYS_BY_SPECIFICITY = ("hotel", "selectedproperty", "poi", "landmark", "area", "toparea", "region", "city")
# the GraphQL operation + request variable per destination kind
SEARCH_OPS = {
    "city": ("citySearch", "CitySearchRequest", "cityId"),
    "area": ("areaSearch", "AreaSearchRequest", "areaId"),
    "top_area": ("topAreaSearch", "TopAreaSearchRequest", "topAreaId"),
    "region": ("regionSearch", "RegionSearchRequest", "regionId"),
    "landmark": ("landmarkSearch", "LandmarkSearchRequest", "landmarkId"),
}
SEARCHABLE_KINDS = ("city", "area", "top_area", "region", "landmark", "hotel")

# the destination kinds in the order a bare name is taken to mean them: the
# site lists its districts before the city itself ("Embassy District,
# Bangkok" above "Bangkok") and a hotel above the landmark it is named for
KIND_RANK = {"city": 0, "area": 1, "top_area": 2, "landmark": 3, "region": 4, "hotel": 5}
# the state / country qualifier the site appends to an ambiguous place name
# ("Las Vegas (NV)", "Portland (OR)")
_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _name_key(text):
    return " ".join((text or "").lower().split())


def best_suggestion(rows, text):
    """The autocomplete row a typed destination means: a name match first
    (exact, or exact once the site's qualifier is dropped, so "Las Vegas"
    matches "Las Vegas (NV)"), then the broadest place kind, then the site's
    own popularity order. "Las Vegas" is therefore Nevada (listed first),
    not the Argentine town of the same name listed second."""
    if not rows:
        return None
    wanted = _name_key(text)

    def key(item):
        index, row = item
        name = row.get("name") or ""
        matches = _name_key(name) == wanted or _name_key(_QUALIFIER_RE.sub("", name)) == wanted
        return (0 if matches else 1, KIND_RANK.get(row.get("type"), 9), index)
    return min(enumerate(rows), key=key)[1]

# ---- reference resolvers -------------------------------------------------------------------

_HOTEL_PATH_RE = re.compile(r"^/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9][a-z0-9_-]*)/(hotel|reviews|photos|maps?)/([a-z0-9-]+)\.html$", re.I)
_CITY_PATH_RE = re.compile(r"^/(?:[a-z]{2}-[a-z]{2}/)?city/([a-z0-9-]+)-([a-z]{2})\.html$", re.I)
_TYPED_RE = re.compile(r"^(city|area|top_area|toparea|region|landmark|poi|hotel|property)\s*[:=]\s*(\d{1,12})$", re.I)
_KIND_ALIASES = {"toparea": "top_area", "poi": "landmark", "property": "hotel"}


def _is_agoda(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "agoda.com" or host.endswith(".agoda.com")


def _as_url(value):
    return value if not value.startswith("//") else "https:" + value


def hotel_path(path):
    """The canonical hotel-page path for a hotel / reviews / photos page
    path (locale prefix dropped), or None when it is not one."""
    match = _HOTEL_PATH_RE.match(path or "")
    if not match:
        return None
    return f"/{match.group(1)}/hotel/{match.group(3)}.html"


def resolve_hotel(value):
    """Numeric property id, "hotel:<id>" or an agoda.com hotel link -> a
    string: the id's digits, or the hotel page path (api.py turns a path
    into the id with one page fetch)."""
    value = (value or "").strip()
    if value.isdigit():
        return value
    typed = _TYPED_RE.match(value)
    if typed and _KIND_ALIASES.get(typed.group(1).lower(), typed.group(1).lower()) == "hotel":
        return typed.group(2)
    if value.startswith(("http://", "https://", "//")):
        url = _as_url(value)
        if not _is_agoda(url):
            raise ValueError("Must be an agoda.com link.")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in ("hotelId", "hotel_id", "hotel", "selectedproperty", "hid"):
            if query.get(key) and query[key][0].isdigit():
                return query[key][0]
        path = hotel_path(parsed.path)
        if path:
            return path
        raise ValueError("Must be an agoda.com hotel page link "
                         "(https://www.agoda.com/<hotel-slug>/hotel/<city>-<cc>.html) or a property id.")
    raise ValueError("Must be an Agoda property id (9059812) or an agoda.com hotel page link.")


def resolve_location(value):
    """Destination name, typed key, bare city id or agoda.com link -> a dict
    {"kind": city|area|top_area|region|landmark|hotel|name|hotel_path,
    "id": int|None, "name": str|None, "path": str|None}; a search link that
    carries the city next to a narrower key also gives "city_id"."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Must not be empty.")
    if value.isdigit():
        return {"kind": "city", "id": int(value), "name": None, "path": None}
    typed = _TYPED_RE.match(value)
    if typed:
        kind = typed.group(1).lower()
        kind = _KIND_ALIASES.get(kind, kind)
        return {"kind": kind, "id": int(typed.group(2)), "name": None, "path": None}
    if value.startswith(("http://", "https://", "//")):
        url = _as_url(value)
        if not _is_agoda(url):
            raise ValueError("Must be an agoda.com link.")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        city_id = int(query["city"][0]) if query.get("city") and query["city"][0].isdigit() else None
        for key in URL_KEYS_BY_SPECIFICITY:
            if query.get(key) and query[key][0].isdigit():
                kind = URL_KEY_TO_KIND[key]
                out = {"kind": kind, "id": int(query[key][0]), "name": None, "path": None}
                if kind != "city" and city_id:
                    out["city_id"] = city_id
                return out
        if query.get("hotelId") and query["hotelId"][0].isdigit():
            return {"kind": "hotel", "id": int(query["hotelId"][0]), "name": None, "path": None}
        city = _CITY_PATH_RE.match(parsed.path)
        if city:
            # /city/bangkok-th.html: the slug words are the destination name
            return {"kind": "name", "id": None, "name": city.group(1).replace("-", " "), "path": None}
        path = hotel_path(parsed.path)
        if path:
            return {"kind": "hotel_path", "id": None, "name": None, "path": path}
        raise ValueError("Must be an agoda.com search link (?city=, ?area=, ?region=, ?poi=), a city page "
                         "or a hotel page link.")
    if len(value) > 100:
        raise ValueError("Must be at most 100 characters.")
    return {"kind": "name", "id": None, "name": value, "path": None}


def location_key(kind, object_id):
    """The typed key /agoda/places/autocomplete prints and /agoda/hotels/search
    accepts ("city:9395")."""
    if kind not in SEARCHABLE_KINDS or object_id is None:
        return None
    return f"{kind}:{object_id}"


def absolute_link(path):
    """'//pix8.agoda.net/x.jpg' / '/bangkok/hotel/x.html' -> https link."""
    if not path:
        return None
    if path.startswith("//"):
        return "https:" + path
    if path.startswith("/"):
        return SITE + path
    return path


def hotel_link(path):
    """The public hotel page for a reviews/hotel page path."""
    canonical = hotel_path(path or "")
    return absolute_link(canonical) if canonical else None


def search_link(kind, object_id, checkin=None, checkout=None, adults=None, children=None, rooms=None):
    """A plain agoda.com search link for a destination (the keys its own
    autocomplete uses)."""
    key = {"city": "city", "area": "area", "top_area": "toparea", "region": "region", "landmark": "poi",
           "hotel": "selectedproperty"}.get(kind)
    if not key or object_id is None:
        return None
    params = [f"{key}={object_id}"]
    if checkin:
        params.append(f"checkIn={checkin}")
    if checkout:
        params.append(f"checkOut={checkout}")
    if adults:
        params.append(f"adults={adults}")
    if children:
        params.append(f"children={len(children)}")
        params.append("childAges=" + ",".join(str(a) for a in children))
    if rooms:
        params.append(f"rooms={rooms}")
    return f"{SITE}/search?" + "&".join(params)

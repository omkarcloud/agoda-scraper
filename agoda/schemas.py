"""Marshmallow request schemas for every /agoda/* route.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url` / `id` pair):

    hotel      Agoda property id (9059812) OR any agoda.com hotel page link
    location   destination name, a typed key from /agoda/places/autocomplete
               ("city:9395", "area:27713", "landmark:141553", ...), a bare
               city id OR an agoda.com search / city / hotel link
    language   an Agoda storefront locale ("en-us", "ja-jp", ...) or a bare
               language ("fr" -> "fr-fr"); texts, labels and review
               translations follow it
    currency   ISO 4217 code the prices come back in

Stay dates default to a one-night stay 30 days out (the site needs dates
for prices, and a month ahead is rarely sold out). The filter vocabularies
are Agoda's own (agoda/refs.py).
"""
from datetime import date, timedelta

from marshmallow import ValidationError, post_load, validate

from schema_fields import (BaseSchema, ChoiceField, CommaListField, CurrencyField, DateField, Flag, LimitField,
                           PageField, PageSizeField, PositiveInt, Price, QueryField, RefField, StrippedString)
from agoda import refs

MAX_SEARCH_PAGE = 200     # 100 x 200 covers the biggest city (Bangkok lists ~6,600)
MAX_REVIEW_PAGE = 1000
MAX_NIGHTS = 30           # Agoda's own stay limit
MAX_ADULTS = 10           # per room
MAX_CHILDREN = 6          # per room
MAX_ROOMS = 9
DEFAULT_DAYS_AHEAD = 30
DEFAULT_NIGHTS = 1


class HotelRefField(RefField):
    """Agoda property id (9059812) or an agoda.com hotel / reviews page link."""
    resolver = staticmethod(refs.resolve_hotel)


class LocationRefField(RefField):
    """Destination name, an autocomplete key (city:9395), a bare city id or an agoda.com link."""
    resolver = staticmethod(refs.resolve_location)


class LanguageField(StrippedString):
    """Agoda storefront locale ("en-us") or a bare language ("fr")."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        # = refs.DEFAULT_LOCALE, spelled out so the listing tooling reads the default
        kwargs.setdefault("load_default", "en-us")
        kwargs.setdefault("metadata", {"description": "site language: a locale like en-us, en-gb, ja-jp, zh-cn, "
                                                      "th-th, fr-fr, de-de, es-es (default en-us) or a bare language code"})
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return refs.DEFAULT_LOCALE
        try:
            return refs.normalize_locale(value)
        except ValueError as e:
            raise ValidationError(str(e))


class ChildAgesField(CommaListField):
    """Comma-separated child ages (0-17), one per child. Unlike the parent
    field the ages are NOT deduped: twins are "5,5", two children."""

    def __init__(self, **kwargs):
        kwargs.setdefault("metadata", {"description": "child ages per room, comma-separated, one per child (e.g. 5,9)"})
        super().__init__(upper=False, max_items=MAX_CHILDREN, **kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        ages = []
        for raw in str(value).split(","):
            item = raw.strip()
            if not item:
                continue
            if not item.isdigit() or not 0 <= int(item) <= 17:
                raise ValidationError("Child ages must be whole numbers from 0 to 17 (e.g. 5,9).")
            ages.append(int(item))
        if not ages:
            return None
        if len(ages) > self.max_items:
            raise ValidationError(f"At most {self.max_items} children per room.")
        return ages


# Zero-arg field factories shared by the schemas (inline keywords, so the
# listing tooling reads their bounds and defaults).

def _currency():
    return CurrencyField(load_default="USD", metadata={"description": "ISO 4217 currency for the prices (default USD)"})


def _checkin():
    return DateField(bound="future", metadata={
        "description": "check-in date, YYYY-MM-DD, today or later (default: 30 days from today)"})


def _checkout():
    return DateField(bound="future", metadata={
        "description": "check-out date, YYYY-MM-DD, 1-30 nights after check-in (default: one night)"})


def _adults():
    return PositiveInt(max_value=MAX_ADULTS, load_default=2, metadata={
        "description": "adult guests per room, 1-10 (default 2)"})


def _rooms():
    return PositiveInt(max_value=MAX_ROOMS, load_default=1, metadata={
        "description": "rooms to book, 1-9 (default 1), each with the same guests"})


def _hotel():
    return HotelRefField(metadata={
        "description": "Agoda property id (9059812) or an agoda.com hotel page link "
                       "(https://www.agoda.com/<hotel-slug>/hotel/<city>-<cc>.html)"})


def fill_stay(data):
    """Default / check the stay: a one-night stay 30 days out when nothing is
    given, the other end a night away when only one date is given."""
    checkin, checkout = data.get("checkin"), data.get("checkout")
    if not checkin and not checkout:
        start = date.today() + timedelta(days=DEFAULT_DAYS_AHEAD)
        checkin, checkout = start.isoformat(), (start + timedelta(days=DEFAULT_NIGHTS)).isoformat()
    elif checkin and not checkout:
        checkout = (date.fromisoformat(checkin) + timedelta(days=DEFAULT_NIGHTS)).isoformat()
    elif checkout and not checkin:
        checkin = (date.fromisoformat(checkout) - timedelta(days=DEFAULT_NIGHTS)).isoformat()
        if date.fromisoformat(checkin) < date.today():
            raise ValidationError("checkout must be at least a day after today.", "checkout")
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    if nights < 1:
        raise ValidationError("checkout must be after checkin.", "checkout")
    if nights > MAX_NIGHTS:
        raise ValidationError(f"Stays are at most {MAX_NIGHTS} nights on Agoda.", "checkout")
    data["checkin"], data["checkout"] = checkin, checkout
    return data


# ---- places/autocomplete -------------------------------------------------------------------

class AutocompleteSchema(BaseSchema):
    query = QueryField(max_length=100, metadata={
        "description": "place or hotel name as a traveler would type it (Bangkok, Sukhumvit, Terminal 21, Marina Bay Sands)"})
    language = LanguageField()


# ---- hotels/search ------------------------------------------------------------------------

class SearchSchema(BaseSchema):
    location = LocationRefField(metadata={
        "description": "destination: a name (Bangkok, Sukhumvit, Terminal 21), a key from /agoda/places/autocomplete "
                       "(city:9395, area:27713, landmark:141553, region:4157, hotel:286830), a bare city id, or an "
                       "agoda.com search / city / hotel link"})
    checkin = _checkin()
    checkout = _checkout()
    adults = _adults()
    children = ChildAgesField()
    rooms = _rooms()
    currency = _currency()
    language = LanguageField()
    sort = ChoiceField(list(refs.SORTS), load_default="recommended", metadata={
        "description": "recommended (default), price_low_to_high, price_high_to_low, guest_rating, secret_deals"})
    stars = CommaListField(allowed=["1", "2", "3", "4", "5", "luxe"], upper=False, max_items=6, metadata={
        "description": "star classes to keep, comma-separated (e.g. 4,5); luxe = Agoda Luxe properties"})
    min_score = ChoiceField(refs.MIN_SCORES, metadata={
        "description": "minimum guest score on Agoda's 10-point scale: 6 (good), 7 (very good), 8 (excellent), 9 (exceptional)"})
    min_price = Price(metadata={"description": "lowest nightly price per room to keep, in `currency`"})
    max_price = Price(metadata={"description": "highest nightly price per room to keep, in `currency`"})
    property_type = CommaListField(allowed=list(refs.PROPERTY_TYPES), upper=False, max_items=10, metadata={
        "description": "comma-separated property types: hotels or homes (Agoda's split), or specific kinds — "
                       + ", ".join(refs.ACCOMMODATION_TYPES)})
    amenities = CommaListField(allowed=list(refs.AMENITIES), upper=False, max_items=16, metadata={
        "description": "comma-separated property amenities every result must have: " + ", ".join(refs.AMENITIES)})
    bed_type = CommaListField(allowed=list(refs.BED_TYPES), upper=False, max_items=5, metadata={
        "description": "comma-separated bed types: " + ", ".join(refs.BED_TYPES)})
    free_cancellation = Flag(metadata={"description": "true keeps only properties with a free-cancellation rate"})
    pay_later = Flag(metadata={"description": "true keeps only properties with a book-now-pay-later rate"})
    pay_at_hotel = Flag(metadata={"description": "true keeps only properties that take payment at the property"})
    breakfast = Flag(metadata={"description": "true keeps only properties with a breakfast-included rate"})
    name = StrippedString(required=False, load_default=None, validate=validate.Length(max=100), metadata={
        "description": "only properties whose name contains this text"})
    page = PageField(max_page=MAX_SEARCH_PAGE, metadata={"description": "result page"})
    limit = PageSizeField(default=refs.SEARCH_PAGE_SIZE, max_size=refs.SEARCH_MAX_SIZE, metadata={
        "description": "properties per page, 1-100 (default 45, the site's own page); sponsored extras may add a few"})

    @post_load
    def _stay(self, data, **kwargs):
        fill_stay(data)
        lo, hi = data.get("min_price"), data.get("max_price")
        if lo is not None and hi is not None and lo > hi:
            raise ValidationError("min_price must not exceed max_price.", "max_price")
        return data


# ---- hotels/details -------------------------------------------------------------------------

class HotelDetailsSchema(BaseSchema):
    hotel = _hotel()
    checkin = _checkin()
    checkout = _checkout()
    adults = _adults()
    children = ChildAgesField()
    rooms = _rooms()
    currency = _currency()
    language = LanguageField()

    @post_load
    def _stay(self, data, **kwargs):
        return fill_stay(data)


# ---- hotels/reviews -------------------------------------------------------------------------

class HotelReviewsSchema(BaseSchema):
    hotel = _hotel()
    sort = ChoiceField(list(refs.REVIEW_SORTS), load_default="recent", metadata={
        "description": "recent (default), highest_rating, lowest_rating or helpful"})
    source = ChoiceField(list(refs.REVIEW_SOURCES), load_default="agoda", metadata={
        "description": "review pool: agoda (default, Agoda's own guests), booking (the Booking.com reviews Agoda shows), "
                       "or all (both; each page then carries `limit` reviews from each source)"})
    review_language = ChoiceField(list(refs.REVIEW_LANGUAGES), metadata={
        "description": "only reviews written in this language: " + ", ".join(refs.REVIEW_LANGUAGES)})
    # the site has no free-text review search (its searchKeyword is ignored);
    # its "reviews that mention" chips are these topic tags (refs.REVIEW_TOPIC_FILTER)
    topic = StrippedString(required=False, load_default=None, validate=validate.Length(max=100), metadata={
        "description": "only Agoda reviews about this topic: a tag name or id from the hotel's summary.tags "
                       "(e.g. Pool, Room view, Service)"})
    page = PageField(max_page=MAX_REVIEW_PAGE, metadata={"description": "review page"})
    limit = LimitField(default=refs.REVIEWS_PAGE_SIZE, max_size=refs.REVIEWS_MAX_SIZE, metadata={
        "description": "reviews per page per source, 1-100 (default 20)"})
    language = LanguageField(metadata={
        "description": "site language for the labels and the translated review text (default en-us)"})

    @post_load
    def _topic_source(self, data, **kwargs):
        if data.get("topic") and data.get("source") != "agoda":
            raise ValidationError("Topics filter Agoda's own reviews only; use topic with source=agoda.", "topic")
        return data

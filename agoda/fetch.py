"""Agoda transport: plain curl_cffi with browser-impersonated TLS. No
browser, no cookies, no token — every surface below answers a bare request.
Egress: the pod's own IP (config.agoda_proxy(), knob AGODA_EXIT; pod probe
2026-11-09: direct, 2/2 datacenter and 2/2 TH residential ports all 200 on
every surface). A retryable failure drops the thread's session, so the
retry rides a fresh exit when the knob points at a proxy tier.

Upstream surfaces (www.agoda.com, all open):

  * AUTOCOMPLETE GET /api/cronos/search/GetUnifiedSuggestResult/3/<langId>/1/0/<locale>/
                 ?searchText=<text> -> ViewModelList of cities / areas /
                 regions / landmarks / hotels (the first row echoes the typed
                 text: IsDefault true). The language id in the path localizes
                 the names; the locale segment alone does not.

  * SEARCH       POST /graphql/search, operation citySearch (and its siblings
                 areaSearch / topAreaSearch / regionSearch / landmarkSearch
                 with the same searchRequest). The site builds its 30 KB query
                 document in the browser; QUERY below is a hand-trimmed
                 document with the fields the API uses (the schema only
                 validates names). Variables: dates / occupancy / currency /
                 sorting / filters / page (pageSize <= 100, pageNumber). No
                 header beyond content-type is needed; AG-LANGUAGE-LOCALE +
                 searchContext.locale pick the language. Clean GraphQL errors
                 ("CityInfo is empty for cityId", "checkInDate must be greater
                 than yesterday and before two years ahead", "Page size must
                 be between 0 and 100") are mapped below.

  * DETAILS      GET /api/cronos/property/BelowFoldParams/GetSecondaryData
                 ?hotel_id=<id>&checkIn=YYYY-MM-DD&los=<nights>&adults=&children=
                 &childAges=&rooms=&all=false -> 300 KB-1.3 MB JSON: hotelInfo,
                 aboutHotel, mosaicInitData (images), reviews (summary),
                 essentialInfo, roomGridData (rooms + rate plans). TRAPS:
                 `checkOut` is ignored (send `los`), `currencyCode` is ignored
                 (the CR-Currency-Code header sets it), the language is the
                 AG-Language-Locale header (a locale path prefix is 404), and
                 an unknown id is HTTP 200 with hotelInfo null.

  * HOTEL PAGE   GET /<slug>/hotel/<city>-<cc>.html -> the lite page shell
                 whose GetSecondaryData URL carries hotel_id=<id> (the only
                 slug -> id mapping; unknown slugs redirect to
                 /pagenotfound.html with HTTP 404).

  * REVIEWS      POST /api/cronos/property/review/ReviewComments json
                 {hotelId, providerId, demographicId, page, pageSize (<=100),
                 sorting (1 recent / 2 high-low / 3 low-high / 7 helpful),
                 providerIds [332 Agoda, 3038 Booking.com], isReviewPage,
                 isCrawlablePage, filters {language: [ids], room: []},
                 searchKeyword, searchFilters} -> comments + the filter block.
                 The site has no free-text review search: `searchKeyword` is
                 ignored (2026-11-10, same totals and rows with or without
                 it). Its "reviews that mention" chips are the topic tags,
                 sent as searchFilters [{id: 8 (REVIEWTAGV2ID), value:
                 [tagId]}] — Agoda's own reviews only. An EMPTY page (no
                 comments, no providerList) also comes back now and then
                 from the pod's exit for a hotel with thousands of reviews;
                 see EMPTY_PAGE_RETRIES.
                 No total: /HotelReviews (same body) is the summary — score,
                 grades, distribution, per-provider counts (reviewTabs), the
                 review-page URL (= the hotel slug) and the topic tags
                 (reviewTagsV2: tagId + tagName).

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  AgodaUpstreamError  transport failure / 5xx / odd payload  — retryable
  AgodaBlocked        403 / 429 / challenge page              — retryable
  AgodaBadRequest     the site rejected the dates / page size — never retried
  AgodaNotFound       unknown city / area / hotel / slug      — never retried
"""
import os
import re
import sys
import threading
import time
import uuid

# Allow direct execution (python agoda/fetch.py): flat imports resolve like
# under the server. Idempotent when imported normally.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import egress
import fastjson
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from agoda.refs import (DEFAULT_LOCALE, PROVIDER_AGODA, REVIEW_TOPIC_FILTER, SEARCH_OPS, SITE, language_id)

IMPERSONATE = "chrome"
JSON_TIMEOUT = 40      # GetSecondaryData is up to ~1.3 MB
PAGE_TIMEOUT = 30
SEARCH_TIMEOUT = 40

_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)
# the GetSecondaryData URL in the page shell, whether its `&` is raw,
# HTML-escaped (&amp;) or JSON-escaped (&)
_PAGE_HOTEL_ID_RE = re.compile(r"(?:[?&]|&amp;|\\u0026)hotel_id=(\d+)")
_PAGE_HOTEL_ID_ALT_RE = re.compile(r'"hotelId"\s*:\s*(\d+)')
_CHALLENGE_MARKERS = ("captcha", "access denied", "just a moment", "unusual traffic", "px-captcha",
                      "cf-chl", "awswaf", "datadome")


class AgodaUpstreamError(UpstreamError):
    """Transport failure, 5xx or a payload of an unexpected shape — retryable."""


class AgodaBlocked(AgodaUpstreamError, Blocked):
    """403 / 429 or a challenge page where JSON/HTML was expected — retryable."""


class AgodaBadRequest(BadRequest):
    """The site rejected the parameters (dates out of range, page size) — never retried."""


class AgodaNotFound(NotFound):
    """City / area / landmark / hotel / slug does not exist — never retried."""


# ---- sessions ------------------------------------------------------------------------
# One session per cheroot worker thread: a curl handle must not be shared
# across threads; the calls are stateless, so thread-lifetime sessions with
# keep-alive are enough. The Accept-Language is the exit's own
# (egress.accept_language), decided when the session is created.
_local = threading.local()


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        from curl_cffi import requests as curl_requests
        proxy = config.agoda_proxy()
        sess = curl_requests.Session(impersonate=IMPERSONATE)
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        sess.headers["accept-language"] = egress.accept_language(proxy)
        _local.session = sess
    return sess


def _drop_session():
    """Close the thread's session so a poisoned connection (or exit) dies."""
    sess = getattr(_local, "session", None)
    _local.session = None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _retrying(fn):
    """Run fn() under the shared retry policy (AgodaUpstreamError only;
    NotFound / BadRequest surface immediately)."""
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except AgodaUpstreamError as e:
            last = e
            _drop_session()
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def _classify(resp, label):
    """Map an HTTP response to the error taxonomy; return the body text."""
    if resp.status_code == 404:
        raise AgodaNotFound(f"{label} not found")
    if resp.status_code == 400:
        raise AgodaBadRequest(resp.text[:200])
    if resp.status_code in (401, 403, 429):
        raise AgodaBlocked(f"HTTP {resp.status_code} for {label}")
    if resp.status_code != 200:
        raise AgodaUpstreamError(f"HTTP {resp.status_code} for {label}")
    return resp.text


def _json(resp, label):
    text = _classify(resp, label)
    try:
        return fastjson.loads(text)
    except Exception:
        head = text[:400].lower()
        if any(marker in head for marker in _CHALLENGE_MARKERS):
            raise AgodaBlocked(f"challenge page for {label}")
        raise AgodaUpstreamError(f"non-JSON response for {label}: {text[:120]!r}")


def _api_headers(locale, currency=None):
    headers = {"accept": "application/json, text/plain, */*",
               "ag-language-locale": locale,
               "ag-language-id": str(language_id(locale)),
               "x-requested-with": "XMLHttpRequest",
               "referer": SITE + "/"}
    if currency:
        headers["cr-currency-code"] = currency
    return headers


# ---- autocomplete -----------------------------------------------------------------------

def fetch_autocomplete(text, locale=DEFAULT_LOCALE):
    """GET the suggestion list for a typed text -> the raw payload (dict
    with ViewModelList)."""
    path = f"/api/cronos/search/GetUnifiedSuggestResult/3/{language_id(locale)}/1/0/{locale}/"

    def once():
        try:
            resp = _session().get(SITE + path, params={"searchText": text},
                                  headers=_api_headers(locale), timeout=JSON_TIMEOUT)
        except Exception as e:
            raise AgodaUpstreamError(f"request failed: {type(e).__name__}: {e}")
        payload = _json(resp, "autocomplete")
        if not isinstance(payload, dict) or "ViewModelList" not in payload:
            raise AgodaUpstreamError("autocomplete payload without ViewModelList")
        return payload
    return _retrying(once)


# ---- search (GraphQL) -------------------------------------------------------------------

# A hand-trimmed citySearch document: the site's own is built by a query
# builder in the browser (search-*.js) and runs 30 KB; the schema validates
# names only, so this subset is accepted. `OP` and `VAR` are substituted per
# destination kind (refs.SEARCH_OPS).
QUERY = """
query OP($VAR: VAR!, $ContentSummaryRequest: ContentSummaryRequest!, $PricingSummaryRequest: PricingRequestParameters, $PriceStreamMetaLabRequest: PriceStreamMetaLabRequest, $ContentTopicsRequest: TopicsRequest) {
  OP(VAR: $VAR) {
    searchResult {
      searchInfo {
        isComplete
        totalFilteredHotels
        objectInfo { objectName cityName cityEnglishName countryId countryEnglishName mapLatitude mapLongitude cityId }
      }
      histogram { bins { numOfElements upperBound { perNightPerRoom } } }
    }
    properties(ContentSummaryRequest: $ContentSummaryRequest, PricingSummaryRequest: $PricingSummaryRequest, PriceStreamMetaLabRequest: $PriceStreamMetaLabRequest, ContentTopicsRequest: $ContentTopicsRequest) {
      propertyId
      propertyResultType
      sponsoredDetail { sponsoredType isShowSponsoredFlag }
      content {
        informationSummary {
          localeName defaultName displayName accommodationType accommodation { accommodationName }
          propertyLinks { propertyPage }
          address { countryCode country { id name } city { id name } area { id name } }
          propertyType rating isGovernmentVerifiedStars awardYear isSustainableTravel
          geoInfo { latitude longitude }
          remarks { renovationInfo { renovationType year } }
          spokenLanguages { id }
          nhaSummary { hostPropertyCount }
          hotelCharacter { hotelTag { name symbol } hotelView { name symbol } }
        }
        propertyEngagement { lastBooking peopleLooking todayBooking }
        nonHotelAccommodation { masterRooms { noOfBathrooms noOfBedrooms noOfBeds roomSizeSqm highlightedFacilities } hostLevel { id name } supportedLongStay }
        images { hotelImages { id caption providerId urls { key value } } }
        reviews {
          contentReview { isDefault providerId demographics { groups { id grades { id score } } } summaries { recommendationScores { recommendationScore } snippets { countryId countryCode countryName date demographicId demographicName reviewer reviewRating snippet } } cumulative { reviewCount score } }
          cumulative { reviewCount score }
        }
        familyFeatures { hasChildrenFreePolicy isFamilyRoom hasMoreThanOneBedroom isInterConnectingRoom isInfantCottageAvailable hasKidsPool hasKidsClub }
        localInformation { landmarks { transportation { landmarkName distanceInM } topLandmark { landmarkName distanceInM } beach { landmarkName distanceInM } } hasAirportTransfer }
        highlight { cityCenter { distanceFromCityCenter } favoriteFeatures { features { id title category } } uniqueAttributes { summary } hasNearbyPublicTransportation }
      }
      contentTopics { topics { topic reviewSnippets } }
      soldOut { soldOutPrice { averagePrice } }
      pricing {
        isAvailable isReady isEasyCancel isInsiderDeal
        payment { cancellation { cancellationType freeCancellationDate } payLater { isEligible } payAtHotel { isEligible } noCreditCard { isEligible } }
        supplierInfo { id name isAgodaBand }
        childPolicy { freeChildren }
        offers {
          roomOffers {
            room {
              availableRooms supplierId
              promotions { typeId promotionDiscount { value } description }
              pricing {
                currency
                price {
                  perNight { exclusive { display crossedOutPrice originalPrice } inclusive { display crossedOutPrice originalPrice } }
                  perBook { exclusive { display crossedOutPrice originalPrice } inclusive { display crossedOutPrice originalPrice } }
                  perRoomPerNight { exclusive { display crossedOutPrice originalPrice } inclusive { display crossedOutPrice originalPrice } }
                  totalDiscount
                }
              }
              payment { paymentModel cancellation { cancellationType } }
              discount { deals channelDiscount }
              capacity { extraBedsAvailable }
            }
          }
        }
      }
      enrichment {
        topSellingPoint { tspType value }
        bookingHistory { bookingCount { count timeFrame } lastBooking }
        isPopular isTopRatedInCity
        roomInformation { cheapestRoomSizeSqm facilities { id propertyFacilityName symbol } }
      }
    }
    aggregation { matrixGroupResults { matrixGroup matrixItemResults { id name count filterKey filterRequestType } } }
  }
}
"""

# the filter groups the aggregation is asked to count
MATRIX_GROUPS = ("StarRatingWithLuxury", "ReviewScore", "ProductType", "AccommodationType", "HotelFacilities",
                 "RoomAmenities", "RoomBenefits", "GroupedBedTypes", "PaymentOptions", "HotelAreaId", "LandmarkIds",
                 "HotelChainId", "AtmosphereIds", "TripPurpose", "CityCenterDistance", "AffordableCategory",
                 "NoOfBedrooms", "NoOfBeds", "NoOfBathrooms", "ReviewTopicIds", "AllGuestReviewBreakdown",
                 "ReviewLocationScore", "MetroSubwayStationLandmarkIds", "TrainStationLandmarkIds",
                 "BusStationLandmarkIds", "BeachAccessTypeIds", "KidsStayForFree", "PopularForFamily")

_GQL_NOT_FOUND = ("is empty for", "doesn't exist", "does not exist", "doesn't not support", "not support for id",
                  "not found")
_GQL_BAD_REQUEST = ("must be greater than", "must be between", "expected value of type", "invalid")

# The site adds the `RoomAmenityFilter` pricing feature whenever a room-level
# filter (bed types, room amenities) is on (its search bundle:
# isRoomAmenitiesRequest -> featureFlag RoomAmenityFilter). Without it the
# search answers the filtered COUNT but an EMPTY property list (2026-11-10:
# Bangkok + king beds = 2,483 hotels, 0 rows; with the flag 12 rows a page).
ROOM_LEVEL_FILTERS = ("GroupedBedTypes", "RoomAmenities")


def room_level_filtered(ids_filters):
    """True when an ids filter needs the RoomAmenityFilter pricing feature."""
    return any(isinstance(f, dict) and f.get("filterKey") in ROOM_LEVEL_FILTERS and f.get("ids")
               for f in ids_filters or [])


def search_variables(kind, object_id, checkin, checkout, adults=2, child_ages=None, rooms=1, currency="USD",
                     locale=DEFAULT_LOCALE, sort=("Ranking", "Desc"), ids_filters=None, range_filters=None,
                     text_filters=None, page=1, page_size=45, extra_hotel_ids=None, city_id=None):
    """The GraphQL variables for one search page. `kind` picks the
    operation (refs.SEARCH_OPS); areaSearch also needs the area's cityId."""
    from datetime import date
    op, var, id_field = SEARCH_OPS[kind]
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    user_id, search_id = str(uuid.uuid4()), str(uuid.uuid4())
    child_ages = list(child_ages or [])
    origin = (egress.exit_country(config.agoda_proxy()) or egress.DIRECT_COUNTRY).upper()
    request = {id_field: object_id}
    if kind == "area":
        request["cityId"] = city_id or 0
    request["searchRequest"] = {
        "searchCriteria": {
            "isAllowBookOnRequest": True, "bookingDate": now,
            "checkInDate": f"{checkin}T00:00:00.000Z", "localCheckInDate": checkin,
            "los": nights, "rooms": rooms, "adults": adults, "children": len(child_ages), "childAges": child_ages,
            "ratePlans": [],
            "featureFlagRequest": {"fetchNamesForTealium": True, "fiveStarDealOfTheDay": True, "isAllowBookOnRequest": False,
                                   "showUnAvailable": True, "showRemainingProperties": True, "isMultiHotelSearch": False,
                                   "enableAgencySupplyForPackages": True,
                                   "flags": [{"feature": "FamilyChildFriendlyPopularFilter", "enable": True},
                                             {"feature": "FamilyChildFriendlyPropertyTypeFilter", "enable": True},
                                             {"feature": "FamilyMode", "enable": False}],
                                   "enableDealsOfTheDayFilter": False, "isEnableSupplierFinancialInfo": False,
                                   "citySearchIgnoreRoomsCountForNha": False, "isFlexibleMultiRoomSearch": True,
                                   "enableLuxuryHotelTSP": True},
            "isUserLoggedIn": False, "currency": currency, "travellerType": "Couple", "isAPSPeek": False,
            "enableOpaqueChannel": False, "isEnabledPartnerChannelSelection": None,
            "sorting": {"sortField": sort[0], "sortOrder": sort[1]},
            "requiredBasis": "PRPN", "requiredPrice": "Exclusive", "suggestionLimit": 0, "synchronous": False,
            "supplierPullMetadataRequest": None, "isRoomSuggestionRequested": False, "isAPORequest": False,
            "hasAPOFilter": False,
        },
        "searchContext": {"userId": user_id, "memberId": 0, "locale": locale, "cid": -1, "origin": origin, "platform": 1,
                          "deviceTypeId": 1, "experiments": {"forceByVariant": None, "forceByExperiment": []},
                          "isRetry": False, "showCMS": False, "storeFrontId": 3, "pageTypeId": 103, "whiteLabelKey": None,
                          "ipAddress": "127.0.0.1", "endpointSearchType": op[0].upper() + op[1:], "trackSteps": None,
                          "searchId": search_id},
        "matrix": None,
        "matrixGroup": [{"matrixGroup": g, "size": 100} for g in MATRIX_GROUPS],
        "filterRequest": {"idsFilters": ids_filters or [], "rangeFilters": range_filters or [],
                          "textFilters": text_filters or []},
        "page": {"pageSize": page_size, "pageNumber": page, "pageToken": None},
        "apoRequest": {"apoPageSize": 10}, "searchHistory": None,
        "searchDetailRequest": {"priceHistogramBins": 50}, "isTrimmedResponseRequested": False,
        "featuredAgodaHomesRequest": None, "featuredLuxuryHotelsRequest": None,
        "highlyRatedAgodaHomesRequest": None, "extraAgodaHomesRequest": None,
        "extraHotels": {"extraHotelIds": list(extra_hotel_ids or []), "enableFiltersForExtraHotels": False},
        "rankingRequest": {"isNhaKeywordSearch": False}, "rocketmilesRequestV2": None,
        "featuredPulsePropertiesRequest": None,
    }
    return {
        var: request,
        "ContentSummaryRequest": {
            "context": {"rawUserId": user_id, "memberId": 0, "userOrigin": origin, "locale": locale,
                        "forceExperimentsByIdNew": [], "apo": False, "searchCriteria": {"cityId": city_id or (object_id if kind == "city" else 0)},
                        "platform": {"id": 1}, "storeFrontId": 3, "cid": "-1",
                        "occupancy": {"numberOfAdults": adults, "numberOfChildren": len(child_ages), "travelerType": 0,
                                      "checkIn": f"{checkin}T00:00:00.000Z"},
                        "deviceTypeId": 1, "whiteLabelKey": "", "correlationId": ""},
            "summary": {"highlightedFeaturesOrderPriority": None, "includeHotelCharacter": True},
            "reviews": {"commentary": None, "demographics": {"providerIds": None, "filter": {"defaultProviderOnly": True}},
                        "summaries": {"providerIds": None, "apo": True, "limit": 1, "travellerType": 0},
                        "cumulative": {"providerIds": None}, "filters": None},
            "images": {"page": None, "maxWidth": 0, "maxHeight": 0, "imageSizes": None, "indexOffset": None},
            "rooms": {"images": None, "featureLimit": 0, "filterCriteria": None, "includeMissing": False,
                      "includeSoldOut": False, "includeDmcRoomId": False, "soldOutRoomCriteria": None,
                      "showRoomSize": True, "showRoomFacilities": True, "showRoomName": False},
            "nonHotelAccommodation": True, "engagement": True,
            "highlights": {"maxNumberOfItems": 0, "images": {"imageSizes": [{"key": "full", "size": {"width": 0, "height": 0}}]}},
            "personalizedInformation": True, "localInformation": {"images": None}, "features": None,
            "rateCategories": True, "contentRateCategories": {"escapeRateCategories": {}}, "synopsis": True,
        },
        "PricingSummaryRequest": {
            "cheapestOnly": True,
            "context": {"isAllowBookOnRequest": True, "abTests": [],
                        "clientInfo": {"cid": -1, "languageId": language_id(locale), "languageUse": 1, "origin": origin,
                                       "platform": 1, "searchId": search_id, "storefront": 3, "userId": user_id,
                                       "ipAddress": "127.0.0.1", "attributionInfos": []},
                        "experiment": [], "sessionInfo": {"isLogin": False, "memberId": 0, "sessionId": 1}},
            "isSSR": True,
            "pricing": {"bookingDate": now, "checkIn": f"{checkin}T00:00:00.000Z", "checkout": f"{checkout}T00:00:00.000Z",
                        "localCheckInDate": checkin, "localCheckoutDate": checkout, "currency": currency,
                        "details": {"cheapestPriceOnly": False, "itemBreakdown": False, "priceBreakdown": False},
                        "featureFlag": ["ClientDiscount", "PriceHistory", "VipPlatinum", "RatePlanPromosCumulative",
                                        "PromosCumulative", "MixAndSave", "APSPeek", "StackChannelDiscount", "AutoApplyPromos",
                                        "EnableAgencySupplyForPackages", "EnableCashback", "CreditCardPromotionPeek",
                                        "EnableCofundedCashback", "DispatchGoLocalForInternational", "EnableGoToTravelCampaign",
                                        "EnableCashbackMildlyAggressiveDisplay", "EnablePriceTrend", "MultipleExtraBeds"]
                                       + (["RoomAmenityFilter"] if room_level_filtered(ids_filters) else []),
                        "features": {"calculateRareRoomBadge": False, "crossOutRate": False, "isAPSPeek": False, "isAllOcc": False,
                                     "isApsEnabled": False, "isIncludeUsdAndLocalCurrency": False, "isMSE": True,
                                     "isRPM2Included": True, "maxSuggestions": 0, "isEnableSupplierFinancialInfo": False,
                                     "isLoggingAuctionData": False, "newRateModel": False, "overrideOccupancy": False,
                                     "filterCheapestRoomEscapesPackage": False, "priusId": 0, "synchronous": False,
                                     "enableRichContentOffer": True, "showCouponAmountInUserCurrency": False,
                                     "disableEscapesPackage": False, "enablePushDayUseRates": False, "enableDayUseCor": False,
                                     "ignoreRoomsCountForNha": False, "enableSuggestPriceExclusiveWithFees": True},
                        "filters": {"cheapestRoomFilters": [], "filterAPO": False, "ratePlans": [1], "secretDealOnly": False,
                                    "suppliers": [], "nosOfBedrooms": []},
                        "includedPriceInfo": False,
                        "occupancy": {"adults": adults, "children": len(child_ages), "childAges": child_ages, "rooms": rooms,
                                      "childrenTypes": []},
                        "supplierPullMetadata": {"requiredPrecheckAccuracyLevel": 0}, "mseHotelIds": [], "mseClicked": "",
                        "ppLandingHotelIds": [], "searchedHotelIds": [], "paymentId": -1, "requiredBasis": "PRPN"},
            "suggestedPrice": "Exclusive",
        },
        "PriceStreamMetaLabRequest": {"attributesId": [8, 1, 18, 7, 11, 2, 3]},
        "ContentTopicsRequest": {"topicNames": ["Location"]},
    }


def _raise_graphql(errors, label, default=AgodaUpstreamError):
    """Map a GraphQL error list to the taxonomy. `default` is the class for
    a message neither list knows: AgodaBadRequest when the gateway already
    said HTTP 400 (deterministic, retrying only burns the backoff), the
    retryable AgodaUpstreamError for errors inside a 200."""
    message = "; ".join(str((e or {}).get("message") or e) for e in errors)[:300]
    low = message.lower()
    if any(marker in low for marker in _GQL_NOT_FOUND):
        raise AgodaNotFound(f"{label}: {message}")
    if any(marker in low for marker in _GQL_BAD_REQUEST):
        raise AgodaBadRequest(message)
    if default is AgodaBadRequest:
        raise AgodaBadRequest(message)
    raise AgodaUpstreamError(f"{label}: {message}")


def fetch_search(kind, object_id, locale=DEFAULT_LOCALE, **criteria):
    """POST one search page -> the operation's result dict (searchResult /
    properties / aggregation). `criteria` are search_variables' keywords."""
    op, var, _ = SEARCH_OPS[kind]
    query = QUERY.replace("OP", op).replace("VAR", var)
    body = {"operationName": op, "variables": search_variables(kind, object_id, locale=locale, **criteria),
            "query": query}
    label = f"{kind} {object_id} search"
    headers = {**_api_headers(locale, criteria.get("currency")), "content-type": "application/json",
               "ag-page-type-id": "103", "ag-request-id": str(uuid.uuid4()), "origin": SITE}

    def once():
        try:
            resp = _session().post(SITE + "/graphql/search", data=fastjson.dumps(body), headers=headers,
                                   timeout=SEARCH_TIMEOUT)
        except Exception as e:
            raise AgodaUpstreamError(f"request failed: {type(e).__name__}: {e}")
        if resp.status_code == 400:
            # the gateway answers a rejected search with HTTP 400 and a
            # GraphQL error list (unknown city, dates out of range)
            try:
                errors = (fastjson.loads(resp.text) or {}).get("errors")
            except Exception:
                errors = None
            if errors:
                _raise_graphql(errors, label, default=AgodaBadRequest)
        payload = _json(resp, label)
        if not isinstance(payload, dict):
            raise AgodaUpstreamError(f"unexpected search payload: {str(payload)[:120]}")
        data = (payload.get("data") or {}).get(op)
        if payload.get("errors") and not data:
            _raise_graphql(payload["errors"], label)
        if not isinstance(data, dict):
            raise AgodaUpstreamError(f"search payload without {op}")
        return data
    return _retrying(once)


# ---- details -----------------------------------------------------------------------------

def fetch_details(hotel_id, checkin=None, nights=None, adults=2, child_ages=None, rooms=1, currency="USD",
                  locale=DEFAULT_LOCALE):
    """GET GetSecondaryData -> the raw JSON. Without `checkin` the record is
    dateless (no room grid). An unknown id raises AgodaNotFound."""
    child_ages = list(child_ages or [])
    params = {"hotel_id": hotel_id, "all": "false", "isHostPropertiesEnabled": "true"}
    if checkin:
        params.update({"checkIn": checkin, "los": nights or 1, "adults": adults, "rooms": rooms,
                       "children": len(child_ages), "childAges": ",".join(str(a) for a in child_ages)})
    label = f"hotel {hotel_id}"

    def once():
        try:
            resp = _session().get(SITE + "/api/cronos/property/BelowFoldParams/GetSecondaryData", params=params,
                                  headers=_api_headers(locale, currency), timeout=JSON_TIMEOUT)
        except Exception as e:
            raise AgodaUpstreamError(f"request failed: {type(e).__name__}: {e}")
        payload = _json(resp, label)
        if not isinstance(payload, dict) or "hotelInfo" not in payload:
            raise AgodaUpstreamError(f"{label}: payload without hotelInfo")
        info = payload.get("hotelInfo")
        if not info or not info.get("name"):
            raise AgodaNotFound(f"{label} not found")
        return payload
    return _retrying(once)


def fetch_hotel_id(path):
    """GET a hotel page by its slug path -> the numeric property id (the
    page shell's GetSecondaryData URL carries hotel_id=). Unknown slugs
    redirect to /pagenotfound.html (HTTP 404)."""
    label = f"hotel page {path}"

    def once():
        try:
            resp = _session().get(SITE + path, headers={"accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                                  timeout=PAGE_TIMEOUT, allow_redirects=True)
        except Exception as e:
            raise AgodaUpstreamError(f"request failed: {type(e).__name__}: {e}")
        text = _classify(resp, label)
        if "pagenotfound" in str(resp.url).lower():
            raise AgodaNotFound(f"{label} not found")
        match = _PAGE_HOTEL_ID_RE.search(text) or _PAGE_HOTEL_ID_ALT_RE.search(text)
        if not match:
            title = (_TITLE_RE.search(text) or [None, ""])[1].strip().lower() if _TITLE_RE.search(text) else ""
            if any(marker in text[:3000].lower() for marker in _CHALLENGE_MARKERS):
                raise AgodaBlocked(f"challenge page for {label}")
            if "404" in title or "not found" in title:
                raise AgodaNotFound(f"{label} not found")
            raise AgodaUpstreamError(f"{label}: page without a hotel id")
        return int(match.group(1))
    return _retrying(once)


# ---- reviews -----------------------------------------------------------------------------

def _review_body(hotel_id, page=1, size=20, sort=1, provider_ids=None, language_ids=None, topic_ids=None,
                 demographic_id=0):
    """The ReviewComments / HotelReviews body, as the site's own review
    widget builds it. `topic_ids` are review tag ids (the summary's
    reviewTagsV2 tagId), sent the way the site's topic chips send them."""
    provider_ids = list(provider_ids or [PROVIDER_AGODA])
    search_filters = [{"id": REVIEW_TOPIC_FILTER, "value": list(topic_ids)}] if topic_ids else []
    return {"hotelId": hotel_id, "providerId": provider_ids[0] if len(provider_ids) == 1 else PROVIDER_AGODA,
            "demographicId": demographic_id, "page": page, "pageSize": size, "sorting": sort,
            "providerIds": provider_ids, "isReviewPage": False, "isCrawlablePage": True,
            "filters": {"language": list(language_ids or []), "room": []},
            "searchKeyword": "", "searchFilters": search_filters}


def _post_reviews(endpoint, body, locale, currency, label):
    headers = {**_api_headers(locale, currency), "content-type": "application/json", "origin": SITE}

    def once():
        try:
            resp = _session().post(SITE + endpoint, data=fastjson.dumps(body), headers=headers, timeout=JSON_TIMEOUT)
        except Exception as e:
            raise AgodaUpstreamError(f"request failed: {type(e).__name__}: {e}")
        payload = _json(resp, label)
        if not isinstance(payload, dict):
            raise AgodaUpstreamError(f"{label}: unexpected payload {str(payload)[:120]}")
        return payload
    return _retrying(once)


# From the pod's direct exit about one ReviewComments call in six answered
# an EMPTY page (no comments, no providerList) for a hotel with thousands
# of reviews (2026-11-10: 3 of 18 fresh calls from the pod, 0 of 24 from a
# home connection). A genuinely empty page (past the end, a filter that
# matches nothing) has the same shape, so an empty page is asked again on
# a fresh session this many times before it is believed — and api.py never
# caches one.
EMPTY_PAGE_RETRIES = 2


def fetch_reviews(hotel_id, page=1, size=20, sort=1, provider_ids=None, language_ids=None, topic_ids=None,
                  locale=DEFAULT_LOCALE, currency=None):
    """POST ReviewComments -> the raw page (comments + reviewFilterAndSort).
    An unknown hotel answers an empty page, so api.py checks the summary first."""
    body = _review_body(hotel_id, page, size, sort, provider_ids, language_ids, topic_ids)
    for attempt in range(EMPTY_PAGE_RETRIES + 1):
        payload = _post_reviews("/api/cronos/property/review/ReviewComments", body, locale, currency,
                                f"hotel {hotel_id} reviews")
        if "comments" not in payload:
            raise AgodaUpstreamError(f"hotel {hotel_id} reviews: payload without comments")
        if payload.get("comments") or payload.get("providerList"):
            break
        if attempt < EMPTY_PAGE_RETRIES:
            _drop_session()
    return payload


def fetch_review_summary(hotel_id, provider_ids=None, locale=DEFAULT_LOCALE):
    """POST HotelReviews -> the summary (score, grades, distribution, per-
    provider counts, review-page URL, topic tags). hasReviewsData false +
    no hotelName = unknown hotel -> AgodaNotFound."""
    payload = _post_reviews("/api/cronos/property/review/HotelReviews",
                            _review_body(hotel_id, 1, 5, 7, provider_ids or [PROVIDER_AGODA]),
                            locale, None, f"hotel {hotel_id} review summary")
    if "hasReviewsData" not in payload:
        raise AgodaUpstreamError(f"hotel {hotel_id} review summary: unexpected payload")
    if not payload.get("hasReviewsData") and not payload.get("hotelName"):
        raise AgodaNotFound(f"hotel {hotel_id} not found")
    return payload

"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. Params go through the same schemas the API uses, so defaults
are filled in (a one-night stay 30 days out, 2 adults, USD) and a hotel id
or an agoda.com hotel link both work.
"""
import json
import os

from agoda import api, schemas

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


def run(schema_cls, fn, **params):
    return fn(**schema_cls().load(params))


if __name__ == "__main__":
    # a hotel id or any agoda.com hotel link — every room with live rates, scores, facilities, policies, photos
    save("hotel_marina_bay_sands.json", run(schemas.HotelDetailsSchema, api.hotel_details, hotel="185945"))

    # a city, area or landmark name (or an autocomplete key like city:5085) — 45 properties per page
    save("search_tokyo.json", run(schemas.SearchSchema, api.hotels_search, location="Tokyo"))

    # guest reviews, 20 per page — source=booking for the Booking.com ones Agoda shows
    save("reviews_marina_bay_sands.json", run(schemas.HotelReviewsSchema, api.hotel_reviews, hotel="185945"))

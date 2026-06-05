"""
Electronics category configuration for the Njuskalo scraper.

Njuskalo has no single "electronics" root; instead it exposes several flat
top-level *families*, each with its own brand/type subcategories discovered
at crawl time. These are the confirmed-live family roots we crawl.

A family page lists subcategories via ``div.entity-list-categories`` (the
same structure the real-estate scraper already parses). Leaf subcategories
have no further sub-list and contain the actual ad listings.
"""

BASE_URL = "https://www.njuskalo.hr"

# slug -> human label. Each is a top-level electronics family root.
FAMILIES = {
    "mobiteli":              "Mobiteli (phones)",
    "informatika":           "Informatika (computers / IT)",
    "foto":                  "Foto (cameras & photo gear)",
    "audio-oprema":          "Audio oprema (audio equipment)",
    "bijela-tehnika":        "Bijela tehnika (large appliances)",
    "mali-kucanski-aparati": "Mali kucanski aparati (small appliances)",
    "slusalice":             "Slusalice (headphones)",
    "dronovi":               "Dronovi (drones)",
    "playstation":           "PlayStation (consoles & games)",
    "xbox":                  "Xbox (consoles & games)",
}


def family_url(slug: str) -> str:
    return f"{BASE_URL}/{slug}"


def all_family_slugs():
    return list(FAMILIES.keys())

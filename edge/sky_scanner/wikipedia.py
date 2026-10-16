"""Representative Wikipedia photos for aircraft types.

Each ICAO type designator is resolved once to an English Wikipedia article:
first through Wikipedia's "List of aircraft type designators", otherwise by
searching the readable model name and accepting the hit only when the
article's short description reads like an aircraft and its title names the
same model (a search for "OV-10D" once returned the B-25 Mitchell). The article summary's
lead image is kept only when it is hosted on Wikimedia Commons (freely
licensed); the page hotlinks it from Wikimedia's image servers.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API = "https://en.wikipedia.org/w/api.php"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
DESIGNATOR_PAGE = "List_of_aircraft_type_designators"
USER_AGENT = ("SkyScanner/1.0 (personal receive-only ADS-B station; "
              "one cached lookup per aircraft type; https://github.com/rdpharr/sky-scanner)")
AIRCRAFT_WORDS = re.compile(
    r"aircraft|airliner|airplane|aeroplane|jet|helicopter|rotorcraft|turboprop|glider|"
    r"trainer|bomber|fighter|tanker|transport|seaplane|flying boat|tiltrotor|airlifter", re.I)
GENERIC_WORDS = {"AIR", "AIRCRAFT", "AIRPLANE", "AEROSPACE", "AMERICA", "AMERICAN", "AVIATION", "CANADA", "COMPANY",
                 "CORP", "CORPORATION", "DESIGN", "HELICOPTER", "HELICOPTERS", "INC", "INDUSTRIES", "INTERNATIONAL",
                 "LIMITED", "LLC", "LTD", "MANUFACTURING", "NORTH", "TEXTRON", "THE"}
COMMONS_IMAGE = re.compile(r"^https://(upload|thumb)\.wikimedia\.org/wikipedia/commons/")


def _strip_query(url: str | None) -> str | None:
    """Drop tracking parameters (utm_*) that the API appends to image URLs."""
    return url.split("?", 1)[0] if url else url


def _get_json(url: str, timeout: int = 30) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(5 * 1024 * 1024))


def parse_designator_list(page_html: str) -> dict[str, str]:
    """ICAO designator -> article title from the list's table rows."""
    result: dict[str, str] = {}
    for row in re.findall(r"<tr>(.*?)</tr>", page_html, re.S):
        cells = re.findall(r"<td>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue
        designator = re.sub(r"<[^>]+>", "", cells[0]).strip().upper()
        link = re.search(r'<a href="/wiki/[^"]+" title="([^"]+)"', cells[2])
        if re.fullmatch(r"[A-Z0-9]{2,4}", designator) and link:
            result.setdefault(designator, link.group(1).replace("&amp;", "&"))
    return result


def fetch_designator_list(get_json: Callable[[str], Any] = _get_json) -> dict[str, str]:
    url = API + "?" + urllib.parse.urlencode({
        "action": "parse", "page": DESIGNATOR_PAGE, "prop": "text",
        "format": "json", "formatversion": 2})
    return parse_designator_list(get_json(url)["parse"]["text"])


def names_same_model(query: str, title: str) -> bool:
    """Does the article title plausibly name the model searched for?

    When both name model numbers, they must share one ("OV-10D" is not the
    "B-25 Mitchell"; "182T" is the "Cessna 182 Skylane"). Otherwise the
    title must share a distinctive word with the query, not counting the
    first word (usually the manufacturer): "King Air 350" matches "Super
    King Air". A family article without the variant's number is accepted
    only through such a word.
    """
    query, title = query.upper(), title.upper()
    query_numbers, title_numbers = set(re.findall(r"\d+", query)), set(re.findall(r"\d+", title))
    if query_numbers and title_numbers:
        return bool(query_numbers & title_numbers)
    words = re.findall(r"[A-Z]{3,}", query)
    words = [w for w in (words[1:] if len(words) > 1 else words) if w not in GENERIC_WORDS]
    title_words = re.findall(r"[A-Z]{3,}", title)
    return any(t == w or (len(w) >= 5 and t.startswith(w)) for w in words for t in title_words)


def search_title(query: str, get_json: Callable[[str], Any] = _get_json) -> str | None:
    url = API + "?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": f"{query} aircraft",
        "srlimit": 1, "srnamespace": 0, "format": "json"})
    hits = get_json(url).get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def summary(title: str, get_json: Callable[[str], Any] = _get_json) -> dict[str, Any] | None:
    url = SUMMARY.format(title=urllib.parse.quote(title.replace(" ", "_"), safe=""))
    try:
        data = get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if data.get("type") == "disambiguation":
        return None
    image = _strip_query((data.get("thumbnail") or {}).get("source"))
    original = _strip_query((data.get("originalimage") or {}).get("source"))
    free = bool(image and COMMONS_IMAGE.match(image))
    file_name = urllib.parse.unquote(original.rsplit("/", 1)[-1]) if free and original else None
    return {
        "title": data.get("title") or title,
        "description": data.get("description") or "",
        "article_url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
        or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
        "image_url": image if free else None,
        "file_url": f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(file_name)}"
        if file_name else None,
    }


def resolve(designator: str, model_name: str | None, designators: dict[str, str],
            get_json: Callable[[str], Any] = _get_json) -> tuple[str, dict[str, Any] | None]:
    """Return (method, result) for one type; result None when nothing suitable."""
    title = designators.get(designator.upper())
    if title:
        found = summary(title, get_json)
        if found:
            return "designator_list", found
    query = (model_name or "").split("/")[0].strip()
    if not query:
        return "search", None
    title = search_title(query, get_json)
    if not title:
        return "search", None
    found = summary(title, get_json)
    if not found or not AIRCRAFT_WORDS.search(found["description"]) or not names_same_model(query, title):
        return "search", None
    return "search", found

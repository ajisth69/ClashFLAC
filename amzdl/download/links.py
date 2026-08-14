"""Resolve a download argument — a bare ASIN, an Amazon Music link, or a text
file of either — to the list of content ids the pipeline runs over. Link parsing
mirrors the submodule's `custom_url_parse` (the `trackAsin` query param wins,
else the path segment after the type keyword)."""

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from amazonmusic.models import AmazonRegion

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

_TYPE_KEYWORDS = ("tracks", "albums", "playlists", "user-playlists", "artists")

_KEYWORD_KIND = {
    "tracks": "track",
    "albums": "album",
    "artists": "artist",
    "playlists": "playlist",
    "user-playlists": "user-playlist",
}


def _id_from_url(link: str) -> str:
    url = urlparse(link)
    track = parse_qs(url.query).get("trackAsin")
    if track and track[0]:
        return track[0]
    components = [c for c in url.path.split("/") if c]
    for i, comp in enumerate(components):
        if comp in _TYPE_KEYWORDS and i + 1 < len(components):
            return components[i + 1]
    for comp in components:
        if _ASIN_RE.match(comp):
            return comp
    raise ValueError(f"could not find a content id in link: {link}")


def _looks_like_url(item: str) -> bool:
    return "://" in item or "amazon." in item.lower()


_TLD_PRIMARY = {"com": "US", "de": "DE", "com.au": "AU"}


@functools.lru_cache
def _tld_to_country() -> dict:
    by_tld: dict = {}
    for country, region in AmazonRegion.get_known_regions().items():
        by_tld.setdefault(region.domain_tld, []).append(country)
    return {
        tld: (
            _TLD_PRIMARY.get(tld, countries[0])
            if len(countries) > 1
            else countries[0]
        )
        for tld, countries in by_tld.items()
    }


def _country_from_host(host: str) -> str | None:
    host = (host or "").lower()
    marker = "amazon."
    idx = host.rfind(marker)
    if idx == -1:
        return None
    return _tld_to_country().get(host[idx + len(marker):])


def _type_from_url(url) -> str | None:
    if parse_qs(url.query).get("trackAsin"):
        return "track"
    components = [c for c in url.path.split("/") if c]
    for i, comp in enumerate(components):
        if comp in _TYPE_KEYWORDS and i + 1 < len(components):
            if comp == "playlists" and i > 0 and components[i - 1] == "my":
                return "user-playlist"
            return _KEYWORD_KIND.get(comp)
    return None


@dataclass(frozen=True)
class LinkHint:
    type: str | None = None
    country: str | None = None


def hint(raw: str) -> LinkHint:
    item = raw.strip()
    if not _looks_like_url(item):
        return LinkHint()
    try:
        url = urlparse(item)
    except ValueError:
        return LinkHint()
    return LinkHint(
        type=_type_from_url(url), country=_country_from_host(url.hostname or "")
    )


def type_hint(raw: str) -> str | None:
    return hint(raw).type


def parse_one(item: str) -> str:
    item = item.strip()
    if _looks_like_url(item):
        return _id_from_url(item)
    return item


def input_file_label(raw: str) -> str | None:
    try:
        if Path(raw).is_file():
            return Path(raw).name
    except OSError:
        return None
    return None


def resolve_inputs(raw: str) -> list[str]:
    try:
        is_file = Path(raw).is_file()
    except OSError:
        is_file = False
    if is_file:
        lines = Path(raw).read_text(encoding="utf-8").splitlines()
        items = [ln.strip() for ln in lines]
        items = [ln for ln in items if ln and not ln.startswith("#")]
        return [parse_one(ln) for ln in items]
    return [parse_one(raw)]

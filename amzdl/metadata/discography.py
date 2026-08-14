"""Artist discography discovery via submodule endpoints (no catalog search).
Harvests an artist's album ASINs from its `get_page("artist/<asin>")` catalog
page and the `chronological-albums` shoveler into the `ArtistMetadata` the
pipeline expands."""


import logging

from amazonmusic.azapi import AmazonMusicMobileAPI
from amzdl.metadata.metadata import ArtistMetadata

_log = logging.getLogger("downloader.discography")

_ALBUM_TYPE_SUFFIX = "#Album"
_ARTIST_PAGE_MAX_PAGES = 40


def _find_next_token(obj) -> str | None:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, value in cur.items():
                lk = str(key).lower()
                if "next" in lk and "token" in lk and value:
                    return str(value)
                if isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return None


def _collect_album_asins(obj, seen: set, ordered: list[str]) -> None:
    if isinstance(obj, dict):
        if str(obj.get("__type", "")).endswith(_ALBUM_TYPE_SUFFIX) and obj.get("asin"):
            asin = str(obj["asin"])
            if asin not in seen:
                seen.add(asin)
                ordered.append(asin)
        for value in obj.values():
            if isinstance(value, (dict, list, tuple)):
                _collect_album_asins(value, seen, ordered)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _collect_album_asins(item, seen, ordered)


def _artist_album_asins(session: AmazonMusicMobileAPI, artist_asin: str) -> list[str]:
    seen: set = set()
    ordered: list[str] = []

    try:
        page = session.get_page(f"artist/{artist_asin}")
        _collect_album_asins(page, seen, ordered)
    except Exception:
        _log.warning(
            "artist landing page fetch failed for %s; relying on chronological pages",
            artist_asin, exc_info=True,
        )

    next_token = None
    for _ in range(_ARTIST_PAGE_MAX_PAGES):
        try:
            page = session.get_page(
                f"artist/{artist_asin}/chronological-albums", next_token=next_token
            )
        except Exception:
            _log.warning(
                "artist album page fetch failed for %s; discography may be incomplete",
                artist_asin, exc_info=True,
            )
            break
        _collect_album_asins(page, seen, ordered)
        next_token = _find_next_token(page)
        if not next_token:
            break
    return ordered


def build_artist(
    session: AmazonMusicMobileAPI, artist_data: dict, content_asin: str
) -> ArtistMetadata:
    artist_asin = str(artist_data.get("asin") or content_asin)
    return ArtistMetadata(
        name=artist_data.get("name") or "Unknown Artist",
        asin=artist_asin,
        album_asins=_artist_album_asins(session, artist_asin),
    )

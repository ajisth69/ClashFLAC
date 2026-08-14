"""Catalog search via the `textsearch` API. Resolves a free-text query to a
short list of `SearchResult`s (track, album, artist, or playlist) whose chosen
ASIN feeds straight into the normal download pipeline."""

from dataclasses import dataclass

from amzdl.api.amzn_api import AmazonMusicMobileAPI

SEARCH_TYPES = {
    "track": "catalog_track",
    "album": "catalog_album",
    "artist": "catalog_artist",
    "playlist": "catalog_playlist",
}


@dataclass
class SearchResult:
    asin: str
    fields: tuple[str, ...]


def normalize_type(value: str) -> str:
    norm = (value or "").strip().lower()
    if norm.endswith("s") and norm[:-1] in SEARCH_TYPES:
        norm = norm[:-1]
    if norm not in SEARCH_TYPES:
        raise ValueError(
            f"unknown search type '{value}' (choose one of: {', '.join(SEARCH_TYPES)})"
        )
    return norm


def _fields(search_type: str, doc: dict) -> tuple[str, ...]:
    title = doc.get("title") or "?"
    artist = doc.get("artistName") or "?"
    if search_type == "track":
        return (title, doc.get("albumName") or "?", artist)
    if search_type == "album":
        return (title, artist)
    if search_type == "artist":
        return (doc.get("name") or doc.get("title") or "?",)
    count = doc.get("trackCount")
    return (title, f"{count} tracks") if count else (title,)


def search_catalog(
    session: AmazonMusicMobileAPI, query: str, search_type: str, limit: int
) -> list[SearchResult]:
    label = SEARCH_TYPES[normalize_type(search_type)]
    region = session.credentials.account_region
    docs = session.search(
        query=query, search_types=(label,), limit=limit, region_to_use=region
    )

    results: list[SearchResult] = []
    for doc in docs or ():
        if not isinstance(doc, dict):
            continue
        asin = doc.get("asin")
        if not asin:
            continue
        results.append(SearchResult(asin=str(asin), fields=_fields(search_type, doc)))
        if len(results) >= limit:
            break
    return results

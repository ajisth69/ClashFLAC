"""Track, album, artist & playlist metadata via the submodule API. Resolves an
ASIN to a `TrackMetadata` / `AlbumMetadata` / `ArtistMetadata` /
`PlaylistMetadata` dataclass using the signed `muse`, `textsearch`, and
artist/playlist endpoints (never catalog search)."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from amazonmusic.azapi import AmazonMusicMobileAPI

_BATCH_SIZE = 20


@dataclass
class TrackMetadata:
    asin: str
    title: str
    artist: str
    album_name: str
    album_artist: str
    album_asin: str
    disc: int
    track_number: int
    total_tracks: int
    total_discs: int
    duration_seconds: int
    is_explicit: bool
    isrc: str | None = None
    composers: str | None = None
    release_date: str | None = None
    copyright: str | None = None
    label: str | None = None
    genre: str | None = None
    merchant: str | None = None
    review_average: float | None = None
    review_count: int | None = None
    cover_url: str | None = None


@dataclass
class AlbumMetadata:
    album_name: str
    artist_name: str
    album_asin: str
    cover_url: str | None
    release_date: str | None
    copyright: str | None
    label: str | None
    genre: str | None
    track_count: int
    total_discs: int
    tracks: list[TrackMetadata] = field(default_factory=list)


@dataclass
class PlaylistMetadata:
    name: str
    asin: str
    track_asins: list[str] = field(default_factory=list)
    tracks: list[TrackMetadata] = field(default_factory=list)


@dataclass
class ArtistMetadata:
    name: str
    asin: str
    album_asins: list[str] = field(default_factory=list)


_EXPLICITNESS_LABEL_RE = re.compile(r"\s*\[(?:explicit|clean)\]\s*$", re.IGNORECASE)


def _strip_explicitness_label(name: str | None) -> str | None:
    if not name:
        return name
    stripped = _EXPLICITNESS_LABEL_RE.sub("", name).strip()
    return stripped or name


def _ms_to_date(ms) -> str | None:
    if ms in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(
            int(ms) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _upgrade_cover(url: str | None) -> str | None:
    if not url:
        return url
    return re.sub(r"\._(S[XYL]|U[XY])\d+_", "._SL1200_", url)


def _search_cover(
    session: "AmazonMusicMobileAPI",
    artist: str,
    title: str,
    asins: list[str],
    fallback: str | None,
) -> str | None:
    asins = [a for a in asins if a]
    if not asins:
        return fallback

    try:
        doc = session.search(
            query=f'"{artist}" - "{title}"',
            asins=tuple(asins),
            search_types=("catalog_album", "catalog_track"),
            limit=100,
            region_to_use=session.credentials.account_region,
        )
    except Exception:
        return fallback

    if not isinstance(doc, dict):
        return fallback

    art = doc.get("artOriginal") or {}
    url = art.get("artUrl") if isinstance(art, dict) else None
    if not url:
        album_art = (doc.get("metadata") or {}).get("albumArt") or {}
        url = album_art.get("url") if isinstance(album_art, dict) else None
    return str(url) if url else fallback


def _hi_res_cover(session: "AmazonMusicMobileAPI", album_data: dict) -> str | None:
    return _search_cover(
        session,
        artist=(
            album_data.get("primaryArtistName")
            or (album_data.get("artist") or {}).get("name")
            or ""
        ),
        title=album_data.get("title") or "",
        asins=[
            album_data.get("asin"),
            album_data.get("globalAsin"),
            album_data.get("requestedAsin"),
            *(t.get("asin") for t in album_data.get("tracks", [])),
        ],
        fallback=_upgrade_cover(album_data.get("image")),
    )


def resolve_track_cover(
    session: "AmazonMusicMobileAPI", track: "TrackMetadata"
) -> str | None:
    return _search_cover(
        session,
        artist=track.album_artist or track.artist or "",
        title=track.album_name or "",
        asins=[track.album_asin, track.asin],
        fallback=track.cover_url,
    )


def _composers(track_data: dict) -> str | None:
    writers = [str(w).strip() for w in (track_data.get("songWriters") or []) if w]
    if not writers:
        return None
    return "; ".join(sorted(set(writers)))


def _merchant(product: dict) -> str | None:
    name = product.get("merchantName")
    return " ".join(str(name).split()) if name else None


def _reviews(album_data: dict) -> tuple[float | None, int | None]:
    reviews = album_data.get("reviews") or {}
    if not isinstance(reviews, dict):
        return None, None
    return reviews.get("average"), reviews.get("total")


def _album_release_date(album_data: dict) -> str | None:
    return _ms_to_date(
        album_data.get("originalReleaseDate") or album_data.get("merchantReleaseDate")
    )


def _build_track(
    track_data: dict, album_data: dict, disc_total: int, cover_url: str | None
) -> TrackMetadata:
    product = album_data.get("productDetails") or {}
    review_average, review_count = _reviews(album_data)
    return TrackMetadata(
        asin=track_data.get("asin"),
        title=_strip_explicitness_label(track_data.get("title")),
        artist=(track_data.get("artist") or {}).get("name"),
        album_name=_strip_explicitness_label(album_data.get("title")),
        album_artist=album_data.get("primaryArtistName")
        or (album_data.get("artist") or {}).get("name"),
        album_asin=album_data.get("asin"),
        disc=int(track_data.get("discNum") or 1),
        track_number=int(track_data.get("trackNum") or 1),
        total_tracks=int(album_data.get("trackCount") or 1),
        total_discs=disc_total,
        duration_seconds=int(track_data.get("duration") or 0),
        is_explicit=bool(
            (track_data.get("parentalControls") or {}).get("hasExplicitLanguage", False)
        ),
        isrc=track_data.get("isrc"),
        composers=_composers(track_data),
        release_date=_album_release_date(album_data),
        copyright=product.get("copyright"),
        label=product.get("label"),
        genre=product.get("primaryGenreName"),
        merchant=_merchant(product),
        review_average=review_average,
        review_count=review_count,
        cover_url=cover_url,
    )


def _disc_total(album_data: dict) -> int:
    discs = [int(t.get("discNum") or 1) for t in album_data.get("tracks", []) if t]
    return max(discs) if discs else 1


def _fetch_album_data(session: AmazonMusicMobileAPI, album_asin: str) -> dict:
    resp = session.get_metadata((album_asin,))
    albums = resp.get("albumList") or []
    if not albums:
        raise ValueError(f"album {album_asin} not found in muse response")
    return albums[0]


def _fetch_tracks(session: AmazonMusicMobileAPI, asins: list[str]) -> dict:
    out: dict = {}
    for i in range(0, len(asins), _BATCH_SIZE):
        chunk = tuple(asins[i:i + _BATCH_SIZE])
        resp = session.get_metadata(chunk)
        for td in resp.get("trackList") or []:
            if td.get("asin"):
                out[td["asin"]] = td
    return out


def fetch_meta_chunk(
    session: AmazonMusicMobileAPI, asins: list[str]
) -> tuple[dict, dict]:
    resp = session.get_metadata(tuple(asins))
    tracks = {td["asin"]: td for td in (resp.get("trackList") or []) if td.get("asin")}
    albums = {ad["asin"]: ad for ad in (resp.get("albumList") or []) if ad.get("asin")}
    return tracks, albums


def fetch_tracks_and_albums(
    session: AmazonMusicMobileAPI, asins: list[str]
) -> tuple[dict, dict]:
    tracks: dict = {}
    albums: dict = {}
    for i in range(0, len(asins), _BATCH_SIZE):
        t, a = fetch_meta_chunk(session, asins[i:i + _BATCH_SIZE])
        tracks.update(t)
        albums.update(a)
    return tracks, albums


def _resolve_playlist(
    session: AmazonMusicMobileAPI, content_asin: str, prefer_user: bool,
    build_tracks: bool = True,
) -> Optional["PlaylistMetadata"]:
    from amzdl.metadata.playlist import try_fetch_playlist, try_fetch_user_playlist
    if prefer_user:
        return (try_fetch_user_playlist(session, content_asin, build_tracks)
                or try_fetch_playlist(session, content_asin, build_tracks))
    return (try_fetch_playlist(session, content_asin, build_tracks)
            or try_fetch_user_playlist(session, content_asin, build_tracks))


def fetch_metadata(
    session: AmazonMusicMobileAPI, content_asin: str, defer_track_cover: bool = False,
    type_hint: str | None = None, defer_playlist_tracks: bool = False,
) -> tuple[str, object]:
    build_tracks = not defer_playlist_tracks
    if type_hint in ("playlist", "user-playlist"):
        playlist = _resolve_playlist(
            session, content_asin, prefer_user=(type_hint == "user-playlist"),
            build_tracks=build_tracks,
        )
        if playlist is not None:
            return "playlist", playlist

    try:
        resp = session.get_metadata((content_asin,))
    except Exception:
        resp = {}
    tracks_list = resp.get("trackList") or []
    albums_list = resp.get("albumList") or []
    artists_list = resp.get("artistList") or []

    track_data = next((t for t in tracks_list if t.get("asin") == content_asin), None)
    album_match = next((a for a in albums_list if a.get("asin") == content_asin), None)
    artist_match = next(
        (a for a in artists_list if a.get("asin") == content_asin), None
    )

    if artist_match or (artists_list and not albums_list and not tracks_list):
        from amzdl.metadata.discography import build_artist
        return "artist", build_artist(
            session, artist_match or artists_list[0], content_asin
        )

    if track_data or (tracks_list and not album_match):
        track_data = track_data or tracks_list[0]
        album_asin = (track_data.get("album") or {}).get("asin")
        if not album_asin:
            raise ValueError(f"track {content_asin} has no album reference")
        album_data = next((a for a in albums_list if a.get("asin") == album_asin), None)
        if not album_data:
            album_data = _fetch_album_data(session, album_asin)
        cover_url = (
            _upgrade_cover(album_data.get("image"))
            if defer_track_cover
            else _hi_res_cover(session, album_data)
        )
        return "track", _build_track(
            track_data, album_data, _disc_total(album_data), cover_url
        )

    album_data = album_match or (albums_list[0] if albums_list else None)
    if not album_data:
        playlist = _resolve_playlist(
            session, content_asin, prefer_user=len(str(content_asin)) != 10,
            build_tracks=build_tracks,
        )
        if playlist is not None:
            return "playlist", playlist
        raise ValueError(
            f"ASIN {content_asin} did not resolve to a track, album, artist, "
            "or playlist"
        )

    disc_total = _disc_total(album_data)
    track_count = int(album_data.get("trackCount") or 0)
    light_tracks = album_data.get("tracks") or []
    track_asins = [t.get("asin") for t in light_tracks if t.get("asin")]
    rich = _fetch_tracks(session, track_asins)

    cover_url = _hi_res_cover(session, album_data)

    tracks: list[TrackMetadata] = []
    for lt in light_tracks:
        asin = lt.get("asin")
        td = rich.get(asin, lt)
        tracks.append(_build_track(td, album_data, disc_total, cover_url))

    product = album_data.get("productDetails") or {}
    album = AlbumMetadata(
        album_name=_strip_explicitness_label(album_data.get("title")),
        artist_name=album_data.get("primaryArtistName")
        or (album_data.get("artist") or {}).get("name"),
        album_asin=album_data.get("asin"),
        cover_url=cover_url,
        release_date=_album_release_date(album_data),
        copyright=product.get("copyright"),
        label=product.get("label"),
        genre=product.get("primaryGenreName"),
        track_count=track_count or len(tracks),
        total_discs=disc_total,
        tracks=tracks,
    )
    return "album", album

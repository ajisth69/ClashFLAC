"""Playlist resolution via submodule endpoints (catalog and user/library).
Members come from `get_catalog_playlist` or `get_user_playlist` and are built
into full `TrackMetadata` that keep their own album tags, so tracks still land
under `<album_artist>/<album>/`."""


from amazonmusic.azapi import AmazonMusicMobileAPI
from amzdl.metadata.metadata import (
    PlaylistMetadata,
    TrackMetadata,
    _build_track,
    _disc_total,
    _fetch_album_data,
    _hi_res_cover,
    fetch_tracks_and_albums,
)


def _playlist_track_asin(track: dict) -> str | None:
    if not isinstance(track, dict):
        return None
    meta = track.get("metadata")
    if isinstance(meta, dict) and meta.get("asin"):
        return str(meta["asin"])
    if track.get("asin"):
        return str(track["asin"])
    return None


def _build_tracks_from_asins(
    session: AmazonMusicMobileAPI, track_asins: list[str]
) -> list[TrackMetadata]:
    rich, albums = fetch_tracks_and_albums(session, track_asins)
    cover_cache: dict = {}
    tracks: list[TrackMetadata] = []
    for asin in track_asins:
        td = rich.get(asin)
        if not td:
            continue
        album_asin = (td.get("album") or {}).get("asin")
        if not album_asin:
            continue
        album_data = albums.get(album_asin)
        if album_data is None:
            album_data = albums[album_asin] = _fetch_album_data(session, album_asin)
        if album_asin not in cover_cache:
            cover_cache[album_asin] = _hi_res_cover(session, album_data)
        tracks.append(
            _build_track(
                td, album_data, _disc_total(album_data), cover_cache[album_asin]
            )
        )
    return tracks


def _playlist_from_data(
    session: AmazonMusicMobileAPI, p_data: dict, playlist_id: str,
    build_tracks: bool = True,
) -> PlaylistMetadata | None:
    if not isinstance(p_data, dict):
        return None
    raw_tracks = p_data.get("tracks") or []
    track_asins = [a for a in (_playlist_track_asin(t) for t in raw_tracks) if a]
    if not track_asins:
        return None
    tracks = _build_tracks_from_asins(session, track_asins) if build_tracks else []
    if build_tracks and not tracks:
        return None
    meta = p_data.get("metadata") if isinstance(p_data.get("metadata"), dict) else {}
    name = (
        meta.get("title")
        or p_data.get("title")
        or p_data.get("name")
        or "Unknown Playlist"
    )
    return PlaylistMetadata(
        name=str(name), asin=str(playlist_id), track_asins=track_asins, tracks=tracks
    )


def try_fetch_playlist(
    session: AmazonMusicMobileAPI, playlist_asin: str, build_tracks: bool = True
) -> PlaylistMetadata | None:
    try:
        catalog = session.get_catalog_playlist(playlist_asin)
    except Exception:
        return None
    if not isinstance(catalog, dict):
        return None
    p_data = catalog.get("playlist")
    if not isinstance(p_data, dict):
        p_data = catalog
    return _playlist_from_data(session, p_data, playlist_asin, build_tracks)


def try_fetch_user_playlist(
    session: AmazonMusicMobileAPI, playlist_id: str, build_tracks: bool = True
) -> PlaylistMetadata | None:
    try:
        resp = session.get_user_playlist(playlist_id)
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
    playlists = resp.get("playlists") or []
    if not playlists:
        return None
    return _playlist_from_data(session, playlists[0], playlist_id, build_tracks)

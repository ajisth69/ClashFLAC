"""Download orchestration: dispatch an ASIN/batch to the right concurrency
layout. `download()` resolves one input's kind and routes it (single track /
album / artist / playlist); `download_batch()` runs a text file's inputs through
the same two-phase bar."""

import asyncio
import logging

from amzdl.cli import cli
from amzdl.cli.progress import Progress
from amzdl.download.fetch_track import fetch_track, process_track, purge_temp_dir
from amzdl.download.mpd_info import fetch_representations, select_representation
from amzdl.metadata.metadata import (
    _BATCH_SIZE,
    _build_track,
    _disc_total,
    _fetch_album_data,
    _hi_res_cover,
    fetch_meta_chunk,
    fetch_metadata,
)

_log = logging.getLogger("downloader.download")


def _note_skipped(results):
    skipped = sum(1 for r in results if r)
    if skipped:
        cli.note(f"{skipped} file(s) already existed; skipped.")


def _report_failures(failures, total):
    if failures:
        cli.note(f"{len(failures)} of {total} input(s) failed:")
        for asin, err in failures:
            cli.note(f"  {asin}: {err}")


async def _fetch_track_streams(session, asin, quality):
    return await asyncio.gather(
        asyncio.to_thread(fetch_representations, session, asin, quality),
        asyncio.to_thread(session.get_track_lyrics, asin),
        return_exceptions=True,
    )


def _has_lyrics(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    return bool((resp.get("lyrics") or {}).get("lines"))


async def download(session, asin, output_dir, quality, wvd_path=None, plain=False,
                   concurrency=5, metadata_concurrency=10, type_hint=None):
    prog = Progress(asin=asin, plain=plain)

    try:
        track_streams = None
        if type_hint == "track":
            prog.set_desc("fetching metadata, manifest & lyrics")
            meta_res, track_streams = await asyncio.gather(
                asyncio.to_thread(
                    fetch_metadata, session, asin, defer_track_cover=True,
                    type_hint=type_hint,
                ),
                _fetch_track_streams(session, asin, quality),
            )
            kind, meta = meta_res
        else:
            prog.set_desc("fetching metadata")
            kind, meta = await asyncio.to_thread(
                fetch_metadata, session, asin, defer_track_cover=True,
                type_hint=type_hint, defer_playlist_tracks=True,
            )

        if kind == "artist":
            prog.abort()
            await _download_artist(
                session, asin, meta, output_dir, quality, wvd_path, plain,
                concurrency, metadata_concurrency
            )
            return

        if kind == "playlist":
            prog.abort()
            await _download_playlist(
                session, asin, meta, output_dir, quality, wvd_path, plain,
                concurrency, metadata_concurrency
            )
            return

        if kind == "track":
            prog.set_name(meta.title)
            if track_streams is None:
                prog.set_desc("fetching manifest & lyrics")
                track_streams = await _fetch_track_streams(session, meta.asin, quality)
            reps_res, lyrics_res = track_streams
            representations = reps_res
            if isinstance(representations, Exception) or not representations:
                representations = fetch_representations(session, meta.asin, quality)
            representation = select_representation(meta.asin, representations, quality)
            lyrics_resp = None if isinstance(lyrics_res, Exception) else lyrics_res
            if meta.asin != asin and not _has_lyrics(lyrics_resp):
                lyrics_resp = await asyncio.to_thread(
                    session.get_track_lyrics, meta.asin
                )
            skipped = await process_track(
                session, meta, representation, output_dir, True, lyrics_resp,
                on_step=lambda desc: prog.update(desc), wvd_path=wvd_path,
                resolve_hi_res_cover=True,
            )
            prog.finish()
            _note_skipped([skipped])
        else:
            prog.set_name(meta.album_name)
            prog.begin_custom(len(meta.tracks))
            results = await _run_tracks(
                prog, session, meta.tracks, output_dir, quality, wvd_path, concurrency
            )
            prog.finish()
            _note_skipped(results)
    except Exception:
        prog.abort()
        raise
    finally:
        purge_temp_dir(output_dir)


async def _run_tracks(
    prog, session, tracks, output_dir, quality, wvd_path, concurrency
):
    sem = asyncio.Semaphore(concurrency)

    async def run_track(track):
        async with sem:
            slot = prog.track_start(track.title)
            try:
                return await fetch_track(
                    session, track, output_dir, quality,
                    on_step=lambda desc: prog.track_step(slot, desc),
                    wvd_path=wvd_path,
                )
            finally:
                prog.track_done(slot)

    return await asyncio.gather(*(run_track(t) for t in tracks))


async def _artist_tracks(session, artist, metadata_concurrency, on_album=None):
    sem = asyncio.Semaphore(max(1, metadata_concurrency))
    metas = []

    async def fetch_one(album_asin):
        async with sem:
            try:
                kind, meta = await asyncio.to_thread(
                    fetch_metadata, session, album_asin
                )
                if kind == "album" and getattr(meta, "tracks", None):
                    metas.append(meta)
            except Exception:
                _log.warning(
                    "album metadata fetch failed for %s; skipping it", album_asin,
                    exc_info=True,
                )
            finally:
                if on_album:
                    on_album()

    await asyncio.gather(*(fetch_one(a) for a in artist.album_asins))
    return [track for meta in metas for track in meta.tracks]


async def _resolve_to_tracks(session, asin, metadata_concurrency):
    kind, meta = await asyncio.to_thread(fetch_metadata, session, asin)
    if kind == "track":
        return [meta]
    if kind in ("album", "playlist"):
        return list(meta.tracks)
    if kind == "artist":
        return await _artist_tracks(session, meta, metadata_concurrency)
    return []


async def _download_artist(session, asin, artist, output_dir, quality, wvd_path,
                           plain, concurrency, metadata_concurrency):
    albums = artist.album_asins
    if not albums:
        cli.note(f"No albums found for artist '{artist.name}'.")
        return

    prog = Progress(asin=asin, plain=plain)
    prog.set_name(artist.name)
    try:
        prog.begin_custom(len(albums), rate_label="albums/s")
        tracks = await _artist_tracks(
            session, artist, metadata_concurrency, on_album=prog.advance_aggregate
        )
        if not tracks:
            prog.abort()
            cli.note(f"No downloadable tracks found for artist '{artist.name}'.")
            return

        prog.begin_custom(len(tracks), rate_label="tracks/s")
        results = await _run_tracks(
            prog, session, tracks, output_dir, quality, wvd_path, concurrency
        )
        prog.finish()
        _note_skipped(results)
    except Exception:
        prog.abort()
        raise
    finally:
        purge_temp_dir(output_dir)


async def _playlist_member_meta(session, track_asins, metadata_concurrency):
    sem = asyncio.Semaphore(max(1, metadata_concurrency))
    rich: dict = {}
    albums: dict = {}
    chunks = [track_asins[i:i + _BATCH_SIZE]
              for i in range(0, len(track_asins), _BATCH_SIZE)]

    async def fetch_chunk(chunk):
        async with sem:
            try:
                tracks, alb = await asyncio.to_thread(fetch_meta_chunk, session, chunk)
            except Exception:
                _log.warning(
                    "playlist member metadata fetch failed for %d track(s); "
                    "skipping them",
                    len(chunk), exc_info=True,
                )
                return
            rich.update(tracks)
            albums.update(alb)

    await asyncio.gather(*(fetch_chunk(c) for c in chunks))

    by_album: dict = {}
    for asin in track_asins:
        td = rich.get(asin)
        album_asin = (td.get("album") or {}).get("asin") if td else None
        if album_asin:
            by_album.setdefault(album_asin, []).append(asin)
    return rich, albums, by_album


async def _build_playlist_tracks(session, rich, albums, by_album, track_asins,
                                 metadata_concurrency, on_album=None):
    sem = asyncio.Semaphore(max(1, metadata_concurrency))
    built: dict = {}

    async def build_album(album_asin, members):
        try:
            async with sem:
                try:
                    album_data = albums.get(album_asin) or await asyncio.to_thread(
                        _fetch_album_data, session, album_asin
                    )
                except Exception:
                    _log.warning(
                        "playlist album build failed for %s; skipping its track(s)",
                        album_asin, exc_info=True,
                    )
                    return
                cover = await asyncio.to_thread(_hi_res_cover, session, album_data)
                disc_total = _disc_total(album_data)
                for asin in members:
                    if asin in rich:
                        built[asin] = _build_track(
                            rich[asin], album_data, disc_total, cover
                        )
        finally:
            if on_album:
                on_album()

    await asyncio.gather(*(build_album(a, m) for a, m in by_album.items()))
    return [built[a] for a in track_asins if a in built]


async def _download_playlist(session, asin, playlist, output_dir, quality, wvd_path,
                             plain, concurrency, metadata_concurrency):
    track_asins = playlist.track_asins
    if not track_asins:
        cli.note(f"No tracks found in playlist '{playlist.name}'.")
        return

    prog = Progress(asin=asin, plain=plain)
    prog.set_name(playlist.name)
    try:
        prog.set_desc("fetching metadata")
        rich, albums, by_album = await _playlist_member_meta(
            session, track_asins, metadata_concurrency
        )
        prog.begin_custom(len(by_album), rate_label="albums/s")
        tracks = await _build_playlist_tracks(
            session, rich, albums, by_album, track_asins, metadata_concurrency,
            on_album=prog.advance_aggregate,
        )
        if not tracks:
            prog.abort()
            cli.note(f"No downloadable tracks found in playlist '{playlist.name}'.")
            return

        prog.begin_custom(len(tracks), rate_label="tracks/s")
        results = await _run_tracks(
            prog, session, tracks, output_dir, quality, wvd_path, concurrency
        )
        prog.finish()
        _note_skipped(results)
    except Exception:
        prog.abort()
        raise
    finally:
        purge_temp_dir(output_dir)


async def download_batch(session, source_label, asins, output_dir, quality, wvd_path,
                         plain, concurrency, metadata_concurrency):
    prog = Progress(asin=source_label, plain=plain)
    failures = []
    try:
        prog.begin_custom(len(asins), rate_label="input/s")
        sem = asyncio.Semaphore(max(1, metadata_concurrency))
        per_input = [None] * len(asins)

        async def resolve_one(idx, asin):
            async with sem:
                try:
                    per_input[idx] = await _resolve_to_tracks(
                        session, asin, metadata_concurrency
                    )
                except Exception as exc:
                    failures.append((asin, str(exc) or type(exc).__name__))
                finally:
                    prog.advance_aggregate()

        await asyncio.gather(*(resolve_one(i, a) for i, a in enumerate(asins)))

        tracks = [track for group in per_input if group for track in group]
        if not tracks:
            prog.abort()
            cli.note("No downloadable tracks found in input.")
            _report_failures(failures, len(asins))
            return

        prog.begin_custom(len(tracks), rate_label="tracks/s")
        results = await _run_tracks(
            prog, session, tracks, output_dir, quality, wvd_path, concurrency
        )
        prog.finish()
        _note_skipped(results)
        _report_failures(failures, len(asins))
    except Exception:
        prog.abort()
        raise
    finally:
        purge_temp_dir(output_dir)

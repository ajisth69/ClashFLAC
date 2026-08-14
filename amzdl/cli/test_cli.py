"""Offline smoke test for the CLI. Drives the real `main.download()` flow and
`Progress` bar with auth, metadata, manifest, lyrics and the download pipeline
all replaced with fakes, across five modes matching `run_download()`'s
branches."""

import asyncio
import os
import random
import sys
import tempfile

from amzdl import main
from amzdl.download import download
from amzdl.metadata.metadata import (
    AlbumMetadata,
    ArtistMetadata,
    PlaylistMetadata,
    TrackMetadata,
)

_FAKE_TRACK_ASIN = "B0FAKETRACK"
_FAKE_ALBUM_ASIN = "B0FAKEALBUM"
_FAKE_PLAYLIST_ASIN = "B0FAKEPLST"
_FAKE_ARTIST_ASIN = "B0FAKEARTS"

_FAKE_STEPS = [
    ("downloading track", 0.8),
    ("decrypting", 0.4),
    ("remuxing to flac", 0.4),
    ("tagging metadata", 0.3),
]

_FAKE_ALBUM_STEPS = [("fetching manifest", 0.3)] + _FAKE_STEPS

_ALBUM_TRACKS = [
    "Opening Theme",
    "A Considerably Longer Track Title That Runs On",
    "Interlude",
    "Midpoint",
    "Another Fairly Long Song Name For Good Measure",
    "Short One",
    "Penultimate",
    "Bridge",
    "Reprise",
    "Hidden Track",
    "Encore",
    "Closing Credits",
]


class FakeSession:

    def get_track_lyrics(self, asin):
        return None


def _make_track(asin, title, track_number=1, total_tracks=1, disc=1, total_discs=1):
    return TrackMetadata(
        asin=asin,
        title=title,
        artist="Fake Artist",
        album_name="Fake Album",
        album_artist="Fake Artist",
        album_asin=_FAKE_ALBUM_ASIN,
        disc=disc,
        track_number=track_number,
        total_tracks=total_tracks,
        total_discs=total_discs,
        duration_seconds=212,
        is_explicit=False,
        isrc="USFAKE0000001",
        release_date="2024-01-01",
        copyright="℗ 2024 Fake Records",
        label="Fake Records",
        genre="Test",
    )


def _fake_track_metadata(session, asin, defer_track_cover=False, type_hint=None,
                         defer_playlist_tracks=False):
    track = _make_track(asin, "Fake Track Marquee Example Fake Track Marquee Example")
    return "track", track


def _fake_album_metadata(session, asin, defer_track_cover=False, type_hint=None,
                         defer_playlist_tracks=False):
    n = len(_ALBUM_TRACKS)
    tracks = [
        _make_track(f"{asin}T{i:02d}", title, track_number=i, total_tracks=n)
        for i, title in enumerate(_ALBUM_TRACKS, start=1)
    ]
    album = AlbumMetadata(
        album_name="Fake Album: A Multi-Track Marquee Example Collection",
        artist_name="Fake Artist",
        album_asin=asin,
        cover_url=None,
        release_date="2024-01-01",
        copyright="℗ 2024 Fake Records",
        label="Fake Records",
        genre="Test",
        track_count=n,
        total_discs=1,
        tracks=tracks,
    )
    return "album", album


def _fake_playlist_metadata(session, asin, defer_track_cover=False, type_hint=None,
                            defer_playlist_tracks=False):
    n = len(_ALBUM_TRACKS)
    track_asins = [f"{asin}T{i:02d}" for i in range(1, n + 1)]
    tracks = [] if defer_playlist_tracks else [
        _make_track(a, title, track_number=i, total_tracks=n)
        for i, (a, title) in enumerate(
            zip(track_asins, _ALBUM_TRACKS, strict=False), start=1
        )
    ]
    return "playlist", PlaylistMetadata(
        name="Fake Playlist: A Marquee Example Mix", asin=asin,
        track_asins=track_asins, tracks=tracks,
    )


_FAKE_PLAYLIST_ALBUMS = 3


async def _fake_playlist_member_meta(session, track_asins, metadata_concurrency):
    by_album = {}
    for i, asin in enumerate(track_asins):
        album = f"{_FAKE_ALBUM_ASIN}{i % _FAKE_PLAYLIST_ALBUMS}"
        by_album.setdefault(album, []).append(asin)
    return {}, {}, by_album


async def _fake_build_playlist_tracks(session, rich, albums, by_album, track_asins,
                                      metadata_concurrency, on_album=None):
    n = len(track_asins)
    titles = (_ALBUM_TRACKS * (n // len(_ALBUM_TRACKS) + 1))[:n]
    title_of = dict(zip(track_asins, titles, strict=False))
    built = {}
    for members in by_album.values():
        await asyncio.sleep(0.3)
        for asin in members:
            built[asin] = _make_track(asin, title_of[asin], total_tracks=n)
        if on_album:
            on_album()
    return [built[a] for a in track_asins if a in built]


def _fake_artist_metadata(session, asin, defer_track_cover=False, type_hint=None,
                          defer_playlist_tracks=False):
    if asin == _FAKE_ARTIST_ASIN:
        return "artist", ArtistMetadata(
            name="Fake Artist",
            asin=asin,
            album_asins=[f"{_FAKE_ALBUM_ASIN}1", f"{_FAKE_ALBUM_ASIN}2"],
        )
    return _fake_album_metadata(session, asin)


def _fake_batch_metadata(session, asin, defer_track_cover=False, type_hint=None,
                         defer_playlist_tracks=False):
    if asin == _FAKE_ARTIST_ASIN:
        return _fake_artist_metadata(session, asin)
    if asin == _FAKE_PLAYLIST_ASIN:
        return _fake_playlist_metadata(session, asin)
    if asin == _FAKE_TRACK_ASIN:
        return _fake_track_metadata(session, asin)
    return _fake_album_metadata(session, asin)


def _fake_representations(session, asin, quality=None):
    return [
        {
            "id": "1",
            "track_type": "HD",
            "codec": "flac",
            "bandwidth": 940000,
            "sample_rate": "44100",
            "bit_depth": "16",
            "base_url": "https://example.invalid/fake.mp4",
            "first_segment_range": None,
            "pssh": "FAKEPSSH==",
        },
    ]


async def _fake_process_track(session, track, representation, output_dir,
                              build_folder_structure=True, lyrics_resp=None,
                              on_step=None, wvd_path="device.wvd",
                              resolve_hi_res_cover=False):
    for desc, delay in _FAKE_STEPS:
        if on_step:
            on_step(desc)
        await asyncio.sleep(delay)


async def _fake_fetch_track(session, track, output_dir, quality,
                            build_folder_structure=True, on_step=None,
                            wvd_path="device.wvd"):
    for desc, delay in _FAKE_ALBUM_STEPS:
        if on_step:
            on_step(desc)
        await asyncio.sleep(delay * random.uniform(0.5, 1.6))


def main_test():
    argv = sys.argv[1:]
    album = "--album" in argv
    playlist = "--playlist" in argv
    artist = "--artist" in argv
    batch = "--batch" in argv
    argv = [
        a for a in argv if a not in ("--album", "--playlist", "--artist", "--batch")
    ]

    if batch:
        default_asin, fake_metadata = None, _fake_batch_metadata
    elif artist:
        default_asin, fake_metadata = _FAKE_ARTIST_ASIN, _fake_artist_metadata
    elif playlist:
        default_asin, fake_metadata = _FAKE_PLAYLIST_ASIN, _fake_playlist_metadata
    elif album:
        default_asin, fake_metadata = _FAKE_ALBUM_ASIN, _fake_album_metadata
    else:
        default_asin, fake_metadata = _FAKE_TRACK_ASIN, _fake_track_metadata

    main.auth.get_session = lambda *a, **k: FakeSession()
    download.fetch_metadata = fake_metadata
    download.fetch_representations = _fake_representations
    download.process_track = _fake_process_track
    download.fetch_track = _fake_fetch_track
    download._playlist_member_meta = _fake_playlist_member_meta
    download._build_playlist_tracks = _fake_build_playlist_tracks

    batch_file = None
    if batch:
        fd, batch_file = tempfile.mkstemp(prefix="batch-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join((
                "# fake batch of mixed inputs",
                _FAKE_TRACK_ASIN,
                _FAKE_ALBUM_ASIN,
                _FAKE_PLAYLIST_ASIN,
                _FAKE_ARTIST_ASIN,
            )) + "\n")

    arg = argv[0] if argv else (batch_file or default_asin)
    sys.argv = ["test_cli.py", arg, "--quality", "HD"]
    try:
        main.main()
    finally:
        if batch_file:
            os.remove(batch_file)


if __name__ == "__main__":
    main_test()

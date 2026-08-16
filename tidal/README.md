# TIDAL Music Resolution & Download Engine (Testing Module)

Standalone Tidal integration inspired by [yaronzz/Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader).

All Tidal functionality is encapsulated inside the `tidal/` folder and exposes exact Amazon Music-compatible endpoints under `/api/tidal/*`.

---

## Package Setup & Installation

Install or register the package locally matching the exact `tidal-dl` method:

```bash
# Development editable install
python setup.py develop

# Or standard install
python setup.py install
```

---

## Interactive `tidal-dl` CLI

Run the interactive console menu or pass command-line arguments:

```bash
# Launch interactive menu
python -m tidal

# Or use CLI flags
python -m tidal -s "Blinding Lights"
python -m tidal -l "134858527" -q HD -o "./downloads/tidal"
python -m tidal --login
```

---

## Folder Structure

```
├── setup.py            # Standard Python setuptools installer
tidal/
├── __init__.py         # Package exports
├── __main__.py         # python -m tidal entry point
├── cli.py              # tidal-dl interactive menu & CLI
├── config.py           # API endpoints, Client IDs, quality presets
├── models.py           # Pydantic contracts (exact match to Amazon endpoints)
├── auth.py             # Dual Client Credentials & OAuth2 Device Login
├── decryption.py       # AES-CTR token decryption for legacy streams
├── api.py              # Catalog search, track/album metadata, manifest resolver
├── downloader.py       # Track streaming, decryption, and FLAC/M4A tagging
├── routes.py           # FastAPI APIRouter mounted at /api/tidal/*
├── test_client.py      # Standalone CLI testing script
└── README.md           # Documentation
```

---

## API Endpoints (Exact Amazon Contract)

### 1. Catalog Search
- **Endpoint:** `GET /api/tidal/search`
- **Query Params:** `q` (search query), `limit` (default 5, max 20)
- **Response:** `List[SearchResultItem]`
```json
[
  {
    "asin": "134858527",
    "title": "Blinding Lights",
    "artist": "The Weeknd",
    "album": "After Hours",
    "duration_sec": 200,
    "thumbnail_url": "https://resources.tidal.com/images/8ed32167/0df3/4a8b/b59f/471861dd1fd1/1280x1280.jpg",
    "url": "https://tidal.com/browse/track/134858527",
    "release_date": "2020-03-20",
    "year": "2020",
    "genre": null,
    "explicit": false,
    "track_number": 9,
    "disc_number": 1
  }
]
```

### 2. Track Resolution
- **Endpoint:** `POST /api/tidal/resolve`
- **Body:** `{"input": "134858527", "quality": "HD"}`
- **Response:** `TrackResponse`
```json
{
  "source_type": "tidal",
  "asin": "134858527",
  "title": "Blinding Lights",
  "artist": "The Weeknd",
  "album": "After Hours",
  "duration_sec": 200,
  "thumbnail_url": "https://resources.tidal.com/images/8ed32167/0df3/4a8b/b59f/471861dd1fd1/1280x1280.jpg",
  "stream_url": "https://...",
  "codec": "flac",
  "bitrate": 1411000
}
```

### 3. URL Resolution to Query
- **Endpoint:** `GET /api/tidal/resolve?q=https://tidal.com/browse/track/134858527`
- **Response:** `{"resolved": "Blinding Lights The Weeknd"}`

### 4. Track Download
- **Endpoint:** `POST /api/tidal/download`
- **Body:** `{"input": "134858527", "quality": "HD"}`
- **Response:** `FileResponse` (`audio/flac` or `audio/mp4`) with `Content-Disposition: attachment; filename="1-09 Blinding Lights.flac"` and embedded metadata tags + cover art.

### 5. Authentication Endpoints
- **Check Status:** `GET /api/tidal/auth/status`
- **Initiate Device Login:** `POST /api/tidal/auth/device` (Returns `verification_uri_complete` e.g., `link.tidal.com/CODE`)
- **Check Device Login:** `POST /api/tidal/auth/check` (Body: `{"device_code": "..."}`)

---

## Environment Variables & Token Persistence

Tidal credentials can be supplied via:
1. **Interactive Device Login:** `POST /api/tidal/auth/device` or `python -m tidal --login` (tokens saved to `config/tidal_tokens.json`).
2. **Environment Variables:**
   - `TIDAL_ACCESS_TOKEN`
   - `TIDAL_REFRESH_TOKEN`
   - `TIDAL_USER_ID`
   - `TIDAL_COUNTRY_CODE`
3. **Base64 Cloud Variable:** `TIDAL_CREDENTIALS_BASE64` (for Railway/cloud deployments).

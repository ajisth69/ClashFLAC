# ClashFLAC &mdash; Documentation

Comprehensive technical documentation, setup guides, API specifications, and architecture details for ClashFLAC.

---

## 1. Overview & Architecture

**ClashFLAC** is a full-stack platform providing bit-perfect lossless FLAC audio streaming and download capabilities.

```
                  ┌───────────────────────────────┐
                  │    Vite + Vanilla JS Client   │
                  └──────────────┬────────────────┘
                                 │ HTTP / REST
                                 ▼
                  ┌───────────────────────────────┐
                  │      FastAPI REST Server      │
                  └──────┬─────────────────┬──────┘
                         │                 │
                         ▼                 ▼
             ┌──────────────────────┐  ┌──────────────────────┐
             │ Amazon Music Engine  │  │     Tidal Engine     │
             │   (amzdl / Widevine) │  │  (DASH / Direct Hi-Res│
             └──────────────────────┘  └──────────────────────┘
```

---

## 2. Prerequisites & Installation

### Requirements
- **Python:** 3.10 or higher
- **Node.js:** 18.x or higher (for frontend build / dev)
- **FFmpeg:** Installed and available in PATH (for uncompressed FLAC remuxing and tag embedding)

### Backend Setup
```bash
# Clone the repository
git clone https://github.com/ajisth69/ClashFLAC.git
cd ClashFLAC

# Install Python dependencies
pip install -r requirements.txt

# Start backend server
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

### Frontend Setup
```bash
cd frontend
npm install
npm run dev
```

---

## 3. Authentication & Account Configuration

ClashFLAC requires credentials to interact with third-party music catalogs.

### Amazon Music Setup
1. Run the account setup CLI:
   ```bash
   python amzdl/main.py accounts --add
   ```
2. Select your geographical region (`US`, `IN`, `GB`, `DE`, `JP`).
3. Open the generated URL in your browser, sign in, and paste the final redirect URL back into the CLI.
4. Credentials are automatically saved to `config/credentials.bin`.

### Tidal Setup
Tidal can be authorized using device login or environment variables:
- **Device Authorization (Browser):**
  1. Call `POST /api/tidal/auth/device` to obtain `user_code` and `verification_uri_complete`.
  2. Visit the URL (e.g. `https://link.tidal.com/XXXXX`) and approve the device.
  3. Call `POST /api/tidal/auth/check` with `{"device_code": "..."}` to save tokens to `config/tidal_tokens.json`.
- **CLI Method:**
  ```bash
  python -m tidal --login
  ```

---

## 4. API Reference

### Core & Health Endpoints

#### Health Check
- **Endpoint:** `GET /health`
- **Description:** Returns operational status of the server.
- **Response:**
  ```json
  {"status": "ok"}
  ```

#### Public Configuration
- **Endpoint:** `GET /api/config`
- **Description:** Returns server configuration and supported engine capabilities.

---

### Search Endpoints

#### Amazon Music Search
- **Endpoint:** `GET /api/search`
- **Query Parameters:**
  - `q` (string, required): Search query (artist, album, or track name).
- **Response:** List of track and album results from Amazon Music.

#### Spotify Metadata Search
- **Endpoint:** `GET /api/spotify/search`
- **Query Parameters:**
  - `q` (string, required): Search query.
- **Response:** List of metadata search results from Spotify catalog.

#### Tidal Search
- **Endpoint:** `GET /api/tidal/search`
- **Query Parameters:**
  - `q` (string, required): Search query.
  - `limit` (integer, optional): Maximum results to return (default: `5`, max: `20`).
- **Response:**
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

---

### Stream Resolution & Download Endpoints

#### Amazon Music Stream Resolution
- **Endpoint:** `POST /api/resolve`
- **Request Body:**
  ```json
  {
    "track_id": "B085XN1QRS",
    "quality": "HD"
  }
  ```
- **Response:** Stream URL and playback metadata.

#### Amazon Music Track Download
- **Endpoint:** `POST /api/download`
- **Request Body:**
  ```json
  {
    "track_id": "B085XN1QRS",
    "quality": "ULTRA_HD"
  }
  ```
- **Response:** Bit-perfect lossless `audio/flac` stream with Vorbis comment tags, synchronized LRC lyrics, and high-res cover art.

#### Tidal Stream Resolution
- **Endpoint:** `POST /api/tidal/resolve`
- **Request Body:**
  ```json
  {
    "input": "134858527",
    "quality": "HD"
  }
  ```
- **Response:** Resolved Tidal stream metadata, codec, and playback URLs.

#### Tidal Track Download
- **Endpoint:** `POST /api/tidal/download`
- **Request Body:**
  ```json
  {
    "input": "134858527",
    "quality": "HI_RES_LOSSLESS"
  }
  ```
- **Response:** Downloadable FLAC file (`audio/flac`) with `Content-Disposition: attachment; filename="..."`.

---

### Tidal Authentication Endpoints

#### Check Authentication Status
- **Endpoint:** `GET /api/tidal/auth/status`
- **Response:** `{"authenticated": true, "user_id": "..."}`

#### Initiate Device OAuth2 Flow
- **Endpoint:** `POST /api/tidal/auth/device`
- **Response:**
  ```json
  {
    "device_code": "...",
    "user_code": "...",
    "verification_uri": "https://link.tidal.com",
    "verification_uri_complete": "https://link.tidal.com/XXXXX",
    "expires_in": 300,
    "interval": 5
  }
  ```

#### Check Device Authorization
- **Endpoint:** `POST /api/tidal/auth/check`
- **Request Body:**
  ```json
  {
    "device_code": "..."
  }
  ```

---

## 5. Deployment Environment Variables

| Variable | Description |
| :--- | :--- |
| `AMZ_CREDENTIALS_BASE64` | Base64-encoded `credentials.bin` for headless Amazon Music auth |
| `TIDAL_CREDENTIALS_BASE64` | Base64-encoded `tidal_tokens.json` for headless Tidal auth |
| `TIDAL_ACCESS_TOKEN` | Direct OAuth2 access token for Tidal |
| `TIDAL_REFRESH_TOKEN` | Direct OAuth2 refresh token for Tidal |
| `PORT` | Web server listening port (default: `8000`) |

---

## 6. Disclaimer & Legal Notice

> [!WARNING]
> - **Experimental / Educational Purpose Only:** This project is developed strictly for educational, research, and personal experimental purposes.
> - **No Hosted Content:** This software does not host, store, or distribute any copyrighted media or audio files. All operations rely on user-supplied accounts and third-party APIs.
> - **Personal Use Only:** Kindly use this project for personal, non-commercial purposes only. Users are responsible for complying with the terms of service of third-party platforms.
> - **No Affiliation:** This project is not affiliated with, endorsed by, or associated with Amazon Music, TIDAL, Spotify, or any of their parent corporations.

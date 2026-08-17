<div align="center">

<img src="frontend/Clash%20music.png" alt="ClashFLAC Logo" width="110" style="border-radius: 20px;" />

# ClashFLAC
**24-bit Hi-Res Lossless FLAC Streaming & Download Platform**

*Instant browser audio previews, comprehensive multi-engine search across Amazon Music & Tidal, and bit-perfect lossless FLAC downloads with synchronized lyrics and high-resolution album artwork.*

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black" alt="JavaScript" />
  <img src="https://img.shields.io/badge/FLAC-Lossless_Audio-2ea44f?style=for-the-badge" alt="FLAC" />
  <img src="https://img.shields.io/badge/Tidal-Hi--Res_Master-000000?style=for-the-badge&logo=tidal&logoColor=white" alt="Tidal" />
  <img src="https://img.shields.io/badge/License-MIT-blueviolet?style=for-the-badge" alt="License" />
</p>

[Documentation](documentation.md)

</div>

> [!WARNING]
> ### ⚠️ Legal Disclaimer & Usage Notice
> 
> - **Experimental / Educational Purpose Only:** This project is developed strictly for educational, research, and personal experimental purposes.
> - **No Hosted Content:** This software does not host, store, or distribute any copyrighted media or audio files. All operations rely on user-supplied accounts and third-party APIs.
> - **Personal Use Only:** Kindly use this project for personal, non-commercial purposes only. Users are responsible for complying with the terms of service of third-party platforms.
> - **No Affiliation:** This project is not affiliated with, endorsed by, or associated with Amazon Music, TIDAL, Spotify, or any of their parent corporations.

---

## Overview

**ClashFLAC** is a full-stack music platform designed for discovering, streaming, and downloading bit-perfect lossless FLAC audio. It combines a high-performance Python FastAPI REST API backend with a responsive retro-themed web application, featuring dual lossless engines for **Amazon Music (Ultra HD)** and **Tidal (Hi-Res Lossless / Master)**.

---

## Capabilities

### Music Discovery & Playback
- **Multi-Engine Search**: Search across millions of tracks, albums, and artists using integrated Amazon Music, Tidal, and Spotify metadata engines.
- **Instant Browser Playback**: Stream high-quality audio previews directly in the browser with continuous queue management and shuffle/repeat modes.
- **Track Metadata**: View detailed track information including album names, release years, track numbers, and high-resolution cover artwork.

### Lossless Audio Downloads
- **Strict Bit-Perfect FLAC**: Download pure 16-bit Lossless and 24-bit Studio Master FLAC audio files directly to your device (no lossy M4A/AAC fallbacks).
- **Dual Lossless Engines**: 
  - **Amazon Music Engine**: Native Widevine DRM decryption with uncompressed FLAC remuxing.
  - **Tidal Engine**: Support for direct FLAC streams and segmented DASH Hi-Res master manifests with automatic frame remuxing.
- **Embedded Synchronized Lyrics**: Synchronized lyrics (LRC format from LRCLIB and native sources) are embedded directly inside the Vorbis metadata tags.
- **Embedded High-Resolution Artwork**: High-resolution album covers (1280x1280+) are embedded directly into each audio file.

### Interface & Controls
- **Audio Control Bar**: Persistent playback bar with real-time waveform progress, duration scrubbing, and volume control.
- **Engine Priority Switching**: Choose default preference between Amazon Music and Tidal with automatic cross-engine fallback.
- **Play Queue & History**: Manage an active playback queue and review recently played tracks.
- **Theme Customization**: Toggle between high-contrast Light and Dark visual themes.

---

## Repository Structure

```text
ClashFLAC/
├── amzdl/                    # Amazon Music lossless download & metadata engine
├── amazonmusic/              # Amazon Music protocol models & schemas
├── tidal/                    # Tidal Hi-Res Lossless (16/24-bit) download & auth engine
│   ├── api.py                # Tidal REST & DASH manifest client
│   ├── auth.py               # OAuth2 & session management
│   ├── downloader.py         # Lossless segment downloader & tagger
│   └── routes.py             # Tidal FastAPI router endpoints
├── frontend/                 # Single Page Web Application
│   ├── Clash music.png       # Brand asset & favicon
│   ├── index.html            # Main markup & icon templates
│   ├── style.css             # User interface design & animations
│   ├── app.js                # Player logic, state management, & API client
│   ├── vite.config.js        # Vite build & local dev proxy configuration
│   └── dist/                 # Pre-compiled static web bundle
├── Procfile                  # Cloud web process definition
├── railway.json              # Cloud deployment configuration
├── requirements.txt          # Python backend dependencies
└── server.py                 # FastAPI backend server
```

---

## Account Setup

Before running queries against the Amazon Music or Tidal catalogs, register your credentials using the setup guides:

- 👉 **[Amazon Music Account Setup & Device Registration Guide](setup.md)**
- 👉 **[Tidal Configuration & Credentials Guide](tidal/README.md)**

---

## Local Development

### 1. Backend Setup
```bash
git clone https://github.com/ajisth69/ClashFLAC.git
cd ClashFLAC

# Install backend dependencies
pip install -r requirements.txt

# Start backend server
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```
*The backend API will be running at `http://127.0.0.1:8000`.*

### 2. Frontend Setup
In a separate terminal:
```bash
cd frontend
npm install
npm run dev
```
*The web interface will be running at `http://localhost:5173` (with `/api` proxied to `http://127.0.0.1:8000`).*

---

## API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the web application interface |
| `GET` | `/health` | Service health status check |
| `GET` | `/api/config` | Public client configuration |
| `GET` | `/api/search?q={query}` | Search Amazon Music catalog for tracks and albums |
| `GET` | `/api/spotify/search?q={query}` | Search Spotify metadata |
| `POST` | `/api/resolve` | Resolve stream URLs and track playback information |
| `POST` | `/api/download` | Download Amazon Music lossless FLAC audio with embedded tags |
| `GET` | `/api/tidal/search?q={query}` | Search Tidal catalog for tracks and albums |
| `POST` | `/api/tidal/resolve` | Resolve Tidal track metadata and stream info |
| `POST` | `/api/tidal/download` | Download Tidal 16/24-bit Hi-Res FLAC audio with embedded tags |
| `GET` | `/api/tidal/auth/status` | Check Tidal user authentication status |
| `POST` | `/api/tidal/auth/device` | Initialize Tidal device OAuth2 authorization |
| `POST` | `/api/tidal/auth/token` | Poll Tidal OAuth2 device authorization token |

---

## Acknowledgments & Credits

- **Logo Design**: Special thanks to [@Dekuiuto](https://t.me/Dekuiuto) for crafting the brand logo and visuals.
- **Community Recommendation**: Special thanks to [@hariprabhu1008](https://t.me/hariprabhu1008) for featuring and recommending the project on his FOSS channel.

---

## License

This project is licensed under the **MIT License** &mdash; see the [LICENSE](LICENSE) file for details.

<div align="center">
  <sub>ClashFLAC &bull; Lossless Music Streaming & Download Platform</sub>
</div>

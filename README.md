<div align="center">

<img src="frontend/Clash%20music.png" alt="ClashFLAC Logo" width="110" style="border-radius: 20px;" />

# ClashFLAC
**24-bit Hi-Res Lossless FLAC Streaming & Download Platform**

*Instant browser audio previews, comprehensive multi-engine search across Amazon Music, Qobuz & Tidal, and bit-perfect lossless FLAC downloads with synchronized lyrics and high-resolution album artwork.*

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black" alt="JavaScript" />
  <img src="https://img.shields.io/badge/FLAC-Lossless_Audio-2ea44f?style=for-the-badge" alt="FLAC" />
  <img src="https://img.shields.io/badge/Qobuz-Hi--Res_Master-5925dc?style=for-the-badge" alt="Qobuz" />
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
> - **No Affiliation:** This project is not affiliated with, endorsed by, or associated with Amazon Music, Qobuz, TIDAL, Spotify, or any of their parent corporations.

---

## Overview

**ClashFLAC** is a full-stack music platform designed for discovering, streaming, and downloading bit-perfect lossless FLAC audio. It combines a high-performance Python FastAPI REST API backend with a responsive retro-themed web application, featuring a triple lossless engine architecture:
1. **Qobuz Engine (Priority 1 / Default)**: Hi-Res Lossless bit-perfect FLAC (always highest Studio Master resolution up to 24-bit / 192 kHz)
2. **Amazon Music Engine (Priority 2)**: Ultra HD Studio Master (up to 24-bit / 192 kHz)
3. **Tidal Engine (Priority 3)**: Hi-Res Lossless & Master audio (up to 24-bit / 192 kHz)

---

## Capabilities

### Music Discovery & Playback
- **Multi-Engine Search**: Concurrently searches across Qobuz, Amazon Music, Tidal, and Spotify catalogs.
- **Fast Browser Audio Playback & Previews**: Instant browser playback with continuous queue management and shuffle/repeat modes (supports JioSaavn full streams, Qobuz 320kbps Hi-Fi streams, and Spotify 30s previews).
- **Track Metadata**: View detailed track facts including album names, release years, track numbers, and high-resolution cover artwork.

### Lossless Audio Downloads
- **Strict Bit-Perfect FLAC**: Download uncompressed 16-bit CD Lossless and 24-bit Studio Master FLAC files (no lossy M4A/AAC conversions).
- **Triple Lossless Engines**: 
  - **Qobuz Engine**: Direct MD5-signed CDN audio stream downloading with Vorbis FLAC tagging (always fetched in maximum available studio master resolution).
  - **Amazon Music Engine**: Native Widevine DRM decryption with uncompressed FLAC remuxing.
  - **Tidal Engine**: Support for direct FLAC streams and segmented DASH Hi-Res master manifests with automatic frame remuxing.
- **Embedded Synchronized Lyrics**: Synchronized lyrics (LRC format from LRCLIB and native sources) embedded directly inside Vorbis tags.
- **Embedded High-Resolution Artwork**: High-resolution album covers (1280x1280+) embedded directly into each audio file.

### Interface & Controls
- **Engine Priority Order**: Default download priority cascade is `Qobuz -> Amazon Music -> Tidal`, with interactive manual engine switches in the Track Inspector and Settings dialog.
- **Audio Control Bar**: Persistent playback bar with real-time progress, duration scrubbing, and volume control.
- **Play Queue & History**: Manage an active playback queue and review recently played tracks.
- **Theme Customization**: Toggle between high-contrast Light and Dark visual themes.

---

## Repository Structure

```text
ClashFLAC/
├── amzdl/                    # Amazon Music lossless download & metadata engine
├── amazonmusic/              # Amazon Music protocol models & schemas
├── qobuz/                    # Qobuz Hi-Res Lossless (16/24-bit 192kHz) engine
│   ├── api.py                # Qobuz API client & request signing
│   ├── auth.py               # Credentials loader & token storage
│   ├── config.py             # App ID, secrets & quality format maps
│   ├── downloader.py         # Lossless stream downloader & Vorbis tagger
│   └── routes.py             # Qobuz FastAPI router endpoints
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

Before running queries against the lossless catalogs, configure your credentials using the setup guides:

- 👉 **[Account Setup & Credentials Guide](setup.md)**
- 👉 **[Tidal Configuration & Credentials Guide](tidal/README.md)**

---

## Local Development

### 1. Backend Setup
```bash
# Install Python dependencies
pip install -r requirements.txt

# Start backend server
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

### 2. Frontend Setup
```bash
cd frontend
npm install
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173) in your browser.

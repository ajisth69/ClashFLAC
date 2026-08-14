<div align="center">

<img src="frontend/Clash%20music.png" alt="ClashFLAC Logo" width="110" style="border-radius: 20px;" />

# ClashFLAC
**24-bit Hi-Res Lossless FLAC Streaming & Download Platform**

*Instant browser audio previews, comprehensive catalog search, and bit-perfect lossless FLAC downloads with synchronized lyrics and high-resolution album artwork.*

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black" alt="JavaScript" />
  <img src="https://img.shields.io/badge/FLAC-Lossless_Audio-2ea44f?style=for-the-badge" alt="FLAC" />
  <img src="https://img.shields.io/badge/License-MIT-blueviolet?style=for-the-badge" alt="License" />
</p>

[Live Web App](https://clashflac.pages.dev) &bull; [API Service](https://clashflac.up.railway.app) &bull; [Documentation](https://clashflac.up.railway.app/docs)

</div>

---

## Overview

**ClashFLAC** is a full-stack music platform designed for discovering, streaming, and downloading bit-perfect lossless FLAC audio. It combines a high-performance REST API backend with a responsive retro-themed web application.

---

## Capabilities

### Music Discovery & Playback
- **Catalog Search**: Search across millions of tracks, albums, and artists using integrated Amazon Music and Spotify metadata engines.
- **Instant Browser Playback**: Stream high-quality audio previews directly in the browser with continuous queue management and shuffle/repeat modes.
- **Track Metadata**: View detailed track information including album names, release years, track numbers, and high-resolution cover artwork.

### Lossless Audio Downloads
- **Bit-Perfect FLAC**: Download uncompressed 24-bit lossless FLAC audio files directly to your device.
- **Embedded Synchronized Lyrics**: Synchronized lyrics (LRC format) are embedded directly inside the audio metadata tags.
- **Embedded High-Resolution Artwork**: High-resolution album covers (500x500+) are embedded directly into each audio file.

### Interface & Controls
- **Audio Control Bar**: Persistent playback bar with real-time waveform progress, duration scrubbing, and volume control.
- **Play Queue & History**: Manage an active playback queue and review recently played tracks.
- **Theme Customization**: Toggle between high-contrast Light and Dark visual themes.

---

## Repository Structure

```text
ClashFLAC/
├── amzdl/                    # Amazon Music lossless download & metadata engine
├── amazonmusic/              # Amazon Music protocol models & schemas
├── frontend/                 # Single Page Web Application
│   ├── Clash music.png       # Brand asset & favicon
│   ├── index.html            # Main markup & icon templates
│   ├── style.css             # User interface design & animations
│   ├── app.js                # Player logic, state management, & API client
│   └── dist/                 # Pre-compiled static web bundle
├── Procfile                  # Cloud web process definition
├── railway.json              # Cloud deployment configuration
├── requirements.txt          # Python backend dependencies
└── server.py                 # FastAPI backend server
```

---

## Account Setup

Before running queries against the Amazon Music catalog, register your device credentials using the interactive setup tool.

👉 **[Complete Account Setup & Device Registration Guide](setup.md)**

---

## Local Development

### 1. Backend Setup
```bash
git clone https://github.com/ajisth69/ClashFLAC.git
cd ClashFLAC

# Install backend dependencies
pip install -r requirements.txt

# Start backend server
python -m uvicorn server:app --host 127.0.0.1 --port 8001 --reload
```
*The backend API will be running at `http://127.0.0.1:8001`.*

### 2. Frontend Setup
In a separate terminal:
```bash
cd frontend
npm install
npm run dev
```
*The web interface will be running at `http://localhost:5173`.*

---

## API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the web application interface |
| `GET` | `/health` | Service health status check |
| `GET` | `/api/config` | Public client configuration |
| `GET` | `/api/search?q={query}` | Search music catalog for tracks and albums |
| `GET` | `/api/spotify/search?q={query}` | Search Spotify metadata |
| `POST` | `/api/resolve` | Resolve stream URLs and track playback information |
| `POST` | `/api/download` | Download lossless FLAC audio with embedded tags |

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


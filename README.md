# Amazon Music API & Downloader

A lightweight FastAPI server wrapper built around the `amzdl` catalog search, metadata resolution, and lossless audio download library. 

This service allows resolving tracks, searching the catalog, and downloading fully tagged, lossless FLAC files directly to your storage.

## Features

- **Metadata Resolution**: Resolve track details, codec configurations, and stream URLs.
- **Search Catalog**: Query the Amazon Music catalog directly.
- **Lossless Downloads**: Download tracks in CD quality (FLAC) with fully embedded metadata, cover art, and synchronized lyrics.
- **Embedded Synced Lyrics**: Synchronized lyrics (LRC format) are embedded directly within the FLAC metadata (`LYRICS` and `UNSYNCEDLYRICS` tags) rather than writing external sidecar files.
- **Lazy Initialization**: Server starts up instantly even if credentials are missing, allowing health checks to run.

---

## Documentation

For setup instructions, endpoint details, and request/response structures, refer to the detailed documentation:

👉 **[Detailed Documentation](./docs.md)**

---

## Quick Start

### 1. Requirements
Ensure you have Python 3.10+ and the required dependencies installed:
```bash
pip install -r downloader/src/amazonmusic/requirements.txt
pip install fastapi uvicorn httpx xmltodict mutagen
```

### 2. Device Registration & Authentication
The application requires device authentication with Amazon Music. Follow these steps to register your device and generate the required credentials:

1. **Run the account registration CLI**:
   ```bash
   python downloader/src/amzdl/main.py accounts --add
   ```
2. **Select Region**: Choose your region when prompted (e.g., `US`, `IN`, `GB`).
3. **Browser Login**: The CLI will generate an OAuth login link. Copy and open this link in your web browser.
4. **Authorize**: Sign in with your Amazon Music account and authorize the application.
5. **Paste Callback URL**: After authorizing, you will be redirected to a blank page or a redirect URL. Copy the full URL from your browser's address bar, paste it back into the CLI prompt, and press Enter.
6. **Persistence**: This registers your device and automatically generates the `credentials.bin` file inside the `config/` directory.

> [!NOTE]
> Alternatively, the `.env` file in the root directory can be populated with your registered customer ID, token, serial, cookies, and RSA private key to manually declare credentials if running in environments without the `config/credentials.bin` file.

### 3. Run the Server
Launch the server using Uvicorn:
```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

### 4. Run the Frontend

In a second terminal, install the frontend dependencies and launch Vite:

```bash
npm --prefix frontend install
npm --prefix frontend run dev
```

Open `http://localhost:5173`. The frontend connects to
`http://127.0.0.1:8000` by default; the API address can be changed from the
in-app Settings dialog.



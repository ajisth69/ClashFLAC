# Account Setup & Engine Configuration Guide

This guide walks you through registering accounts and configuring credentials for **Amazon Music**, **Qobuz**, and **TIDAL**.

---

## 1. Prerequisites

- Python 3.10+ installed.
- Install project packages:
  ```bash
  pip install -r requirements.txt
  pip install -e .
  ```

---

## 2. Amazon Music Account Registration

### Step 1: Launch Registration CLI
```bash
python amzdl/main.py accounts --add
```

### Step 2: Select Geographic Region
Choose your Amazon account region:
- `US` &mdash; United States
- `IN` &mdash; India
- `GB` &mdash; United Kingdom
- `DE` &mdash; Germany
- `JP` &mdash; Japan

### Step 3: Authorize in Browser
1. Copy the displayed URL into your browser.
2. Log in and approve access.
3. Copy the full redirect URL from your browser's address bar and paste it back into the CLI prompt.

### Step 4: Credential Persistence
Saved locally to `config/credentials.bin` or `~/.config/amzdl/credentials.bin`.

---

## 3. Qobuz Account Configuration

Qobuz provides direct bit-perfect FLAC audio streams (up to 24-bit / 192 kHz Studio Master). ClashFLAC always requests the highest available studio resolution from Qobuz.

### Method A: Environment Variables (`.env`)
Add your Qobuz application and user credentials directly to `.env`:

```env
QOBUZ_APP_ID="950096963"
QOBUZ_APP_SECRET="979549437fcc4a3faad4867b5cd25dcb"
QOBUZ_USER_AUTH_TOKEN="<USER_AUTH_TOKEN>"
QOBUZ_USER_ID="<USER_ID>"
```

### Method B: Token File (`config/qobuz_tokens.json`)
Save your credentials in `config/qobuz_tokens.json`:

```json
{
  "app_id": "950096963",
  "app_secret": "979549437fcc4a3faad4867b5cd25dcb",
  "user_auth_token": "<USER_AUTH_TOKEN>",
  "user_id": "<USER_ID>"
}
```

### Method C: Cloud Deployment (Base64 String)
Export your Qobuz tokens to a single environment variable:
```bash
python -c "import base64, pathlib; p = pathlib.Path('config/qobuz_tokens.json'); print(base64.b64encode(p.read_bytes()).decode() if p.exists() else '')"
```
- **Environment Variable:** `QOBUZ_CREDENTIALS_BASE64`

---

## 4. TIDAL Account Registration

TIDAL can be setup either via HTTP API endpoints or via the `tidal-dl` CLI.

### Method A: Via API Endpoints (Frontend / Postman)

1. **Initiate Device Code:**
   - `POST /api/tidal/auth/device`
   - Response returns `user_code` and `verification_uri_complete` (e.g. `https://link.tidal.com/XXXXX`).
2. **Authorize in Browser:**
   - Open `https://link.tidal.com/XXXXX` and approve access.
3. **Extract & Save Credentials:**
   - `POST /api/tidal/auth/check` with `{"device_code": "..."}`
   - Tokens are automatically saved to `config/tidal_tokens.json`.

*(Alternatively, import pre-existing tokens directly using `POST /api/tidal/auth/set_token`).*

### Method B: Via CLI Account Manager

```bash
# Check current TIDAL account status
tidal-dl accounts --list

# Register / Login via device code (link.tidal.com)
tidal-dl accounts --add

# Remove / Logout account
tidal-dl accounts --remove

# Export session string for cloud deployment
tidal-dl accounts --export

# Manually set access token
tidal-dl accounts --token "<ACCESS_TOKEN>" --refresh-token "<REFRESH_TOKEN>"
```

---

## 5. Cloud Deployment Environment Variables

For production deployments (e.g. Railway / Docker) without persistent local files, configure the following environment variables:

| Environment Variable | Description |
|---|---|
| `AMZ_CREDENTIALS_BASE64` | Base64-encoded `credentials.bin` for Amazon Music |
| `QOBUZ_CREDENTIALS_BASE64` | Base64-encoded `qobuz_tokens.json` for Qobuz Hi-Res Lossless |
| `TIDAL_CREDENTIALS_BASE64` | Base64-encoded `tidal_tokens.json` for Tidal Master Lossless |
| `CLOUDFLARE_SITE_KEY` | Cloudflare Turnstile public site key |
| `CLOUDFLARE_SECRET_KEY` | Cloudflare Turnstile secret key |

---

## 6. Starting the Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

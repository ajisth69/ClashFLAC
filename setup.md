# Account Setup & Device Registration Guide

This guide walks you through registering your accounts and generating the necessary credentials for **Amazon Music** and **TIDAL**.

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

## 3. TIDAL Account Registration

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

## 4. Cloud Deployment (Railway / Docker)

For production deployments without local files, export credentials as Base64 environment variables:

### Amazon Music Credential String:
```bash
python -c "import base64, pathlib; p = pathlib.Path('config/credentials.bin'); print(base64.b64encode(p.read_bytes()).decode() if p.exists() else '')"
```
- **Environment Variable:** `AMZ_CREDENTIALS_BASE64`

### TIDAL Credential String:
```bash
python -c "import base64, pathlib; p = pathlib.Path('config/tidal_tokens.json'); print(base64.b64encode(p.read_bytes()).decode() if p.exists() else '')"
```
- **Environment Variable:** `TIDAL_CREDENTIALS_BASE64`

---

## 5. Starting the Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

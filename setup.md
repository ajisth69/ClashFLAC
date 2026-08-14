# Account Setup & Device Registration Guide

This guide walks you through registering your Amazon Music account and generating the necessary credentials for **ClashFLAC**.

---

## Prerequisites

- An active **Amazon Music** account (Free, Prime, or Unlimited).
- Python 3.10+ installed with project dependencies:
  ```bash
  pip install -r requirements.txt
  ```

---

## Step-by-Step Device Registration

### 1. Launch the Registration CLI
Run the account management tool from the project root:
```bash
python amzdl/main.py accounts --add
```

### 2. Select Your Account Region
When prompted, select the geographic region matching your Amazon account:
- `US` &mdash; United States
- `IN` &mdash; India
- `GB` &mdash; United Kingdom
- `DE` &mdash; Germany
- `JP` &mdash; Japan
- *(or other available regions)*

### 3. Open the Authorization Link
The CLI will output an Amazon OAuth login URL. 
1. Copy the URL and open it in your web browser.
2. Sign in with your Amazon account credentials.
3. Authorize the device when prompted.

### 4. Paste the Redirection URL
After completing login, your browser will be redirected to a redirect page (or a blank page):
1. Copy the **entire URL** from your browser's address bar (including `https://...` and all query parameters).
2. Return to your terminal, paste the copied URL into the prompt, and press **Enter**.

### 5. Confirmation
The tool will finalize authentication, generate cryptographic device keys, and output confirmation of your registered account name and region.

---

## Credential Persistence

Once registration succeeds, your session is saved locally in:
- `~/.config/amzdl/credentials.bin` (or `config/credentials.bin`)

The backend automatically loads this file when running locally on your machine.

---

## Cloud Deployment (Railway / Docker)

Because binary credential files are excluded from git for security, export your session to a single environment variable when deploying to cloud hosts:

### 1. Generate Base64 Session String
Run this command in your terminal:
```bash
python -c "import base64, pathlib; p = pathlib.Path.home() / '.config/amzdl/credentials.bin'; print(base64.b64encode(p.read_bytes()).decode() if p.exists() else base64.b64encode(pathlib.Path('config/credentials.bin').read_bytes()).decode())"
```

### 2. Add to Cloud Environment Variables
Copy the resulting output string and add it to your cloud platform's environment variables:
- **Variable Name:** `AMZ_CREDENTIALS_BASE64`
- **Value:** *(Pasted base64 string)*

The server will automatically restore the credential session into memory on startup.

---

## Managing Accounts

### List Registered Accounts
```bash
python amzdl/main.py accounts --list
```

### Remove an Account
```bash
python amzdl/main.py accounts --remove <customer_id>
```

#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
"""
Tidal-DL CLI - Command Line Interface & Account Management
Modeled after yaronzz/Tidal-Media-Downloader and amzdl
"""

import os
import sys
import time
import getopt
import asyncio
import argparse
from pathlib import Path
from typing import Optional

# Ensure project root is in PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from tidal.config import TidalConfig
from tidal.auth import TidalAuth
from tidal.api import TidalAPI
from tidal.downloader import TidalDownloader

VERSION = "2026.1.0"

class TidalDLCLI:
    def __init__(self):
        self.auth = TidalAuth()
        self.api = TidalAPI(auth=self.auth)
        self.downloader = TidalDownloader(api=self.api)
        self.quality = "HD"
        self.download_path = Path("downloads/tidal")

    def print_banner(self):
        print(r"""
  _______ _     _       _         _____  _      
 |__   __(_)   | |     | |       |  __ \| |     
    | |   _  __| | __ _| |______ | |  | | |     
    | |  | |/ _` |/ _` | |______|| |  | | |     
    | |  | | (_| | (_| | |       | |__| | |____ 
    |_|  |_|\__,_|\__,_|_|       |_____/|______|
        Tidal Media Downloader (v""" + VERSION + r""")
""")
        print("=" * 60)
        print(f" [Settings]")
        print(f"   Download Path: {self.download_path.resolve()}")
        print(f"   Audio Quality: {self.quality} (LOSSLESS/FLAC)")
        auth_status = f"Logged In (ID: {self.auth.user_id}, {self.auth.country_code})" if self.auth.is_authenticated() else "Guest Mode (Client Credentials)"
        print(f"   Account:       {auth_status}")
        print("=" * 60)

    def list_accounts(self):
        print("=" * 60)
        print("              TIDAL Account Status")
        print("=" * 60)
        if self.auth.is_authenticated():
            exp_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.auth.token_expiry)) if self.auth.token_expiry else "Unknown"
            print(f" Status:       AUTHENTICATED")
            print(f" User ID:      {self.auth.user_id}")
            print(f" Country Code: {self.auth.country_code}")
            print(f" Token Expiry: {exp_str}")
            print(f" Storage Path: {TidalConfig.TOKEN_FILE.resolve()}")
        else:
            print(f" Status:       NOT AUTHENTICATED (Guest Web / Client Credentials Mode)")
            print(f" Mode:         Catalog search & public metadata enabled.")
            print(f"               Run `tidal-dl accounts --add` to link an account.")
        print("=" * 60)

    def export_account(self):
        b64 = self.auth.get_credentials_base64()
        if not b64:
            print("[ERROR] No authenticated Tidal session to export.")
            return
        print("\n--- TIDAL Cloud Environment Variable ---")
        print(f"TIDAL_CREDENTIALS_BASE64={b64}\n")

    def remove_account(self):
        self.auth.clear_tokens()
        print("[SUCCESS] Tidal account credentials removed successfully.")

    def set_token(self, access_token: str, refresh_token: Optional[str] = None, user_id: Optional[str] = None, country_code: str = "US"):
        token_record = {
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip() if refresh_token else None,
            "user_id": user_id or "user",
            "country_code": country_code or "US",
            "expires_in": 86400 * 30,
            "token_expiry": time.time() + 86400 * 30,
        }
        self.auth.save_tokens(token_record)
        print(f"[SUCCESS] Saved Tidal tokens for user '{self.auth.user_id}' ({self.auth.country_code}).")

    async def login_flow(self):
        print("\n--- TIDAL Device Authorization ---")
        try:
            init_res = await self.auth.init_device_authorization()
            verify_url = init_res["verification_uri_complete"]
            if not verify_url.startswith("http"):
                verify_url = f"https://{verify_url}"

            print(f"\n1. Open link in your browser:")
            print(f"   --> {verify_url}")
            print(f"2. Confirm code: {init_res['user_code']}")
            print("\nWaiting for browser approval (polling every 5s)...")

            device_code = init_res["device_code"]
            interval = init_res.get("interval", 5)
            expires_in = init_res.get("expires_in", 300)

            for _ in range(int(expires_in / interval)):
                await asyncio.sleep(interval)
                check = await self.auth.check_device_token(device_code)
                if check.get("authenticated"):
                    print("\n\n[SUCCESS] Successfully logged in to TIDAL!")
                    print(f"User ID:      {check.get('user_id')}")
                    print(f"Country Code: {check.get('country_code')}")
                    print(f"Saved To:     {TidalConfig.TOKEN_FILE.resolve()}")
                    break
                elif check.get("status") == "expired":
                    print("\n[ERROR] Device code expired.")
                    break
                else:
                    print(".", end="", flush=True)
        except Exception as e:
            print(f"\n[ERROR] Login failed: {e}")

    def change_settings(self):
        print("\n--- Change Settings ---")
        print("1. Audio Quality")
        print("   [1] Normal (96kbps AAC)")
        print("   [2] High (320kbps AAC)")
        print("   [3] HiFi / HD (16-bit 44.1kHz FLAC)")
        print("   [4] Master / UHD (24-bit Hi-Res FLAC)")
        choice = input("Select quality [1-4] (default 3): ").strip()
        q_map = {"1": "LOW", "2": "HIGH", "3": "HD", "4": "UHD"}
        if choice in q_map:
            self.quality = q_map[choice]
            print(f"Audio Quality set to: {self.quality}")

        custom_path = input(f"\nDownload Path [{self.download_path}]: ").strip()
        if custom_path:
            self.download_path = Path(custom_path)
            print(f"Download Path set to: {self.download_path}")

    async def download_item(self, input_str: str):
        input_str = input_str.strip()
        if not input_str:
            return
        print(f"\nResolving and downloading: {input_str} (quality: {self.quality})...")
        try:
            file_path = await self.downloader.download_track(
                input_str,
                output_dir=self.download_path,
                quality=self.quality
            )
            print(f"\n[SUCCESS] Saved to: {file_path}")
            print(f"Size: {file_path.stat().st_size / (1024 * 1024):.2f} MB")
        except Exception as e:
            print(f"\n[ERROR] Download failed: {e}")

    async def interactive_menu(self):
        while True:
            self.print_banner()
            print("[0] Exit")
            print("[1] Account Setup / Device Login")
            print("[2] Settings (Quality, Download Path)")
            print("[3] Download Track / Album / URL")
            print("[4] Search Catalog")
            print("[5] Export Credentials (Base64)")
            print("=" * 60)

            choice = input("Enter choice [0-5]: ").strip()
            if choice == "0":
                print("Goodbye!")
                break
            elif choice == "1":
                await self.login_flow()
            elif choice == "2":
                self.change_settings()
            elif choice == "3":
                url = input("\nEnter TIDAL Track ID or URL: ").strip()
                if url:
                    await self.download_item(url)
            elif choice == "4":
                q = input("\nEnter search query: ").strip()
                if q:
                    results = await self.api.search(q, limit=5)
                    print(f"\n--- Search Results ({len(results)}) ---")
                    for i, r in enumerate(results, 1):
                        print(f"{i}. [{r.asin}] {r.title} - {r.artist} ({r.album}) [{r.duration_sec}s]")
            elif choice == "5":
                self.export_account()
            else:
                print("Invalid selection.")
            
            input("\nPress Enter to continue...")

async def async_main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    cli = TidalDLCLI()

    # Subcommand: accounts (matching amzdl accounts pattern)
    if argv and argv[0] == "accounts":
        acc_parser = argparse.ArgumentParser(prog="tidal-dl accounts", description="Manage TIDAL accounts")
        acc_parser.add_argument("--list", "-l", action="store_true", help="List current account status")
        acc_parser.add_argument("--add", "-a", action="store_true", help="Register a new TIDAL account via device code")
        acc_parser.add_argument("--remove", "-r", action="store_true", help="Remove stored account credentials")
        acc_parser.add_argument("--export", "-e", action="store_true", help="Export session string (TIDAL_CREDENTIALS_BASE64)")
        acc_parser.add_argument("--token", type=str, help="Directly set access token")
        acc_parser.add_argument("--refresh-token", type=str, help="Refresh token (optional)")
        acc_parser.add_argument("--user-id", type=str, help="User ID (optional)")
        acc_parser.add_argument("--country", type=str, default="US", help="Country code (default US)")

        acc_args = acc_parser.parse_args(argv[1:])

        if acc_args.token:
            cli.set_token(acc_args.token, acc_args.refresh_token, acc_args.user_id, acc_args.country)
            return
        if acc_args.add:
            await cli.login_flow()
            return
        if acc_args.remove:
            cli.remove_account()
            return
        if acc_args.export:
            cli.export_account()
            return
        # Default action for accounts
        cli.list_accounts()
        return

    # Standard options
    try:
        opts, args = getopt.getopt(argv, "hl:q:o:s:v", ["help", "link=", "quality=", "output=", "search=", "version", "login"])
    except getopt.GetoptError as err:
        print(f"Error: {err}. Use -h for help.")
        return

    link = None
    search_q = None

    for opt, val in opts:
        if opt in ("-h", "--help"):
            print("Usage: tidal-dl [command] [options]")
            print("\nCommands:")
            print("  accounts --list         List current account status")
            print("  accounts --add          Register a new account via device code")
            print("  accounts --remove       Remove account credentials")
            print("  accounts --export       Export credentials as base64 string")
            print("\nOptions:")
            print("  -h, --help              Show this help message")
            print("  -l, --link              Tidal track ID or URL to download")
            print("  -q, --quality           Audio quality: LOW, HIGH, HD (HiFi), UHD (Master)")
            print("  -o, --output            Download output folder path")
            print("  -s, --search            Search catalog query")
            print("  --login                 Start device login authorization")
            print("  -v, --version           Show version")
            return
        if opt in ("-v", "--version"):
            print(f"tidal-dl version {VERSION}")
            return
        if opt in ("-l", "--link"):
            link = val
        if opt in ("-q", "--quality"):
            cli.quality = val.upper()
        if opt in ("-o", "--output"):
            cli.download_path = Path(val)
        if opt in ("-s", "--search"):
            search_q = val
        if opt == "--login":
            await cli.login_flow()
            return

    if link:
        await cli.download_item(link)
        return

    if search_q:
        results = await cli.api.search(search_q, limit=5)
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r.asin}] {r.title} - {r.artist} ({r.album}) [{r.duration_sec}s]")
        return

    # If no flags provided, launch interactive menu
    await cli.interactive_menu()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()

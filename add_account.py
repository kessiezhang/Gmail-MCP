#!/usr/bin/env python3
"""
One-time OAuth onboarding for ghub-lite.

Usage:
    python add_account.py <account_id> /path/to/credentials.json

Where credentials.json is the OAuth client secret JSON downloaded from
Google Cloud Console (Desktop application type).

This script will:
  1. Open a browser for you to sign in to the Gmail account
  2. Save the access/refresh token under ~/.ghub-lite/accounts/<account_id>/token.json
  3. Register the account in ~/.ghub-lite/accounts.json

After this you can run server.py and the account will appear in list_accounts.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

# Google sometimes returns extra scopes (e.g. openid) on the token exchange.
# Tell oauthlib to tolerate that instead of raising.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def config_dir() -> Path:
    raw = os.environ.get("GHUB_LITE_CONFIG_DIR", "~/.ghub-lite")
    return Path(os.path.expanduser(raw))


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python add_account.py <account_id> /path/to/credentials.json")
        return 2

    account_id = sys.argv[1]
    cred_src = Path(sys.argv[2]).expanduser().resolve()
    if not cred_src.exists():
        print(f"Error: credentials file not found at {cred_src}")
        return 1

    if not account_id.replace("-", "").replace("_", "").isalnum():
        print("Error: account_id must be alphanumeric (dashes/underscores allowed).")
        return 1

    base = config_dir()
    acc_dir = base / "accounts" / account_id
    acc_dir.mkdir(parents=True, exist_ok=True)

    cred_dst = acc_dir / "credentials.json"
    if cred_src.resolve() != cred_dst.resolve():
        shutil.copy2(cred_src, cred_dst)

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_dst), SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message="Opening browser to authorize Gmail access...",
        success_message="Authorization complete. You can close this tab.",
    )

    token_path = acc_dir / "token.json"
    token_path.write_text(creds.to_json())

    # Look up the account's email so list_accounts shows it correctly.
    email = ""
    try:
        oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = oauth2.userinfo().get().execute()
        email = info.get("email", "")
    except Exception as exc:
        print(f"(warning) couldn't fetch user email: {exc}")

    # Register / update the account in accounts.json
    accounts_file = base / "accounts.json"
    if accounts_file.exists():
        cfg = json.loads(accounts_file.read_text())
    else:
        cfg = {"defaultAccount": account_id, "accounts": []}

    accounts: list[dict] = cfg.get("accounts", [])
    entry = {
        "id": account_id,
        "email": email,
        "enabled": True,
        "credentials_path": str(cred_dst),
        "token_path": str(token_path),
    }
    replaced = False
    for i, existing in enumerate(accounts):
        if existing.get("id") == account_id:
            accounts[i] = entry
            replaced = True
            break
    if not replaced:
        accounts.append(entry)
    cfg["accounts"] = accounts
    if not cfg.get("defaultAccount"):
        cfg["defaultAccount"] = account_id

    accounts_file.write_text(json.dumps(cfg, indent=2))

    print()
    print(f"Account '{account_id}' ({email}) saved.")
    print(f"  config:      {accounts_file}")
    print(f"  token file:  {token_path}")
    print()
    print("Next: add server.py to your MCP client config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

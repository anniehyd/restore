"""One-time OAuth authorization for Restore's Google Calendar access.

Run once from the project root:

    python scripts/authorize.py

It opens a browser, asks you to sign in and grant access, then writes the
refresh token to token.json. calendar_client.py picks it up automatically and
refreshes it as needed. Re-run this if token.json is deleted or the grant
lapses (Testing-mode tokens expire after 7 days).

Reads the OAuth client-secrets file from GOOGLE_CREDENTIALS_PATH
(default: google_credentials.json).
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Keep these in lockstep with app/calendar_client.py.
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")


def main() -> int:
    if not os.path.exists(CREDENTIALS_PATH):
        print(
            f"ERROR: OAuth client secrets not found at '{CREDENTIALS_PATH}'.\n"
            f"Download it from Google Cloud Console (Credentials -> your Desktop "
            f"OAuth client -> Download JSON) and save it there, or set "
            f"GOOGLE_CREDENTIALS_PATH.",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    # port=0 picks a free localhost port for the redirect; opens the browser.
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w") as fh:
        fh.write(creds.to_json())

    print(f"Success. Refresh token saved to '{TOKEN_PATH}'. You're authorized.")
    print("\n# For deployment, set these as secrets (DO NOT commit them):")
    print(f"GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

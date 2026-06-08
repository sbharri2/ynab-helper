"""Refresh Gmail OAuth for sbharri2@gmail.com.

The OpenClaw OAuth app is in Google "Testing" mode with restricted scopes,
so refresh tokens die every ~7 days. Rerun this whenever the bot starts
logging `invalid_grant: Token has been expired or revoked.`

Uses the client_id / client_secret embedded in the existing token JSON
(no separate client_secrets.json needed). Pops a browser window — sign in
as sbharri2@gmail.com and approve the requested scopes, then come back.

Run:
    .venv\\Scripts\\python.exe -m scripts.reauth_gmail
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from google_auth_oauthlib.flow import InstalledAppFlow

TOKEN_OUT = os.path.expanduser(
    r"~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json"
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def main() -> int:
    if not os.path.exists(TOKEN_OUT):
        print(f"ERROR: existing token file not found at {TOKEN_OUT}")
        print("Need it for client_id/client_secret. Get a fresh token from"
              " the OpenClaw OAuth client console first.")
        return 1

    with open(TOKEN_OUT) as f:
        old = json.load(f)

    if not old.get("client_id") or not old.get("client_secret"):
        print("ERROR: existing token doesn't have client_id/client_secret"
              " embedded — can't do the inline flow.")
        return 1

    client_config = {
        "installed": {
            "client_id": old["client_id"],
            "client_secret": old["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("Starting OAuth flow — a browser window will open.")
    print("Sign in as sbharri2@gmail.com and approve the requested scopes.\n")

    # google-auth-oauthlib wants a file path, so write to a tempfile.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8",
    ) as tmp:
        json.dump(client_config, tmp)
        tmp_path = tmp.name

    try:
        flow = InstalledAppFlow.from_client_secrets_file(tmp_path, SCOPES)
        # port=0 picks a random free port (avoids workspace-mcp 8000 conflict)
        creds = flow.run_local_server(port=0)
    finally:
        os.unlink(tmp_path)

    with open(TOKEN_OUT, "w") as f:
        f.write(creds.to_json())

    print(f"\nToken written to: {TOKEN_OUT}")
    print(f"Has refresh token: {bool(creds.refresh_token)}")
    print("Bot should pick this up on its next gmail poll (~60s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

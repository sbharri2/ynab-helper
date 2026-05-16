"""
Refresh Gmail OAuth for sbharri2@gmail.com.

The existing token at ~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json
has a revoked refresh token. This script kicks off a new OAuth flow using the
desktop client credentials from email-triage, on a random free port (to avoid
the port 8000 conflict with the workspace-mcp server).

Will pop a browser window — approve, then come back.

Run:
    python scripts/reauth_gmail.py
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRETS = r"C:\Users\SHarris\_ClaudeProjects\skills\email-triage\gcp-oauth-credentials.json"
TOKEN_OUT = os.path.expanduser(
    r"~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json"
)

# Scopes that cover everything ynab-helper needs (read + modify for label tagging)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]

def main():
    if not os.path.exists(CLIENT_SECRETS):
        print(f"ERROR: client secrets not found at {CLIENT_SECRETS}")
        sys.exit(1)

    print("Starting OAuth flow — a browser window will open.")
    print("Sign in as sbharri2@gmail.com and approve the requested scopes.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
    creds = flow.run_local_server(port=0)  # random free port

    os.makedirs(os.path.dirname(TOKEN_OUT), exist_ok=True)
    with open(TOKEN_OUT, "w") as f:
        f.write(creds.to_json())

    print(f"\nToken written to: {TOKEN_OUT}")
    print("Refresh-able:", bool(creds.refresh_token))


if __name__ == "__main__":
    main()

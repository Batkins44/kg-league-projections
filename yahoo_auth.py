#!/usr/bin/env python3
"""One-time helper: turn a Yahoo app into a refresh token for the snapshot workflow.

Run it on your own machine, never in Actions:
  python yahoo_auth.py CLIENT_ID CLIENT_SECRET

It prints a Yahoo link. Open it, approve, paste back the code Yahoo shows, and it prints
the refresh token to store as the YAHOO_REFRESH_TOKEN repo secret. Nothing is written to disk.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.parse
import urllib.request

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    client_id, secret = sys.argv[1], sys.argv[2]
    q = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": "oob",
                                "response_type": "code", "language": "en-us"})
    print(f"Open this, approve, then paste the code:\n\n  {AUTH_URL}?{q}\n")
    code = input("code: ").strip()
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                   "redirect_uri": "oob"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        tok = json.load(r)
    print("\nYAHOO_REFRESH_TOKEN =", tok["refresh_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

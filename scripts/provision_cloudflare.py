#!/usr/bin/env python3
"""Create the D1 database idempotently and inject its ID into Wrangler config."""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def api_request(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Cloudflare API returned HTTP {exc.code}: {detail}") from exc
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API request failed: {body.get('errors')}")
    return body.get("result")


def main():
    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    token = os.environ["CLOUDFLARE_API_TOKEN"].strip()
    name = "vanta-discord"
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
        raise SystemExit("CLOUDFLARE_ACCOUNT_ID must be a 32-character hexadecimal account ID")
    if not token:
        raise SystemExit("CLOUDFLARE_API_TOKEN is required")

    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database"
    databases = api_request("GET", endpoint, token)
    database = next((item for item in databases if item.get("name") == name), None)
    if database is None:
        database = api_request("POST", endpoint, token, {"name": name})
        print(f"Created Cloudflare D1 database: {name}")
    else:
        print(f"Using existing Cloudflare D1 database: {name}")

    database_id = database.get("uuid")
    if not database_id or not re.fullmatch(r"[a-fA-F0-9-]{36}", database_id):
        raise RuntimeError("Cloudflare did not return a valid D1 database UUID")

    config = Path("cloudflare-worker/wrangler.toml")
    contents = config.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?m)^database_id\s*=\s*"[^"]+"$',
        f'database_id = "{database_id}"',
        contents,
    )
    if count != 1:
        raise RuntimeError("Expected exactly one database_id in cloudflare-worker/wrangler.toml")
    config.write_text(updated, encoding="utf-8")
    print(f"Configured D1 database ID: {database_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Cloudflare provisioning failed: {error}", file=sys.stderr)
        raise SystemExit(1)

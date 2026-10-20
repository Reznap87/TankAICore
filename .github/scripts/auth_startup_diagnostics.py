"""Read only TankAI container state and recent startup messages.

Uses the production account's existing connection without printing credentials,
request headers, request bodies, environment bindings, or account identifiers.
"""
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
token = os.environ["CLOUDFLARE_API_TOKEN"]
base = f"https://api.cloudflare.com/client/v4/accounts/{account}"


def read(path, payload=None):
    request = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        print("Diagnostic API status:", error.code)
        return {}


instances = read("/containers/applications/a0378ad1-191f-4114-ab2f-9c2c906484a0/instances-v2?per_page=10")
for item in instances.get("result", []):
    print(json.dumps({"instance_status": item.get("status"), "started_at": item.get("started_at")}))
now = int(time.time() * 1000)
result = read("/workers/observability/telemetry/query", {
    "queryId": "tankai-auth-startup", "view": "events", "limit": 100,
    "timeframe": {"from": now - 20 * 60 * 1000, "to": now},
    "parameters": {"filters": [{"key": "$metadata.service", "operation": "starts_with", "type": "string", "value": "tankai-core"}]},
})


def messages(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"message", "$metadata.message", "$metadata.error"} and isinstance(item, str):
                clean = item.replace(token, "[credential]").replace(account, "[account]")
                clean = re.sub(r"scrypt\$\S+|Bearer\s+\S+", "[credential]", clean)
                print(clean[:2500])
            elif key not in {"headers", "body", "env", "bindings", "request"}:
                messages(item)
    elif isinstance(value, list):
        for item in value:
            messages(item)


messages(result.get("result", {}).get("events", {}))
print("Auth startup diagnostics complete (read only).")

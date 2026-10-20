"""Read only TankAI container state and recent startup messages.

Uses the production account's existing connection without printing credentials,
request headers, request bodies, environment bindings, or account identifiers.
"""
import json
import os
import re
import signal
import subprocess
import threading
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
            if key in {"message", "$metadata.message", "$metadata.error"} and isinstance(item, (str, list)):
                text = item if isinstance(item, str) else " ".join(str(part) for part in item)
                clean = text.replace(token, "[credential]").replace(account, "[account]")
                clean = re.sub(r"scrypt\$\S+|Bearer\s+\S+", "[credential]", clean)
                print(clean[:2500])
            elif key not in {"headers", "body", "env", "bindings", "request"}:
                messages(item)
    elif isinstance(value, list):
        for item in value:
            messages(item)


messages(result.get("result", {}).get("events", {}))

# Older production tokens can allow live tailing without the newer stored-log
# query permission. This is still read only and scoped to the same Worker.
def probe():
    time.sleep(5)
    try:
        with urlopen(Request("https://tankaicore.com/api/health", headers={"User-Agent": "TankAI-Readiness/1"}), timeout=25) as response:
            print("Readiness probe:", response.status)
    except HTTPError as error:
        print("Readiness probe:", error.code)
    except Exception:
        print("Readiness probe: unavailable")


tail = subprocess.Popen(["npx", "--no-install", "wrangler", "tail", "tankai-core", "--format", "json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
thread = threading.Thread(target=probe, daemon=True)
thread.start()
try:
    output, errors = tail.communicate(timeout=40)
except subprocess.TimeoutExpired:
    os.killpg(tail.pid, signal.SIGTERM)
    try:
        output, errors = tail.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(tail.pid, signal.SIGKILL)
        output, errors = tail.communicate()
decoder = json.JSONDecoder()
while output.strip():
    start = output.find("{")
    if start < 0:
        break
    output = output[start:]
    try:
        event, end = decoder.raw_decode(output)
    except ValueError:
        output = output[1:]
        continue
    messages(event.get("logs", []))
    messages(event.get("exceptions", []))
    output = output[end:]
if errors:
    print(errors.replace(token, "[credential]").replace(account, "[account]")[-1500:])
print("Auth startup diagnostics complete (read only).")

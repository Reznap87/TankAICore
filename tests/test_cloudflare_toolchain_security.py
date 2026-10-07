from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cloudflare_toolchain_uses_audited_exact_versions() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]

    assert package["devDependencies"]["wrangler"] == "4.145.0"
    assert packages[""]["devDependencies"]["wrangler"] == "4.145.0"
    assert packages["node_modules/wrangler"]["version"] == "4.145.0"
    assert packages["node_modules/miniflare"]["version"] == "5.20260930.0-alpha"
    assert package["overrides"]["sharp"] == "0.35.5"
    assert packages["node_modules/sharp"]["version"] == "0.35.5"
    assert packages["node_modules/undici"]["version"] == "7.29.1"

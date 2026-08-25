from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "production-preflight.yml"


def test_production_preflight_is_manual_read_only_and_environment_scoped() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  workflow_dispatch:\n" in text
    assert "\n  push:" not in text
    assert "permissions:\n  contents: read" in text
    assert "environment: production" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in text
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}" in text
    assert "npm ci --ignore-scripts" in text
    assert "npx --no-install wrangler containers list >/dev/null" in text
    assert "deploy" not in text.lower()

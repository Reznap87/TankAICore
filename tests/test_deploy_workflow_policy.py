from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-cloudflare.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_production_deploy_has_no_automatic_push_trigger() -> None:
    text = _workflow_text()
    assert "\n  push:" not in text
    assert "\n  workflow_dispatch:\n" in text


def test_production_deploy_requires_explicit_confirmation_on_main() -> None:
    text = _workflow_text()
    assert "confirm_production:" in text
    assert "required: true" in text
    assert "- CANCEL" in text
    assert "- DEPLOY" in text
    assert (
        "if: ${{ github.ref == 'refs/heads/main' && "
        "inputs.confirm_production == 'DEPLOY' }}" in text
    )


def test_production_deploy_remains_serial_and_environment_scoped() -> None:
    text = _workflow_text()
    assert "permissions:\n  contents: read" in text
    assert "group: cloudflare-production" in text
    assert "cancel-in-progress: false" in text
    assert "environment: production" in text
    assert "apiToken: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in text
    assert "accountId: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}" in text

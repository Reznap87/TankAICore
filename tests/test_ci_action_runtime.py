from pathlib import Path


CI_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"

CHECKOUT_V7 = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0"
SETUP_PYTHON_V7 = (
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0"
)
SETUP_NODE_V7 = "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0"


def test_required_ci_uses_node24_compatible_action_releases() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert text.count(CHECKOUT_V7) == 2
    assert text.count(SETUP_PYTHON_V7) == 2
    assert text.count(SETUP_NODE_V7) == 1
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" not in text
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" not in text
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" not in text

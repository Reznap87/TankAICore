import re
from collections import Counter
from pathlib import Path


WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
USES_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)

NODE24_ACTION_PINS = {
    "actions/checkout": (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    ),
    "actions/setup-python": (
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
    ),
    "actions/setup-node": (
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
    ),
    "actions/upload-artifact": (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    ),
}
EXPECTED_COUNTS = Counter(
    {
        "actions/checkout": 6,
        "actions/setup-python": 3,
        "actions/setup-node": 3,
        "actions/upload-artifact": 1,
    }
)


def test_all_workflows_use_approved_node24_first_party_actions() -> None:
    found: Counter[str] = Counter()
    unexpected: list[str] = []

    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for action in USES_PATTERN.findall(workflow.read_text(encoding="utf-8")):
            slug = action.split("@", 1)[0]
            expected = NODE24_ACTION_PINS.get(slug)
            if expected is None:
                continue
            if action != expected:
                unexpected.append(f"{workflow.name}: {action}")
            else:
                found[slug] += 1

    assert not unexpected, "Unapproved first-party action pins:\n" + "\n".join(unexpected)
    assert found == EXPECTED_COUNTS

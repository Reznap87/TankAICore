import re
from pathlib import Path


WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
USES_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
PINNED_ACTION_PATTERN = re.compile(r"^[^/\s]+/[^@\s]+@[0-9a-f]{40}$")


def test_all_external_github_actions_are_pinned_to_full_commits() -> None:
    unpinned: list[str] = []

    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for action in USES_PATTERN.findall(workflow.read_text(encoding="utf-8")):
            if action.startswith("./") or action.startswith("docker://"):
                continue
            if not PINNED_ACTION_PATTERN.fullmatch(action):
                unpinned.append(f"{workflow.name}: {action}")

    assert not unpinned, "Unpinned GitHub Actions:\n" + "\n".join(unpinned)


def test_workflows_contain_external_actions_to_validate() -> None:
    actions = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        actions.extend(USES_PATTERN.findall(workflow.read_text(encoding="utf-8")))

    assert len(actions) == 14

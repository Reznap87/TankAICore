"""Regression for the actual Docker 29 stdout notice observed in the rehearsal."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("pilot_smoke", Path(__file__).resolve().parents[1]
                                             / ".github/scripts/pilot_container_smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
IMAGE = "sha256:" + "a" * 64


def test_docker_29_notice_is_not_part_of_the_image_reference():
    output = ("Flag --pause has been deprecated, and enabled by default. "
              "Use --no-pause to disable pausing during commit.\n" + IMAGE + "\n")
    assert smoke.committed_image_id(output) == IMAGE


@pytest.mark.parametrize("output", ["", "notice only\n", IMAGE + "\n" + IMAGE + "\n",
                                    "sha256:short\n", "repository:tag\n"])
def test_missing_or_ambiguous_commit_identity_is_rejected(output):
    with pytest.raises(RuntimeError, match="one immutable image ID"):
        smoke.committed_image_id(output)

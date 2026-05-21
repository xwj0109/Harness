from __future__ import annotations

import pytest

from harness.action_policy import decide_managed_action
from harness.action_router import ManagedActionDecisionStatus, route_managed_action
from harness.governance.protected_paths import (
    PROTECTED_APPLY_PATTERNS,
    is_protected_apply_path,
    protected_apply_path_match,
)
from harness.tools.patch import PatchValidationError, _is_blocked_edit_path, plan_unified_diff


@pytest.mark.parametrize(
    "path",
    [
        ".harness/governance/merge-check/verdict.json",
        ".git/config",
        "src/harness/governance/gate_registry.py",
        "src/harness/session_tools.py",
        "src/harness/builtin_specs/tool_policies.yaml",
        "docs/plans/toloclaw_governance_parity_plan.md",
    ],
)
def test_protected_apply_path_match_blocks_governance_paths(path: str) -> None:
    match = protected_apply_path_match(path)

    assert match is not None
    assert match.pattern in PROTECTED_APPLY_PATTERNS
    assert is_protected_apply_path(path) is True
    assert _is_blocked_edit_path(path) is True


def test_protected_apply_path_match_allows_normal_project_files() -> None:
    assert protected_apply_path_match("src/harness/ordinary_feature.py") is None
    assert is_protected_apply_path("README.md") is False


def test_managed_action_policy_uses_governance_protected_paths(tmp_path) -> None:
    route = route_managed_action("create an empty .md file").model_copy(
        update={"normalized_arguments": {"filename": "docs/plans/unsafe.md", "allowed_extensions": [".md"]}}
    )

    decision = decide_managed_action(route, tmp_path)

    assert decision.status == ManagedActionDecisionStatus.DENIED
    assert any("protected Harness governance path" in reason for reason in decision.reasons)


def test_patch_validation_uses_governance_protected_paths(tmp_path) -> None:
    target = tmp_path / "src" / "harness" / "session_tools.py"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    patch = """--- a/src/harness/session_tools.py
+++ b/src/harness/session_tools.py
@@ -1 +1 @@
-old
+new
"""

    with pytest.raises(PatchValidationError, match="Blocked edit path"):
        plan_unified_diff(patch, tmp_path, [])

from harness.models import DataBoundary, ToolReplayPolicy, ToolSideEffectLevel
from harness.tool_capabilities import get_tool_capability, list_tool_capabilities


def test_builtin_tool_capabilities_are_complete_sorted_and_unique() -> None:
    descriptors = list_tool_capabilities()
    ids = [descriptor.id for descriptor in descriptors]

    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert {
        "repo_read",
        "artifact_read",
        "artifact_write",
        "isolated_edit",
        "diff_inspect",
        "secret_scan",
        "docker_test",
        "policy_explain",
        "approval_request",
    } <= set(ids)
    assert {
        "generic_shell",
        "shell",
        "mcp",
        "a2a",
        "browser",
        "email",
        "calendar",
        "hosted_fallback",
        "paid_fallback",
        "network_exec",
    }.isdisjoint(ids)
    assert all(descriptor.schema_version == "harness.tool_capability/v1" for descriptor in descriptors)


def test_builtin_tool_capability_safety_metadata() -> None:
    by_id = {descriptor.id: descriptor for descriptor in list_tool_capabilities()}

    assert by_id["repo_read"].side_effect_level == ToolSideEffectLevel.NONE
    assert by_id["artifact_read"].side_effect_level == ToolSideEffectLevel.NONE
    assert by_id["policy_explain"].side_effect_level == ToolSideEffectLevel.NONE
    assert by_id["secret_scan"].side_effect_level == ToolSideEffectLevel.NONE
    assert by_id["artifact_write"].side_effect_level == ToolSideEffectLevel.ARTIFACT_WRITE
    assert by_id["isolated_edit"].side_effect_level == ToolSideEffectLevel.WORKSPACE_WRITE
    assert by_id["isolated_edit"].approval_required == ["hosted_provider"]
    assert by_id["isolated_edit"].sandbox_required is True
    assert by_id["docker_test"].sandbox_required is True
    assert "docker_execution" in by_id["docker_test"].approval_required
    assert by_id["approval_request"].replay_policy == ToolReplayPolicy.NOT_REPLAYABLE
    assert all(descriptor.data_boundary in DataBoundary for descriptor in by_id.values())


def test_get_tool_capability_unknown_id_has_stable_error() -> None:
    try:
        get_tool_capability("generic_shell")
    except KeyError as exc:
        assert str(exc).strip("'") == "Tool capability not found: generic_shell"
    else:
        raise AssertionError("unknown tool id should raise")

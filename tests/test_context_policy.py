from harness.context_policy import (
    CONTEXT_POLICY_HOSTED_DENIED,
    CONTEXT_POLICY_LOCAL_ALLOWED,
    CONTEXT_POLICY_REMOTE_VECTOR_DENIED,
    CONTEXT_POLICY_SECRET_DENIED,
    context_policy_manifest_warnings,
    decide_context_transmission,
)


def test_local_context_policy_is_passive_and_non_authoritative() -> None:
    decision = decide_context_transmission("local_sqlite", source_kind="memory_record", trust_level="memory")
    payload = decision.to_payload()

    assert decision.allowed is True
    assert decision.code == CONTEXT_POLICY_LOCAL_ALLOWED
    assert "memory_not_authority" in payload["warnings"]
    assert payload["permission_granting"] is False
    assert payload["policy_authority"] is False
    assert payload["approval_authority"] is False
    assert payload["process_started"] is False
    assert payload["filesystem_modified"] is False
    assert payload["provider_call_allowed"] is False
    assert payload["docker_allowed"] is False
    assert payload["adapter_dispatch_allowed"] is False


def test_hosted_and_remote_context_destinations_fail_closed() -> None:
    hosted = decide_context_transmission("hosted_embedding", source_kind="repo_file", trust_level="untrusted_repo")
    remote = decide_context_transmission("qdrant")

    assert hosted.allowed is False
    assert hosted.code == CONTEXT_POLICY_HOSTED_DENIED
    assert remote.allowed is False
    assert remote.code == CONTEXT_POLICY_REMOTE_VECTOR_DENIED
    assert context_policy_manifest_warnings([hosted, remote]) == [
        CONTEXT_POLICY_HOSTED_DENIED,
        "untrusted_context",
        CONTEXT_POLICY_REMOTE_VECTOR_DENIED,
    ]


def test_secret_or_forgotten_context_is_denied_for_any_destination() -> None:
    secret = decide_context_transmission("local_sqlite", path=".env")
    forgotten = decide_context_transmission("local_process", redaction_state="forgotten")

    assert secret.allowed is False
    assert secret.code == CONTEXT_POLICY_SECRET_DENIED
    assert forgotten.allowed is False
    assert forgotten.code == CONTEXT_POLICY_SECRET_DENIED

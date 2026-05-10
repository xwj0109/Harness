from harness.config import DEFAULT_CONTEXT_EXCLUDES, default_config, load_config, write_default_config
from harness.models import BackendDescriptor, BackendKind, DataBoundary, RunMode


def test_run_mode_values_are_stable_strings() -> None:
    assert [mode.value for mode in RunMode] == [
        "read_only",
        "planning",
        "local_edit",
        "codex_edit",
        "test",
        "dev",
    ]


def test_default_config_has_exclusions_and_backends() -> None:
    cfg = default_config()
    assert ".harness/" in cfg.context_excludes
    assert set(DEFAULT_CONTEXT_EXCLUDES).issubset(set(cfg.context_excludes))
    assert {"codex_cli", "local_openai_compatible", "paid_openai_compatible"} <= set(cfg.backends)
    assert cfg.backends["codex_cli"].settings["auth_mode"] == "chatgpt"
    assert cfg.backends["codex_cli"].settings["use_subscription_credits"] is True
    assert cfg.backends["codex_cli"].settings["model"] == "gpt-5.5"
    assert cfg.backends["codex_cli"].settings["model_reasoning_effort"] == "low"
    assert cfg.backends["local_openai_compatible"].metadata.data_boundary == DataBoundary.LOCAL_ONLY
    assert cfg.sandbox.install_project is False
    assert cfg.sandbox.install_project_no_build_isolation is True
    assert cfg.sandbox.image_build_file == "Dockerfile.harness-test"


def test_default_backends_produce_safe_descriptors() -> None:
    cfg = default_config()

    for backend in cfg.backends.values():
        descriptor = backend.to_descriptor()
        dumped = descriptor.model_dump(mode="json")

        assert isinstance(descriptor, BackendDescriptor)
        assert descriptor.name == backend.name
        assert descriptor.kind == backend.kind
        assert descriptor.metadata == backend.metadata
        assert descriptor.capabilities == backend.capabilities
        assert descriptor.operator_notes == []
        if backend.name == "paid_openai_compatible":
            assert descriptor.constraints == [
                "disabled_by_default",
                "no_automatic_fallback",
                "preflight_skipped",
            ]
        else:
            assert descriptor.constraints == []
        assert "settings" not in dumped


def test_backend_descriptors_preserve_current_backend_semantics() -> None:
    cfg = default_config()

    codex = cfg.backends["codex_cli"].to_descriptor()
    local = cfg.backends["local_openai_compatible"].to_descriptor()
    paid = cfg.backends["paid_openai_compatible"].to_descriptor()

    assert codex.kind == BackendKind.EXTERNAL_AGENT
    assert codex.metadata.data_boundary == DataBoundary.HOSTED_PROVIDER
    assert codex.metadata.allow_network is False

    assert local.kind == BackendKind.NATIVE_MODEL
    assert local.metadata.data_boundary == DataBoundary.LOCAL_ONLY
    assert local.metadata.allow_network is False

    assert paid.kind == BackendKind.NATIVE_MODEL
    assert paid.metadata.data_boundary == DataBoundary.HOSTED_PROVIDER
    assert paid.metadata.allow_network is True
    assert paid.constraints == ["disabled_by_default", "no_automatic_fallback", "preflight_skipped"]
    assert cfg.backends["paid_openai_compatible"].settings["enabled"] is False
    assert "enabled" not in paid.model_dump(mode="json")


def test_write_and_load_config(tmp_path) -> None:
    write_default_config(tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.backends["codex_cli"].metadata.billing_mode.value == "subscription"
    assert loaded.backends["paid_openai_compatible"].settings["enabled"] is False
    assert loaded.sandbox.install_project is False
    assert loaded.sandbox.install_project_no_build_isolation is True
    assert loaded.sandbox.image_build_file == "Dockerfile.harness-test"

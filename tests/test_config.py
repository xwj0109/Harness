from harness.config import DEFAULT_CONTEXT_EXCLUDES, default_config, load_config, write_default_config
from harness.models import DataBoundary


def test_default_config_has_exclusions_and_backends() -> None:
    cfg = default_config()
    assert ".harness/" in cfg.context_excludes
    assert set(DEFAULT_CONTEXT_EXCLUDES).issubset(set(cfg.context_excludes))
    assert {"codex_cli", "local_openai_compatible", "paid_openai_compatible"} <= set(cfg.backends)
    assert cfg.backends["local_openai_compatible"].metadata.data_boundary == DataBoundary.LOCAL_ONLY
    assert cfg.sandbox.install_project is False
    assert cfg.sandbox.install_project_no_build_isolation is True
    assert cfg.sandbox.image_build_file == "Dockerfile.harness-test"


def test_write_and_load_config(tmp_path) -> None:
    write_default_config(tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.backends["codex_cli"].metadata.billing_mode.value == "subscription"
    assert loaded.backends["paid_openai_compatible"].settings["enabled"] is False
    assert loaded.sandbox.install_project is False
    assert loaded.sandbox.install_project_no_build_isolation is True
    assert loaded.sandbox.image_build_file == "Dockerfile.harness-test"

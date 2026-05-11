from pathlib import Path


def test_operator_docs_exist_and_reference_main_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    operator_guide = root / "docs" / "operator_guide.md"
    smoke_checklist = root / "docs" / "smoke_checklist.md"
    security = root / "SECURITY.md"
    external_channels_plan = root / "docs" / "plans" / "external_channel_adapters_decision_plan.md"

    assert operator_guide.exists()
    assert smoke_checklist.exists()
    assert security.exists()
    assert external_channels_plan.exists()

    combined = operator_guide.read_text(encoding="utf-8") + "\n" + smoke_checklist.read_text(encoding="utf-8")
    assert "harness tests run" in combined
    assert "codex_code_edit" in combined
    assert "docker build -f Dockerfile.harness-test" in combined
    assert "1.8.0" in combined
    assert "harness capabilities list" in combined
    assert "harness memory save-note" in combined
    assert "harness progress --objective" in combined
    assert "harness.capability_catalog/v1" in combined
    assert "harness.memory_record/v1" in combined
    assert "harness.orchestration_progress/v1" in combined
    assert "OpenAI API usage" in combined
    assert "paid API fallback" in combined
    assert "hosted fallback" in combined
    assert "generic shell" in combined
    assert "browser/email/calendar" in combined
    assert "MCP/A2A" in combined

    security_text = security.read_text(encoding="utf-8")
    assert "Do not use the OpenAI API" in security_text
    assert "Do not add paid API fallback" in security_text
    assert "Do not add hosted fallback" in security_text

    external_channels_text = external_channels_plan.read_text(encoding="utf-8")
    assert "does not authorize implementation" in external_channels_text
    assert "No message sending" in external_channels_text
    assert "No channel may create tasks" in external_channels_text
    assert "Channel credentials" in external_channels_text
    assert "must never be stored" in external_channels_text
    assert "harness.channel_catalog/v1" in external_channels_text

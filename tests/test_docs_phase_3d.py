from pathlib import Path


def test_operator_docs_exist_and_reference_main_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    operator_guide = root / "docs" / "operator_guide.md"
    command_catalog = root / "docs" / "command_catalog.md"
    smoke_checklist = root / "docs" / "smoke_checklist.md"
    security = root / "SECURITY.md"

    assert operator_guide.exists()
    assert command_catalog.exists()
    assert smoke_checklist.exists()
    assert security.exists()

    combined = (
        operator_guide.read_text(encoding="utf-8")
        + "\n"
        + command_catalog.read_text(encoding="utf-8")
        + "\n"
        + smoke_checklist.read_text(encoding="utf-8")
    )
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
    assert "summarize this repo" in combined
    assert "plan how to add X" in combined
    assert "fix the failing test with Codex" in combined

    security_text = security.read_text(encoding="utf-8")
    assert "Do not use the OpenAI API" in security_text
    assert "Do not add paid API fallback" in security_text
    assert "Do not add hosted fallback" in security_text

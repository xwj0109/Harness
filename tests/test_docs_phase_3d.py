from pathlib import Path


def test_operator_docs_exist_and_reference_main_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    operator_guide = root / "docs" / "operator_guide.md"
    smoke_checklist = root / "docs" / "smoke_checklist.md"
    security = root / "SECURITY.md"

    assert operator_guide.exists()
    assert smoke_checklist.exists()
    assert security.exists()

    combined = operator_guide.read_text(encoding="utf-8") + "\n" + smoke_checklist.read_text(encoding="utf-8")
    assert "harness tests run" in combined
    assert "codex_code_edit" in combined
    assert "docker build -f Dockerfile.harness-test" in combined

    security_text = security.read_text(encoding="utf-8")
    assert "Do not use the OpenAI API" in security_text
    assert "Do not add paid API fallback" in security_text
    assert "Do not add hosted fallback" in security_text

import json
from pathlib import Path

import pytest

from harness.tui import build_tui_dashboard
from harness.tui_assets import (
    TuiHomeImageError,
    generate_tui_pixel_art_module,
    resolve_tui_image_path,
    set_tui_home_image,
)
from harness.tui_assets.pixel_art import TUI_PIXEL_ART_HALF_BLOCKS


def test_generated_pixel_art_shape() -> None:
    assert TUI_PIXEL_ART_HALF_BLOCKS
    widths = {len(row) for row in TUI_PIXEL_ART_HALF_BLOCKS}
    assert len(widths) == 1
    assert widths.pop() == 80
    assert len(TUI_PIXEL_ART_HALF_BLOCKS) == 40


def test_generated_pixel_art_uses_hex_colors() -> None:
    foreground, background = TUI_PIXEL_ART_HALF_BLOCKS[0][0]
    assert foreground.startswith("#")
    assert background.startswith("#")
    assert len(foreground) == 7
    assert len(background) == 7


def test_tui_dashboard_pixel_art_remains_json_safe(tmp_path: Path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    json.dumps(dashboard)
    assert "pixel_art" not in dashboard


def test_render_pixel_art_returns_rich_renderable() -> None:
    pytest.importorskip("rich")

    from harness.tui import render_pixel_art

    renderable = render_pixel_art()
    assert renderable is not None


def test_set_tui_home_image_imports_local_image_without_runtime_state(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    image_path = tmp_path / "input.png"
    source_path = tmp_path / "assets" / "home.png"
    output_path = tmp_path / "src" / "harness" / "tui_pixel_art.py"
    image = image_module.new("RGB", (12, 8), color=(240, 180, 120))
    image.save(image_path)

    result = set_tui_home_image(
        image_path,
        width=20,
        source_path=source_path,
        output_path=output_path,
    )

    assert result["schema_version"] == "harness.tui_home_image/v1"
    assert result["ok"] is True
    assert result["width"] == 20
    assert result["terminal_rows"] == 7
    assert source_path.exists()
    generated = output_path.read_text(encoding="utf-8")
    assert "TUI_PIXEL_ART_HALF_BLOCKS" in generated
    assert ".harness" not in generated


def test_generate_tui_pixel_art_module_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(TuiHomeImageError, match="Missing source image"):
        generate_tui_pixel_art_module(source_path=tmp_path / "missing.png", output_path=tmp_path / "out.py")


def test_resolve_tui_image_path_rejects_forbidden_paths(tmp_path: Path) -> None:
    forbidden = tmp_path / ".env.home.png"
    forbidden.write_text("not an image", encoding="utf-8")

    with pytest.raises(TuiHomeImageError, match="forbidden"):
        resolve_tui_image_path(forbidden)

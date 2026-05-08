import json
import shutil
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_has_distribution_metadata_and_packaged_specs() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    project = pyproject["project"]
    assert project["name"] == "agent-harness"
    assert project["version"] == "1.0.0"
    assert project["license"] == "MIT"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["harness"] == "harness.cli.main:app"
    assert "Environment :: Console" in project["classifiers"]
    assert not any(classifier.startswith("License ::") for classifier in project["classifiers"])

    package_data = pyproject["tool"]["setuptools"]["package-data"]["harness"]
    assert "builtin_specs/*.yaml" in package_data
    assert "builtin_specs/agents/**/*.yaml" in package_data
    assert "builtin_specs/workbenches/*.yaml" in package_data


def test_wheel_includes_packaged_specs_and_console_entrypoint(tmp_path) -> None:
    source = _copy_project_source(tmp_path)
    wheel = _build_wheel(source, tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "harness/builtin_specs/model_profiles.yaml" in names
        assert "harness/builtin_specs/agents/quant/quant_research/group.yaml" in names
        assert "harness/builtin_specs/agents/quant/profiles/commodities_researcher.default.yaml" in names
        assert "harness/builtin_specs/workbenches/quant.yaml" in names

        metadata = archive.read("agent_harness-1.0.0.dist-info/METADATA").decode("utf-8")
        assert "Name: agent-harness" in metadata
        assert "Version: 1.0.0" in metadata
        assert "Classifier: Environment :: Console" in metadata

        entrypoints = archive.read("agent_harness-1.0.0.dist-info/entry_points.txt").decode("utf-8")
        assert "harness = harness.cli.main:app" in entrypoints


def test_installed_wheel_cli_loads_packaged_specs(tmp_path) -> None:
    source = _copy_project_source(tmp_path)
    wheel = _build_wheel(source, tmp_path)
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    harness = venv_dir / "bin" / "harness"

    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        text=True,
        check=True,
        capture_output=True,
    )

    help_result = subprocess.run([str(harness), "--help"], text=True, check=True, capture_output=True)
    assert "Local-first agent harness" in help_result.stdout

    specs_result = subprocess.run(
        [str(harness), "specs", "--output", "json"],
        text=True,
        check=True,
        capture_output=True,
    )
    specs = json.loads(specs_result.stdout)
    assert "quant_orchestrator" in specs["agents"]
    assert "commodities_researcher.default" in specs["agent_profiles"]

    project_dir = tmp_path / "operator-project"
    project_dir.mkdir()
    home_result = subprocess.run(
        [str(harness), "home", "--project", str(project_dir), "--output", "json"],
        text=True,
        check=True,
        capture_output=True,
    )
    assert json.loads(home_result.stdout)["schema_version"] == "harness.home/v1"
    assert not (project_dir / ".harness").exists()

    quickstart_result = subprocess.run(
        [str(harness), "quickstart", "agent", "--project", str(project_dir), "--output", "json"],
        text=True,
        check=True,
        capture_output=True,
    )
    assert json.loads(quickstart_result.stdout)["schema_version"] == "harness.quickstart_agent/v1"


def _copy_project_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "README.md", source / "README.md")
    shutil.copytree(
        ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.egg-info"),
    )
    return source


def _build_wheel(source: Path, tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheelhouse),
            str(source),
        ],
        text=True,
        check=True,
        capture_output=True,
    )
    wheels = sorted(wheelhouse.glob("agent_harness-*.whl"))
    assert len(wheels) == 1
    return wheels[0]

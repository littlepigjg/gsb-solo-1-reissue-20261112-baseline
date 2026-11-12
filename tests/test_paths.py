"""Project paths remain relative after moving or launching from a parent folder."""
import os
import subprocess
import sys
from pathlib import Path
from backend.settings import Settings, PROJECT_ROOT


def test_relative_project_and_data_paths(tmp_path):
    assert not PROJECT_ROOT.is_absolute()
    assert not Settings.from_environment().root.is_absolute()
    configured = Settings.from_environment(tmp_path / "data")
    assert not configured.root.is_absolute()
    assert os.path.samefile(configured.root.parent, tmp_path)


def test_load_from_parent_directory(tmp_path, monkeypatch):
    project = PROJECT_ROOT.name if PROJECT_ROOT.name else Path.cwd().name
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from backend.app import create_app; "
        "from backend.settings import PROJECT_ROOT; "
        "assert not PROJECT_ROOT.is_absolute(); "
        "app=create_app(data_dir=sys.argv[2], testing=True); client=app.test_client(); "
        "assert client.get('/').status_code==200; "
        "assert client.get('/static/app.js').status_code==200; "
        "app.extensions['studio']['jobs'].shutdown()"
    )
    interpreter = os.path.relpath(sys.executable, "..")
    data_path = os.path.relpath(tmp_path / "parent-probe", "..")
    monkeypatch.chdir("..")
    result = subprocess.run([interpreter, "-c", probe, project, data_path],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

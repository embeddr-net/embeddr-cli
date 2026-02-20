from __future__ import annotations

from pathlib import Path

import questionary

from embeddr.commands import init_wizard
from embeddr.core.project import CONFIG_FILENAME, create_default_config


def test_create_default_config_writes_database_and_paths(tmp_path: Path):
    create_default_config(
        tmp_path,
        name="demo",
        database_provider="postgresql",
        database_url="postgresql://user:pass@localhost:5432/embeddr",
        data_dir=".embeddr",
        plugins_dir=".embeddr/plugins",
    )

    config_path = tmp_path / CONFIG_FILENAME
    content = config_path.read_text()

    assert "provider = \"postgresql\"" in content
    assert "url = \"postgresql://user:pass@localhost:5432/embeddr\"" in content
    assert "data_dir = \".embeddr\"" in content
    assert "plugins_dir = \".embeddr/plugins\"" in content


def test_list_dist_plugins(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "plugins-dist"
    dist_dir.mkdir()
    plugin_dir = dist_dir / "embeddr-search"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("# plugin")

    monkeypatch.setenv("EMBEDDR_PLUGINS_DIST_DIR", str(dist_dir))

    plugins = init_wizard._list_dist_plugins(dist_dir)
    assert plugins == ["embeddr-search"]


def test_install_plugins_from_dist(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "plugins-dist"
    dist_dir.mkdir()
    plugin_dir = dist_dir / "embeddr-core"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("# plugin")

    monkeypatch.setenv("EMBEDDR_PLUGINS_DIST_DIR", str(dist_dir))
    monkeypatch.setattr(questionary, "confirm",
                        lambda *args, **kwargs: _Dummy(True))

    target_dir = tmp_path / "workspace" / "plugins"

    init_wizard._install_plugins_from_dist(target_dir, ["embeddr-core"])

    installed = target_dir / "embeddr-core" / "plugin.py"
    assert installed.exists()


def test_run_init_wizard_creates_workspace_config_and_plugins(tmp_path: Path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    dist_dir = tmp_path / "plugins-dist"
    dist_dir.mkdir()
    plugin_dir = dist_dir / "embeddr-core"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("# plugin")

    monkeypatch.setenv("EMBEDDR_PLUGINS_DIST_DIR", str(dist_dir))

    text_answers = iter([
        "Demo Workspace",  # project name
        ".embeddr",         # data dir
        ".embeddr/plugins",  # plugins dir
    ])

    select_answers = iter([
        "sqlite",    # db provider
        "open",      # auth mode
        # plugin pack (includes embeddr-core + embeddr-storage-local)
        "minimal",
    ])

    monkeypatch.setattr(
        questionary,
        "text",
        lambda *args, **kwargs: _Dummy(next(text_answers)),
    )
    monkeypatch.setattr(
        questionary,
        "select",
        lambda *args, **kwargs: _Dummy(next(select_answers)),
    )
    monkeypatch.setattr(
        questionary,
        "confirm",
        lambda *args, **kwargs: _Dummy(True),
    )

    init_wizard.run_init_wizard(workspace_dir, None, run_db_migrations=False)

    config_path = workspace_dir / CONFIG_FILENAME
    assert config_path.exists()
    content = config_path.read_text()
    assert "data_dir = \".embeddr\"" in content
    assert "plugins_dir = \".embeddr/plugins\"" in content

    installed = workspace_dir / ".embeddr" / \
        "plugins" / "embeddr-core" / "plugin.py"
    assert installed.exists()


class _Dummy:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value

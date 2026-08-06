"""Onboarding: `coordsys setup` is the whole first five minutes -
map + agent wiring + the three measured workflow rules - and must
be idempotent (never clobber a user's own files)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordsys import cli


def _mini_repo(tmp_path):
    (tmp_path / "app.py").write_text(
        "def greet(name):\n    return 'hi ' + name\n")
    return str(tmp_path)


def test_setup_creates_map_wiring_and_rules(tmp_path, capsys):
    repo = _mini_repo(tmp_path)
    cli.cmd_setup(repo)
    out = capsys.readouterr().out
    assert (tmp_path / ".coord").exists()
    wiring = json.loads((tmp_path / ".mcp.json").read_text())
    assert wiring["mcpServers"]["coordsys"]["command"] == "coordsys"
    assert wiring["mcpServers"]["coordsys"]["args"] == ["mcp", "."]
    rules = (tmp_path / "CLAUDE.md").read_text()
    # exact tool names (finding #14): agents must not have to guess
    for tool in ("coordsys_before_edit", "coordsys_verify_change",
                 "coordsys_meta"):
        assert tool in rules
    assert "next steps" in out


def test_setup_is_idempotent_and_preserves_user_files(tmp_path):
    repo = _mini_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# my own rules\n")
    cli.cmd_setup(repo)
    assert (tmp_path / "CLAUDE.md").read_text() == "# my own rules\n"
    first_wiring = (tmp_path / ".mcp.json").read_text()
    cli.cmd_setup(repo)          # second run: everything preserved
    assert (tmp_path / ".mcp.json").read_text() == first_wiring
    assert (tmp_path / ".coord").exists()

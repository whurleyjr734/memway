"""Rules go to every filename an agent might read, from one template.

WHY THREE FILES: a client reads the filename it knows and ignores the
rest. A repo carrying only CLAUDE.md gives a non-Claude agent no rules at
all, so it works correctly and records nothing - the exact failure the
rules exist to prevent, and invisible while it happens. AGENTS.md is
canonical; CLAUDE.md and GEMINI.md are copies.

WHY A TEST RATHER THAN CARE: three copies of anything drift. The managed
block is asserted byte-identical across all three, so drift is a failing
test rather than a thing someone notices later.

WHY THE REFUSAL MATTERS MOST: `setup` runs on repos that already have a
CLAUDE.md, and some of those were written by a person. A file we cannot
prove is ours is left alone and reported. Silently rewriting somebody's
project rules is worse than doing nothing.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway.cli import (RULE_FILES, _LEGACY_RULES, _RULES_BEGIN, _RULES_END,
                        managed_block, plan_rules_write, rules_document)


def _repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
    (r / "a.py").write_text('def f(x):\n    """D."""\n    return x\n')
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c",
                    "user.name=T", "commit", "-qm", "s", "--no-gpg-sign"],
                   check=True)
    return r


def _setup(repo):
    r = subprocess.run([sys.executable, "-m", "memway.cli", "setup", str(repo)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout


# ------------------------------------------------- the identity guarantee

def test_all_three_rule_files_are_emitted(tmp_path):
    repo = _repo(tmp_path)
    _setup(repo)
    for name in RULE_FILES:
        assert (repo / name).exists(), f"{name} was not emitted"
    assert RULE_FILES[0] == "AGENTS.md", "AGENTS.md is the canonical name"


def test_emitted_rule_files_are_byte_identical(tmp_path):
    """The whole point. If this fails, one agent is reading different
    rules from another in the same repo."""
    repo = _repo(tmp_path)
    _setup(repo)
    blobs = {name: (repo / name).read_bytes() for name in RULE_FILES}
    distinct = set(blobs.values())
    assert len(distinct) == 1, (
        "rule files differ: " +
        ", ".join(f"{n}={len(b)}b" for n, b in blobs.items()))


def test_managed_block_stays_identical_even_when_tails_differ(tmp_path):
    """User additions are per-file and may differ. The MANAGED region may
    not: that is the part memway asserts, and the part agents obey."""
    repo = _repo(tmp_path)
    _setup(repo)
    (repo / "CLAUDE.md").write_text(
        (repo / "CLAUDE.md").read_text() + "\n## Ours\n\n- Local rule.\n")
    _setup(repo)
    blocks = {managed_block((repo / n).read_text()) for n in RULE_FILES}
    assert len(blocks) == 1, "the managed block drifted between files"
    assert "Local rule." in (repo / "CLAUDE.md").read_text()


def test_one_template_feeds_every_file(tmp_path):
    """Structural: cmd_setup must not hand-roll per-file content."""
    src = (HERE / "memway" / "cli.py").read_text()
    assert "for name in RULE_FILES:" in src
    assert src.count("rules_document()") >= 1


# ------------------------------------------------------------- upgrades

def test_legacy_unedited_rules_upgrade_in_place(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(_LEGACY_RULES[0])
    out = _setup(repo)
    assert "upgraded CLAUDE.md" in out
    assert _RULES_BEGIN in (repo / "CLAUDE.md").read_text()
    blobs = {(repo / n).read_bytes() for n in RULE_FILES}
    assert len(blobs) == 1


def test_user_tail_below_the_marker_survives_upgrade(tmp_path):
    repo = _repo(tmp_path)
    _setup(repo)
    tail = "\n## House rules\n\n- Never touch billing/ without Susan.\n"
    (repo / "CLAUDE.md").write_text((repo / "CLAUDE.md").read_text() + tail)
    out = _setup(repo)
    assert "kept your additions" in out
    after = (repo / "CLAUDE.md").read_text()
    assert "Never touch billing/ without Susan." in after
    assert after.count(_RULES_BEGIN) == 1, "the block was duplicated"
    assert after.count(_RULES_END) == 1


def test_repeated_setup_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    _setup(repo)
    first = {n: (repo / n).read_bytes() for n in RULE_FILES}
    _setup(repo)
    _setup(repo)
    assert {n: (repo / n).read_bytes() for n in RULE_FILES} == first


# --------------------------------------------------------- the refusal

def test_foreign_rules_file_is_refused_not_clobbered(tmp_path):
    """The case that must never regress: somebody's own CLAUDE.md."""
    repo = _repo(tmp_path)
    mine = "# MY OWN RULES\n\nDo not touch this file.\n"
    (repo / "CLAUDE.md").write_text(mine)
    out = _setup(repo)
    assert (repo / "CLAUDE.md").read_text() == mine, "a human's file was rewritten"
    assert "REFUSING" in out
    assert "CLAUDE.md" in out
    # the other two are still emitted; refusing one file is not refusing all
    assert (repo / "AGENTS.md").exists() and (repo / "GEMINI.md").exists()


def test_refusal_names_the_opt_in(tmp_path):
    """A refusal that does not say how to proceed is a dead end."""
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("# mine\n")
    out = _setup(repo)
    assert _RULES_BEGIN in out, "the refusal must name the marker to add"


@pytest.mark.parametrize("name", RULE_FILES)
def test_every_rule_filename_is_defended(tmp_path, name):
    """Not just CLAUDE.md: each of the three refuses a foreign file."""
    repo = _repo(tmp_path)
    mine = f"# hand-written {name}\n"
    (repo / name).write_text(mine)
    _setup(repo)
    assert (repo / name).read_text() == mine


# ---------------------------------------------------------- the content

def test_rules_name_tools_exactly_and_give_cli_equivalents():
    """Exact names because guessing means skipping (finding #14); CLI
    equivalents because a rule written only in MCP terms is unusable to a
    client without an MCP server."""
    doc = rules_document()
    for tool in ("memway_before_edit", "memway_verify_change",
                 "memway_meta", "memway_at"):
        assert tool in doc, f"{tool} missing from the rules"
    assert "memway --json before-edit" in doc
    assert "memway at ." in doc
    assert "memway meta ." in doc
    # verify_change is MCP-only today; the rules must say so rather than
    # invent a command that does not exist.
    from memway import cli, query
    assert "verify" not in cli.COMMANDS and "verify-change" not in query.QUERIES
    assert "MCP only" in doc


def test_rules_document_is_marker_wrapped():
    doc = rules_document()
    assert doc.startswith(_RULES_BEGIN)
    assert _RULES_END in doc
    assert managed_block(doc).startswith(_RULES_BEGIN)


def test_unmarked_text_has_no_managed_block():
    assert managed_block("# something else\n") == ""


def test_plan_never_returns_content_for_a_foreign_file(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# not ours\n")
    content, msg = plan_rules_write(p, rules_document())
    assert content is None
    assert "REFUSING" in msg


def test_readme_quotes_the_rules_verbatim():
    """The README presents the rules block as what `setup` writes, which
    is a checkable claim. It has gone stale once already (the README
    quoted a version missing the whole 'due whenever a reason SURFACES'
    paragraph), so it is pinned rather than trusted."""
    import re
    quoted = re.search(r'```markdown\n(.*?)```',
                       (HERE / "README.md").read_text(), re.S)
    assert quoted, "the README no longer quotes a rules block"
    from memway.cli import _AGENT_RULES_BODY
    assert quoted.group(1).strip() == _AGENT_RULES_BODY.strip(), \
        "README's quoted rules have drifted from the emitted template"


def test_readme_names_all_three_rule_files():
    readme = (HERE / "README.md").read_text()
    for name in RULE_FILES:
        assert name in readme, f"README does not mention {name}"

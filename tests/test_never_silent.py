"""The map never misleads silently.

freshness.py wrote the principle down for maps - "the map may lag; it must
never lag SILENTLY" - and enforced it by making every read say so on the
way past. Knowledge got the detection and none of the telling: `show
<ref>` flagged a stale entry only if you already suspected that
coordinate, and nothing said anything repo-wide.

Which is how it failed. 0.54.1 shipped a workflow rule saying "supersede
what your change staled" and then broke it within the hour, twice in one
evening, by the person who wrote the rule, with the tool installed and
the rule loaded. Nothing told them the six coordinates existed.

A rule that depends on recall is the failure this project exists to fix.
So the telling became ambient.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import query
from memway.viz import project_name, map_label

SRC = 'def widget(x):\n    """D."""\n    return x + 1\n'
EDIT = 'def widget(x):\n    """D."""\n    return x + 99\n'


def _git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a],
                          capture_output=True, text=True)


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=str(HERE))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one", "--no-gpg-sign")
    assert _cli("init", str(r)).returncode == 0
    assert "added notes entry" in _cli(
        "meta", str(r), "widget", "notes", "The +1 is load-bearing.").stdout
    return r


# ------------------------------------------------- the ambient warning

def test_a_read_reports_stale_knowledge_without_being_asked(repo):
    """The whole point. Not `attention`, not `show <the exact ref>` - an
    ordinary read of anything, reporting that something somewhere rotted."""
    (repo / "m.py").write_text(EDIT)
    _cli("index", str(repo))

    out = _cli("summary", str(repo))
    assert "hold" in out.stdout and "stale knowledge" in out.stdout, out.stdout

    for q in ("summary", "show", "before-edit"):
        args = [q, str(repo)] + (["widget"] if q != "summary" else [])
        d = json.loads(_cli("--json", *args).stdout)
        kl = d.get("knowledge_lag")
        assert kl, f"{q} carries no knowledge_lag: {list(d)}"
        assert kl["count"] == 1, kl
        assert "memway attention" in kl["message"], kl


def test_it_is_SILENT_when_every_stale_entry_was_superseded(repo):
    """THE flagship case, and the one that decides whether this is a
    warning or noise.

    Entries are append-only, so a repo that has diligently answered every
    stale note still HOLDS stale rows on disk forever. memway's own map
    carries 23 of them and must report nothing. A warning that fires
    forever is not a warning - it is the thing people learn to scroll
    past, and then the real one goes unread too.
    """
    (repo / "m.py").write_text(EDIT)
    _cli("index", str(repo))
    assert json.loads(_cli("--json", "summary", str(repo)).stdout)["knowledge_lag"]

    _cli("meta", str(repo), "widget", "notes", "Re-checked: still +1 by design.")

    d = json.loads(_cli("--json", "summary", str(repo)).stdout)
    assert d["knowledge_lag"] == {}, d["knowledge_lag"]
    assert "stale knowledge" not in _cli("summary", str(repo)).stdout

    # ...and the superseded row is still on disk. If this count is 0 the
    # test above proves nothing, because there would be no history to
    # ignore in the first place.
    entries = []
    for f in (repo / ".coord/meta").rglob("*.jsonl"):
        entries += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    assert len(entries) >= 2, entries
    ix_out = json.loads(_cli("--json", "show", str(repo), "widget").stdout)
    stale_rows = [k for k in ix_out["knowledge"] if k["stale"]]
    assert stale_rows, "no superseded-stale row survives; the silence is vacuous"


def test_the_ambient_line_uses_the_ring_rule_not_a_row_count(repo):
    """Falsification guard: counting stale ROWS instead of unsuperseded
    coordinates is the bug this test exists to catch - it is what makes a
    diligent repo shout forever."""
    (repo / "m.py").write_text(EDIT)
    _cli("index", str(repo))
    _cli("meta", str(repo), "widget", "notes", "Answer 1.")
    _cli("meta", str(repo), "widget", "notes", "Answer 2.")
    d = json.loads(_cli("--json", "summary", str(repo)).stdout)
    assert d["knowledge_lag"] == {}, \
        "answered coordinate still counted - the rule is counting rows"


# ------------------------------------------------- the pre-commit hook

def test_the_pre_commit_hook_reports_and_never_blocks(repo):
    """It fires with zero memory required, which is the only reason it
    works where a rule did not."""
    from memway.hooks import HOOKS, PRE_COMMIT
    assert PRE_COMMIT in HOOKS
    assert _cli("hooks", "install", str(repo)).returncode == 0
    hook = repo / ".git" / "hooks" / PRE_COMMIT
    assert hook.exists() and hook.stat().st_mode & 0o111

    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "-A")
    r = _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "change widget", "--no-gpg-sign")
    assert r.returncode == 0, r.stderr
    assert "STALED KNOWLEDGE" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert _git(repo, "log", "--oneline", "-1").stdout.strip(), "commit lost"


def test_the_hook_exits_zero_even_when_memway_is_broken(repo):
    """A hook that blocks a commit gets removed, correctly. Proven by
    pointing it at a binary that does not exist."""
    from memway.hooks import PRE_COMMIT
    _cli("hooks", "install", str(repo))
    hook = repo / ".git" / "hooks" / PRE_COMMIT
    import re
    body = hook.read_text()
    broken = re.sub(r'"[^"]*memway"', '"/nonexistent/memway"', body, count=1)
    assert broken != body, "sabotage did not apply"
    hook.write_text(broken)
    r = subprocess.run(["/bin/sh", str(hook)], capture_output=True,
                       text=True, cwd=str(repo))
    assert r.returncode == 0, (r.returncode, r.stderr)


# ------------------------------------------------------ surface parity

# Tools that are NOT expected on all three doors, with the reason. An
# entry here is a decision, not an excuse - the point of the list is that
# adding to it takes an argument.
SURFACE_EXEMPT = {
    "meta": "a WRITE; --json is the read surface and QUERIES holds reads only",
    "probe": "executes the user's code; neither a read nor a safe default door",
}


def test_every_mcp_tool_has_three_doors_or_a_documented_reason():
    """The class closes structurally.

    Fixed instance-by-instance, this recurs: `attention` was MCP-only
    until 0.54.1, and the same day it was fixed, `summary`, `before_edit`
    and `verify_change` were still missing their CLI door - one of which
    is why the pre-commit hook had nothing readable to call.
    """
    from memway import mcp
    from memway.cli import COMMANDS
    missing = {}
    for tool in {t["name"] for t in mcp.TOOLS}:
        short = tool.replace("memway_", "")
        if short in SURFACE_EXEMPT:
            continue
        doors = []
        if short.replace("_", "-") not in query.QUERIES:
            doors.append("--json")
        if short.replace("_", "-") not in COMMANDS and short not in COMMANDS:
            doors.append("CLI")
        if doors:
            missing[short] = doors
    assert not missing, (
        f"MCP tools missing doors: {missing}\n"
        f"either add the door, or add an entry to SURFACE_EXEMPT with a reason")


def test_the_exemption_list_is_reasoned_and_real():
    """Guard the guard: exemptions must name real tools and carry a
    reason, or the test above passes by emptying itself."""
    from memway import mcp
    names = {t["name"].replace("memway_", "") for t in mcp.TOOLS}
    for k, why in SURFACE_EXEMPT.items():
        assert k in names, f"{k} is exempted but is not an MCP tool"
        assert len(why) > 20, f"{k}'s exemption is not a reason: {why!r}"


# --------------------------------------------------- the name derivation

def test_name_tier_1_pyproject(tmp_path):
    r = tmp_path / "p1"; r.mkdir()
    (r / "pyproject.toml").write_text('[project]\nname = "from-pyproject"\n')
    assert project_name(r) == "from-pyproject"


def test_name_tier_2_package_json(tmp_path):
    r = tmp_path / "p2"; r.mkdir()
    (r / "package.json").write_text('{"name": "from-package-json"}')
    assert project_name(r) == "from-package-json"


def test_name_tier_3_git_remote(tmp_path):
    r = tmp_path / "p3"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "remote", "add", "origin", "https://example.com/org/from-remote.git")
    assert project_name(r) == "from-remote"


def test_name_tier_4_directory_last(tmp_path):
    r = tmp_path / "from-directory"; r.mkdir()
    assert project_name(r) == "from-directory"


def test_first_in_chain_wins_when_they_disagree(tmp_path):
    """The pathological polyglot repo. Deterministic beats clever: a rule
    you can predict is worth more than one that is right slightly more
    often."""
    r = tmp_path / "poly"; r.mkdir()
    (r / "pyproject.toml").write_text('[project]\nname = "python-name"\n')
    (r / "package.json").write_text('{"name": "node-name"}')
    _git(r, "init", "-q", "-b", "main")
    _git(r, "remote", "add", "origin", "https://example.com/o/remote-name.git")
    assert project_name(r) == "python-name"


def test_a_malformed_manifest_never_breaks_a_render(tmp_path):
    r = tmp_path / "broken"; r.mkdir()
    (r / "pyproject.toml").write_text("this is not toml {{{")
    (r / "package.json").write_text("nor is this json")
    assert project_name(r) == "broken"


# ------------------------------------------- flagship page identity

def test_the_flagship_page_does_not_lie_about_itself(tmp_path):
    """Every identity string on the emitted page, against the derivation.

    HONEST SCOPE: this closes CONSTANT DRIFT permanently and cheaply -
    a title, header or version that says something the code no longer
    says. It will NOT catch the next dead toggle. The payload, airgap and
    executed-predicate tests all passed on a page whose tab named someone
    else's project for weeks, and this test would have caught that in a
    second; equally, it would not have caught the 0.53.0 lens toggle,
    which had every constant right and did nothing. Different class,
    different layer.
    """
    import re
    from memway.viz import export, render
    import memway

    # The directory name must not be a SUBSTRING of the project name, or
    # the final assertion cannot discriminate (first attempt used "flag"
    # inside "flagship-fixture" and failed on its own arithmetic).
    r = tmp_path / "zzz-directory-name"; r.mkdir()
    (r / "pyproject.toml").write_text('[project]\nname = "flagship-fixture"\n')
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", str(r)).returncode == 0

    payload = export(str(r))
    html = render(payload)
    expected = map_label(r, "", len(payload["entities"]), len(payload["edges"]))

    assert expected.startswith("flagship-fixture"), expected
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
    assert title.endswith(expected), (title, expected)
    assert payload["repo"] == expected
    assert Path(r).name not in title, \
        "the tab fell back to the directory name despite a declared name"


def test_it_cannot_block_a_commit_when_appended_to_someone_elses_hook(repo):
    """Where `|| true` actually earns its place.

    A FRESH hook file gets a trailing `exit 0` from plan(), which masks
    everything before it - so removing `|| true` changes nothing there,
    and the first falsification of it did not bite. But when the user
    ALREADY has a pre-commit hook (a linter, a formatter - the common
    case), our block is appended and plan() deliberately adds no `exit 0`,
    because that residue would survive uninstall inside somebody else's
    file. Then OUR pipeline's status is the hook's status, and grep exits
    1 whenever there is nothing stale to report - which is most commits.

    Without `|| true`, installing memway into a repo that already has a
    pre-commit hook would block every clean commit. That is the failure
    that gets a tool deleted.
    """
    from memway.hooks import PRE_COMMIT
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    theirs = hooks_dir / PRE_COMMIT
    theirs.write_text("#!/bin/sh\necho 'their linter ran'\n")
    theirs.chmod(0o755)

    assert _cli("hooks", "install", str(repo)).returncode == 0
    body = theirs.read_text()
    assert "their linter ran" in body, "we clobbered their hook"
    assert "exit 0" not in body, (
        "plan() added an exit 0 into somebody else's file - that residue "
        "survives uninstall, and it would also mask this test")

    # nothing is stale here, so the report finds nothing: the exact case
    # where an unguarded grep exits 1
    r = subprocess.run(["/bin/sh", str(theirs)], capture_output=True,
                       text=True, cwd=str(repo))
    assert r.returncode == 0, (
        f"a clean commit would have been BLOCKED: exit {r.returncode}\n{body}")


# ------------------------- superseding must actually heal, first try

def test_supersede_without_reindexing_produces_a_FRESH_note(repo):
    """THE release's promise, as a test. Exactly the reproduction.

        edit -> the hook names the stale note -> supersede -> index
        -> the new note must read stale: False

    NO index between the edit and the supersede, because that is the
    sequence a user is actually in: the pre-commit hook has just told
    them what they staled, and nothing tells them to re-index first.

    It failed before 0.54.2. `meta` stamped from the STORED index, whose
    hash predates the edit, so the brand-new superseding note was born
    stale - it healed nothing, and the coordinate stayed coral. Two
    "corals -> 0" runs on memway's own map only worked because their
    author happened to re-index first, out of habit rather than
    instruction.
    """
    (repo / "m.py").write_text(EDIT)

    # the hook's view: this is what the user is told to supersede
    vc = json.loads(_cli("--json", "verify-change", str(repo)).stdout)
    staled = vc["staled_knowledge"]
    assert len(staled) == 1, vc
    assert staled[0]["channel"] == "notes"

    # supersede exactly as instructed - no index, no commit
    out = _cli("meta", str(repo), "widget", staled[0]["channel"],
               "Re-checked after the change: still load-bearing.")
    assert "added notes entry" in out.stdout, out.stdout + out.stderr

    # now the index catches up, as it eventually always does
    assert _cli("index", str(repo)).returncode == 0

    shown = json.loads(_cli("--json", "show", str(repo), "widget").stdout)
    # [0], not [-1]: the payload leads with the NEWEST entry as of 0.54.2.
    # This took [-1] and passed until the reorder landed, at which point it
    # was reading the superseded entry and calling it the new one.
    notes = [k for k in shown["knowledge"] if k["channel"] == "notes"]
    newest = notes[0]
    assert newest["superseded"] is False, ("the panel does not lead with the "
                                           f"deciding entry: {notes}")
    assert newest["text"].startswith("Re-checked"), newest
    assert newest["stale"] is False, (
        f"the superseding note was born stale - superseding as instructed "
        f"healed nothing: {newest}")

    # and the ring is clear, which is the user-visible half
    d = json.loads(_cli("--json", "summary", str(repo)).stdout)
    assert d["knowledge_lag"] == {}, d["knowledge_lag"]


def test_a_meta_write_still_touches_exactly_one_file(repo):
    """Write scope unchanged. stamp_for now READS the working tree, and a
    re-parse that started persisting anything would put a write back into
    the one path that must stay surgical."""
    root = repo / ".coord"

    def fp():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    (repo / "m.py").write_text(EDIT)
    before = fp()
    assert "added notes entry" in _cli(
        "meta", str(repo), "widget", "notes", "second note").stdout
    after = fp()
    changed = sorted(set(before) ^ set(after)) + \
        sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert len(changed) == 1, changed
    assert changed[0].endswith("notes.jsonl"), changed


def test_a_ref_that_vanished_from_the_tree_is_refused_not_guessed(repo):
    """Never stamp against a ghost.

    The entity is in the map and gone from the working tree - deleted or
    renamed since the last index. A note stamped against it could never be
    fresh and could never be superseded, attached to a coordinate the code
    has abandoned. Refusing costs the user one re-index; guessing costs
    them a note that silently means nothing.
    """
    (repo / "m.py").write_text("def something_else(y):\n    return y\n")
    r = _cli("meta", str(repo), "widget", "notes", "note on a ghost")
    assert r.returncode != 0, r.stdout
    combined = r.stdout + r.stderr
    assert "working tree" in combined, combined
    assert "memway index" in combined, combined
    entries = []
    for f in (repo / ".coord/meta").rglob("*.jsonl"):
        entries += [l for l in f.read_text().splitlines() if l.strip()]
    assert len(entries) == 1, "the refused note was written anyway"


def test_init_ignores_the_regenerable_tiers_but_not_the_authored_ones(tmp_path):
    """The derived-tier taxonomy, expressed where git can act on it.

    Measured before the fix: `memway init` then `git add -A` in a fresh
    repo staged .coord/cache/*.pkl - binary blobs that change on every
    index and conflict on every merge. This repo's own root .gitignore
    had covered them by hand for months, so it never showed up here.

    It goes INSIDE .coord because that directory is memway's to manage.
    Editing the user's root .gitignore would be the same trespass as
    rewriting their CLAUDE.md or their git hook.
    """
    r = tmp_path / "fresh"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", str(r)).returncode == 0

    gi = r / ".coord" / ".gitignore"
    assert gi.exists(), ".coord/.gitignore not written"
    body = gi.read_text()
    for regenerable in ("cache/", "evidence/", "log/", "versions/"):
        assert regenerable in body, regenerable
    for authored in ("meta", "lineage", "docbindings"):
        assert f"\n{authored}" not in body, \
            f"{authored} must stay TRACKED - it is authored or a baseline"

    _git(r, "add", "-A")
    staged = _git(r, "status", "--porcelain").stdout
    assert ".coord/cache" not in staged, staged
    assert ".coord/index" in staged, "the index must still be tracked"

    # a re-init must not clobber an edited copy
    gi.write_text(body + "my-own-line/\n")
    assert _cli("init", str(r)).returncode == 0
    assert "my-own-line/" in gi.read_text(), "re-init clobbered the user's edit"


# ------------------------------- what a human reads, in what order

def test_the_panel_leads_with_the_entry_that_decides(repo):
    """Newest first, and the older one labelled history rather than warning.

    The panel ran oldest-first because entries are append-only and nothing
    reordered them. So on a coordinate whose ring said FRESH, the first
    thing a reader saw was an entry marked STALE - the very one the ring
    rule had discarded. The ring and the panel contradicted each other on
    screen, and the reader had to scroll to reach the truth.

    THE FIXTURE IS ORDERED SO ONLY THE CORRECT RULE PASSES, both ways:
    the older entry is the STALE one and the newer is FRESH, so a payload
    that forgot to reverse leads with stale=True, and one that labelled
    the DECIDING entry as superseded would mark the fresh one. Neither
    mistake can slip through by symmetry.
    """
    (repo / "m.py").write_text(EDIT)
    _cli("index", str(repo))                      # the note is now stale
    _cli("meta", str(repo), "widget", "notes", "Re-checked: still +1.")

    d = json.loads(_cli("--json", "show", str(repo), "widget").stdout)
    kn = [k for k in d["knowledge"] if k["channel"] == "notes"]
    assert len(kn) == 2, kn

    first, second = kn
    assert first["stale"] is False, f"panel leads with a stale entry: {first}"
    assert first["superseded"] is False, "the deciding entry was labelled superseded"
    assert first["text"].startswith("Re-checked"), first

    assert second["superseded"] is True, "the older entry is not labelled history"
    assert second["stale"] is True, "fixture no longer discriminates: the " \
        "older entry must be stale, or 'leads with fresh' proves nothing"

    # the ring still says fresh - panel and ring now agree
    assert json.loads(_cli("--json", "summary", str(repo)).stdout)["knowledge_lag"] == {}


def test_superseded_is_carried_into_the_rendered_page(tmp_path):
    """Executed, not asserted on the payload.

    A key added to the payload and not to normalize() is a silent no-op -
    that is exactly how is_test arrived undefined on every node in 0.53.0
    while every presence test stayed green. So this lifts the shipped
    normalize() out of the emitted bytes and runs it.
    """
    from memway.viz import export, render
    r = tmp_path / "panel"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))
    _cli("meta", str(r), "widget", "notes", "first")
    (r / "m.py").write_text(EDIT)
    _cli("index", str(r))
    _cli("meta", str(r), "widget", "notes", "second")

    html = render(export(str(r)))
    src = html[html.index("function normalize("):]
    depth, i = 0, src.index("{")
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        if depth == 0:
            break
        i += 1
    fn_src = src[:i + 1]

    # EXECUTED, not grepped. The first version of this asserted
    # `"superseded" in fn_src` and passed against a sabotage that removed
    # the flag from the object branch, because the word still appeared in
    # the string branch. Presence is not behaviour - the same mistake that
    # let is_test ship undefined.
    import shutil
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable; the executed check is the real one")
    payload = html[html.index("const SAMPLE = ") + len("const SAMPLE = "):]
    payload = payload[:payload.index("\n")].rstrip().rstrip(";")
    prog = (fn_src + "\nconst out = normalize(" + payload + ");\n"
            "const kn = out.entities.flatMap(e => e.knowledge);\n"
            "console.log(JSON.stringify({\n"
            "  undef: kn.filter(k => k.superseded === undefined).length,\n"
            "  sup: kn.filter(k => k.superseded === true).length,\n"
            "  total: kn.length}));")
    r = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    got = json.loads(r.stdout)
    assert got["total"] >= 2, got
    assert got["undef"] == 0, \
        f"normalize() dropped the flag on {got['undef']} entries - the panel " \
        f"renders history as a warning"
    assert got["sup"] >= 1, \
        f"no entry survived as superseded, so this cannot discriminate: {got}"

    import re
    assert re.search(r'class="note \$\{k\.superseded\?"superseded"', html), \
        "the template does not style superseded entries distinctly"

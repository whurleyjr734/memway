"""Knowledge survives a version jump; staleness tells you when to check.

A bundle records `upstream_sha` and `pull` installs its INDEX whole, so a
checkout at any other commit ends up holding a map of code it does not
have - with `drifted: true` as the entire response. But a bundle is two
things of very different value: an index that regenerates locally in
seconds, and authored knowledge nobody can reconstruct.

So `--replay` indexes YOUR tree and carries only the knowledge across.
Coordinates are sha256(qualname), so anything unrenamed matches exactly;
what moved falls to lineage.score_pair, whose signals every bundle
already ships.

The property that makes it safe is that replayed entries keep their
ORIGINAL stamp. A note written against v1 and replayed onto v2 reads
STALE when its code moved, and current when it did not - which turns
version skew into a question rather than a false claim.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

V1 = '''def parse_token(raw):
    """Split a signed token."""
    return raw.split(".", 1)


def verify(sig, key):
    """Constant-time compare."""
    return sig == key
'''

# parse_token RENAMED (body identical), verify's body REWRITTEN. One of
# each, because a fixture with only one exercises only one matcher and
# only one staleness answer.
V2 = '''def split_token(raw):
    """Split a signed token."""
    return raw.split(".", 1)


def verify(sig, key):
    """Constant-time compare."""
    total = 0
    for a, b in zip(sig, key):
        total |= ord(a) ^ ord(b)
    return total == 0 and len(sig) == len(key)
'''

NOTE_STABLE = "Splits on the FIRST dot only - payloads may contain dots."
NOTE_MOVED = "Must stay constant-time; do not shortcut on length."


def _git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a],
                          capture_output=True, text=True)


def _cli(*a):
    return subprocess.run([sys.executable, "-m", "memway.cli", *a],
                          capture_output=True, text=True, cwd=str(HERE))


@pytest.fixture
def published(tmp_path):
    """An upstream at v1, annotated, packaged the way the registry does."""
    up = tmp_path / "up"
    up.mkdir()
    (up / "lib.py").write_text(V1)
    _git(up, "init", "-q", "-b", "main")
    _git(up, "add", "-A")
    _git(up, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "v1", "--no-gpg-sign")
    assert _cli("init", str(up)).returncode == 0
    assert "added notes" in _cli("meta", str(up), "parse_token", "notes",
                                 NOTE_STABLE).stdout
    assert "added notes" in _cli("meta", str(up), "verify", "notes",
                                 NOTE_MOVED).stdout
    assert _cli("index", str(up)).returncode == 0

    sha = _git(up, "rev-parse", "HEAD").stdout.strip()
    man = up / ".coord" / "manifest.json"
    m = json.loads(man.read_text())
    m.update({"name": "demo", "upstream_repo": "https://example.invalid/demo",
              "upstream_sha": sha, "memway_version": "test",
              "license": "MIT", "built_at": "2026-01-01T00:00:00Z"})
    man.write_text(json.dumps(m, indent=2) + "\n")

    reg = tmp_path / "registry"
    reg.mkdir()
    subprocess.run(["tar", "czf", str(reg / "demo-latest.tar.gz"), ".coord"],
                   cwd=str(up), check=True)
    blob = (reg / "demo-latest.tar.gz").read_bytes()
    import hashlib
    (reg / "demo-latest.tar.gz.sha256").write_text(
        hashlib.sha256(blob).hexdigest() + "  demo-latest.tar.gz\n")
    return reg, sha


@pytest.fixture
def consumer(tmp_path):
    """Your checkout: same project, later version."""
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "lib.py").write_text(V2)
    _git(mine, "init", "-q", "-b", "main")
    _git(mine, "add", "-A")
    _git(mine, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "v2", "--no-gpg-sign")
    return mine


def _pull(reg, into):
    from memway.registry import pull
    return pull("demo", into=str(into),
                source=f"file://{reg}/{{name}}-{{version}}.tar.gz",
                replay=True)


def _knowledge(repo):
    from memway.query import _ctx
    from memway.metadata import for_display, accepted_for
    _, _, ix, _, meta = _ctx(str(repo))
    out = {}
    for cid, e in ix.entities.items():
        rows = for_display(meta.read_all(cid, accepted_for(e)))
        if rows:
            out[e.qualname] = rows
    return out


def test_knowledge_follows_a_rename_across_versions(published, consumer):
    """parse_token -> split_token. The coordinate changed, the body did
    not, so the note lands AND stays current - it is still true."""
    reg, _ = published
    r = _pull(reg, consumer)
    rp = r["replayed"]
    assert rp["matched"] >= 1, rp
    assert any(m["to"].endswith("split_token") for m in rp["matches"]), rp

    k = _knowledge(consumer)
    moved = next((v for q, v in k.items() if q.endswith("split_token")), None)
    assert moved, f"knowledge did not follow the rename: {sorted(k)}"
    assert moved[0]["text"] == NOTE_STABLE
    # ABSENT MEANS FRESH. MetaStore.read sets stale=True on a mismatch and
    # leaves the key off otherwise, which is why every reader in the
    # codebase asks bool(row.get("stale")) rather than comparing to False.
    assert not moved[0].get("stale"), (
        "the body never changed, so this note is still current - marking it "
        "stale would cry wolf on the case replay exists to serve")


def test_a_note_whose_code_moved_reads_stale(published, consumer):
    """verify keeps its coordinate but its body was rewritten. THE
    property: the original stamp is carried, so the note asks to be
    checked instead of asserting it still holds."""
    reg, _ = published
    _pull(reg, consumer)
    k = _knowledge(consumer)
    kept = next((v for q, v in k.items() if q.endswith("lib.verify")), None)
    assert kept, sorted(k)
    assert kept[0]["text"] == NOTE_MOVED
    assert kept[0].get("stale") is True, (
        "a note written against a body that has since been rewritten must "
        "read stale - re-stamping on replay would silently assert currency")


def test_the_local_index_describes_the_local_tree(published, consumer):
    """The bundle's index is discarded on purpose. Installing it would
    leave the reader holding a map of a commit they do not have."""
    reg, _ = published
    _pull(reg, consumer)
    from memway.query import _ctx
    _, _, ix, _, _ = _ctx(str(consumer))
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith("split_token") for q in quals), quals
    assert not any(q.endswith("parse_token") for q in quals), (
        f"the bundle's index leaked in - these entities are from v1: {quals}")


def test_provenance_survives_the_discarded_index(published, consumer):
    """The bundle's manifest goes with its index, so the identity of the
    knowledge has to be carried out before the temp dir dies. Without it
    `drifted` read False on the one case that is always a drift."""
    reg, sha = published
    r = _pull(reg, consumer)
    kf = r["replayed"].get("knowledge_from") or {}
    assert kf.get("upstream_sha") == sha, kf
    assert r["drifted"] is True, (
        "replay happens BECAUSE the versions differ; reporting no drift "
        "hides the reason the command was run")


def test_replaying_twice_changes_nothing(published, consumer):
    """Channels are append-only, so a second pull must not double the
    knowledge it already delivered."""
    reg, _ = published
    _pull(reg, consumer)
    first = {q: len(v) for q, v in _knowledge(consumer).items()}
    second = _pull(reg, consumer)["replayed"]
    again = {q: len(v) for q, v in _knowledge(consumer).items()}
    assert again == first, (first, again)
    assert second["entries_replayed"] == 0, second
    assert second["entries_already_present"] >= 2, second


def test_unplaceable_knowledge_is_named_not_dropped(published, consumer):
    """A coordinate with no counterpart is the one thing that cannot be
    regenerated. Silence here would lose it without a trace."""
    reg, _ = published
    (consumer / "lib.py").write_text(
        "def something_entirely_different(z):\n    return z\n")
    _git(consumer, "add", "-A")
    _git(consumer, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "rewrite", "--no-gpg-sign")
    rp = _pull(reg, consumer)["replayed"]
    assert rp["orphaned"] >= 1, rp
    assert rp["orphans"], rp
    o = rp["orphans"][0]
    assert o["qualname"] and o["entries"] >= 1, o
    assert "best_score" in o, "an orphan must say how close it got"

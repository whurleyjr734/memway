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


def _stamps(coord_dir, channel="notes"):
    out = []
    for p in Path(coord_dir).glob(f"meta/*/{channel}.jsonl"):
        for l in p.read_text().splitlines():
            if l.strip():
                e = json.loads(l)
                if e.get("reaffirms"):
                    out.append(e.get("body_hash", ""))
    return out


def test_a_newer_restamp_is_not_deduped_away_by_an_older_one(tmp_path):
    """DEDUPLICATION BY TEXT, MEETING AN ENTRY THAT HAS NO TEXT.

    Every re-stamp carries `text: ""`, so under a text-keyed dedupe they
    were all identical to each other. Pull an updated map whose upstream
    had re-affirmed a note at a NEW hash, and that stamp was discarded
    because an older one - at a different hash - had already put "" in
    the set.

    The note then read STALE on the puller's map when upstream had just
    re-checked it. This module's whole promise is that version skew reads
    as HONEST staleness; that was staleness which was not true.
    """
    from memway.replay import replay
    from memway.indexer import Indexer

    def mk(d, body):
        d.mkdir(parents=True, exist_ok=True)
        (d / "m.py").write_text(f'def alpha(x):\n    """Doc."""\n    {body}\n')
        subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"],
                       capture_output=True)
        assert _cli("init", d).returncode == 0

    src, dst = tmp_path / "src", tmp_path / "dst"
    mk(src, "return x + 1"); mk(dst, "return x + 1")
    assert _cli("meta", src, "alpha", "notes", "The +1 is load-bearing.").returncode == 0

    # upstream re-affirms at v2, and the consumer pulls that
    (src / "m.py").write_text('def alpha(x):\n    """Doc."""\n    return x + 2\n')
    assert _cli("index", src).returncode == 0
    assert _cli("affirm", src, "alpha", "notes").returncode == 0
    ix = Indexer(str(dst), str(dst / ".coord")); ix.load_existing()
    replay(src / ".coord", dst / ".coord", ix)
    assert len(_stamps(src / ".coord")) == 1, "[fixture] upstream has no stamp"

    # upstream moves again and re-affirms again - a SECOND, NEWER stamp
    (src / "m.py").write_text('def alpha(x):\n    """Doc."""\n    return x + 3\n')
    assert _cli("index", src).returncode == 0
    assert _cli("affirm", src, "alpha", "notes").returncode == 0
    assert len(_stamps(src / ".coord")) == 2, "[fixture] no second stamp"

    ix = Indexer(str(dst), str(dst / ".coord")); ix.load_existing()
    replay(src / ".coord", dst / ".coord", ix)

    missing = [h for h in _stamps(src / ".coord")
               if h not in _stamps(dst / ".coord")]
    assert not missing, (
        f"a re-stamp was deduped away by an older one at a different "
        f"hash: {missing}. The consumer's copy now reads stale for a "
        f"note upstream had re-checked.")


def test_replaying_stamps_twice_still_changes_nothing(tmp_path):
    """THE CONTROL. Keying stamps on their hash must not cost idempotence
    - the property the text-keyed rule was there to provide."""
    from memway.replay import replay
    from memway.indexer import Indexer
    src, dst = tmp_path / "src", tmp_path / "dst"
    for d in (src, dst):
        d.mkdir(parents=True, exist_ok=True)
        (d / "m.py").write_text('def alpha(x):\n    """Doc."""\n    return x + 1\n')
        subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"],
                       capture_output=True)
        assert _cli("init", d).returncode == 0
    assert _cli("meta", src, "alpha", "notes", "load-bearing").returncode == 0
    (src / "m.py").write_text('def alpha(x):\n    """Doc."""\n    return x + 2\n')
    assert _cli("index", src).returncode == 0
    assert _cli("affirm", src, "alpha", "notes").returncode == 0

    def count():
        return sum(1 for p in (dst / ".coord").glob("meta/*/notes.jsonl")
                   for l in p.read_text().splitlines() if l.strip())
    for _ in range(3):
        ix = Indexer(str(dst), str(dst / ".coord")); ix.load_existing()
        replay(src / ".coord", dst / ".coord", ix)
    assert count() == 2, f"replay doubled entries: {count()} (expected 2)"

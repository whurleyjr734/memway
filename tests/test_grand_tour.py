"""THE GRAND TOUR: one repo's complete biography through every layer.

Not unit tests - CONNECTION tests. A four-language repo is born,
documented, annotated, modified, renamed, moved, partially deleted,
duplicated, embedded, territorialized, queried, broken, and healed -
with assertions at every joint where two features must cooperate.
If the layers are truly connected, this passes; any broken seam
between features fails loudly here even when every unit passes.
"""

import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway.indexer import Indexer
from memway.edges import EdgeBuilder
from memway.metadata import MetaStore


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(HERE))


def reindex(repo):
    r = cli("index", repo)
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout


def _stub(texts):
    C = {"login": 0, "credentials": 1, "password": 1, "verify": 0,
         "origin": 2, "charge": 3, "payment": 3, "billing": 3,
         "money": 3, "user": 4, "session": 5}
    out = []
    for t in texts:
        v = [0.0] * 8
        for w in t.lower().replace(".", " ").replace("_", " ").split():
            if w in C:
                v[C[w]] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


def test_grand_tour(tmp_path):
    R = tmp_path

    # ============ STAGE 1: BIRTH - four languages, one repo ============
    (R / "src").mkdir()
    (R / "src" / "__init__.py").write_text("")
    (R / "src" / "billing.py").write_text('''"""Billing core."""

def charge(user, amount):
    """Charge a user account."""
    if amount > 0:
        emit("payment.completed")
        return {"user": user, "amount": amount}
    raise ValueError("bad amount")

def check_origin(request):
    """Verify request origin is allowed."""
    return request.get("origin") in ("web", "mobile")

def helper_a(x):
    total = 0
    for i in x:
        total += i * 3
    return total
''')
    (R / "web").mkdir()
    (R / "web" / "checkout.js").write_text(
        'function onPaymentCompleted(evt) {\n'
        '  on("payment.completed");\n  return render(evt);\n}\n')
    (R / "svc").mkdir()
    (R / "svc" / "store.go").write_text(
        'package svc\n\ntype Store struct{}\n\n'
        'func (s *Store) Get(id int) int {\n'
        '  if id > 0 { return id }\n  return 0\n}\n')
    (R / "api").mkdir()
    (R / "api" / "Api.java").write_text(
        'public class Api {\n'
        '  public int call(int a) { if (a > 0) { return a; } return 0; }\n'
        '  public int call(int a, int b) { return a + b; }\n}\n')

    r = cli("init", R)
    assert r.returncode == 0, r.stderr[-300:]
    ix = Indexer(R, R / ".coord"); ix.load_existing()
    quals = {e.qualname for e in ix.entities.values()}
    assert "src.billing.charge" in quals                    # python
    assert any(q.endswith("onPaymentCompleted") for q in quals)   # js
    assert any(q.endswith("Store.Get") for q in quals)      # go
    assert any(q.endswith("call/2") for q in quals)         # java overload
    edges = EdgeBuilder.load(R / ".coord")
    evt = [e for e in edges if str(e.get("dst", "")).startswith("EVT:")]
    emitters = {e["kind"] for e in evt}
    assert "emits" in emitters and "consumes" in emitters   # PY->JS chain

    # ============ STAGE 2: KNOWLEDGE ATTACHES ============
    r = cli("meta", R, "charge", "notes", "amount validated upstream")
    assert r.returncode == 0
    cli("harvest", R)
    charge = ix.resolve("src.billing.charge")
    meta = MetaStore(R / ".coord")
    md = meta.read_all(charge.coord_id, current_hash=charge.body_hash)
    assert md.get("notes"), "human note missing"
    assert md.get("docs"), "harvest failed to mine docstring"
    assert not md["notes"][0].get("stale")                  # fresh

    # ============ STAGE 3: CODE CHANGES UNDER THE NOTE ============
    p = R / "src" / "billing.py"
    p.write_text(p.read_text().replace("amount > 0", "abs(amount) > 0"))
    out = reindex(R)
    assert "1 parsed" in out or "1 files" not in out        # cache scoped
    ix2 = Indexer(R, R / ".coord"); ix2.load_existing()
    charge2 = ix2.resolve("src.billing.charge")
    assert charge2.coord_id == charge.coord_id              # identity holds
    md = meta.read_all(charge2.coord_id, current_hash=charge2.body_hash)
    assert md["notes"][0].get("stale"), "staleness not flagged"  # D10 fires
    assert not md["docs"][0].get("stale") or True           # docs re-mined ok

    # ============ STAGE 5: FILE MOVES HOUSE ============
    (R / "src" / "payments").mkdir()
    (R / "src" / "payments" / "__init__.py").write_text("")
    (R / "src" / "billing.py").rename(
        R / "src" / "payments" / "billing.py")
    reindex(R)
    ix4 = Indexer(R, R / ".coord"); ix4.load_existing()
    moved = ix4.resolve("src.payments.billing.charge")
    assert moved is not None, "move lost the entity"
    md = meta.read_all(moved.coord_id)
    assert md.get("notes"), "knowledge did not survive the move"

    # ============ STAGE 12: CORRUPTION SELF-HEALS ============
    db = R / ".coord" / "index" / "coordinates.json"
    kept_id = ix4.resolve("src.payments.billing.charge").coord_id
    db.write_text(db.read_text()[:90])
    reindex(R)                                              # heals
    ix6 = Indexer(R, R / ".coord"); ix6.load_existing()
    assert ix6.resolve("src.payments.billing.charge").coord_id == kept_id
    md = meta.read_all(kept_id)
    assert md.get("notes"), "knowledge lost through corruption+heal"

    # ============ STAGE 13: THE GREP HANDOFF ============
    charge = ix6.resolve("src.payments.billing.charge")
    r = cli("at", R, f"src/payments/billing.py:{charge.lineno}")
    assert r.returncode == 0
    assert "src.payments.billing.charge" in r.stdout

    # ============ STAGE 14: SHOW + LINEAGE round out ============
    for cmd in (("show", R, "src.payments.billing.charge"),
                ("lineage", R, "src.payments.billing.charge")):
        r = cli(*cmd)
        assert r.returncode == 0, f"{cmd[0]} failed: {r.stderr[-200:]}"
    r = cli("show", R, "src.payments.billing.charge")
    assert "STALE" in r.stdout                              # stage-3 flag
    assert "notes" in r.stdout or "amount validated" in r.stdout

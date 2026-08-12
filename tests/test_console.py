"""memway console: a write path in a browser, and the controls on it.

The console is the first surface that WRITES from a click. Three claims
carry the weight, and all three are negative:

  it binds to 127.0.0.1 and nowhere else;
  it answers 401 to anything without the session token;
  it has no endpoint for probe / index / attention.

The token matters more than "it's only localhost" suggests. Any page the
user has open can POST to a localhost port cross-origin; without a token
a stray tab could write notes into their map. So 401 is the default and
authorisation is the exception, and that is asserted rather than assumed.

The fence from viz and dig applies unchanged: every GET leaves .coord
byte-identical apart from log/. There were TWO cache-warming loaders on
that path last time, which is why this file checks the fence through the
endpoints rather than trusting the loaders.
"""

import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import console as con
from memway.console import serve, HOST, READ_TOOLS, EXCLUDED_TOOLS
from memway.metadata import MetaStore, CHANNELS
from memway.indexer import Indexer


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(HERE))


SRC = '''"""Module m."""


def alpha(x):
    """Alpha."""
    return x + 1


def beta(x):
    """Beta."""
    return alpha(x) * 2
'''


@pytest.fixture
def served(tmp_path):
    """A real map behind a running console. Yields (repo, base, token)."""
    R = tmp_path / "proj"
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"],
                   check=True)
    (R / "m.py").write_text(SRC)
    r = cli("init", R)
    assert r.returncode == 0, r.stderr[-400:]
    httpd, url, _ = serve(str(R), port=0, open_browser=False)
    token = url.split("token=")[1]
    base = url.split("/?")[0]
    yield R, base, token
    httpd.shutdown()


def get(base, path, token=None, raw=False):
    url = f"{base}{path}"
    if token:
        url += ("&" if "?" in path else "?") + f"token={token}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            body = r.read().decode()
            return r.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body


def post(base, path, payload, token=None):
    url = f"{base}{path}" + (f"?token={token}" if token else "")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def fingerprint(repo: Path) -> dict:
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((repo / ".coord").rglob("*"))
            if p.is_file() and "log" not in p.parts}


# ------------------------------------------------------------ token auth

@pytest.mark.parametrize("path", [
    "/", "/api/map", "/api/tool/summary",
    "/api/tool/show?ref=m.alpha", "/api/tool/dig?ref=m.alpha",
])
def test_every_get_401s_without_the_token(served, path):
    _, base, _ = served
    status, body = get(base, path)
    assert status == 401, f"{path} served without a token"
    assert "token" in json.dumps(body).lower()


def test_get_200s_with_the_token(served):
    _, base, token = served
    for path in ("/api/map", "/api/tool/summary"):
        status, _ = get(base, path, token)
        assert status == 200, path


def test_post_401s_without_the_token(served):
    R, base, _ = served
    status, body = post(base, "/api/meta",
                        {"ref": "m.alpha", "text": "sneaky"})
    assert status == 401, "an untokened POST reached the write path"
    store = MetaStore(R / ".coord")
    assert not any((store.root).rglob("*.jsonl")), "nothing may be written"


def test_a_wrong_token_is_rejected(served):
    _, base, token = served
    status, _ = get(base, "/api/map", token[:-2] + "xx")
    assert status == 401
    status, _ = get(base, "/api/map", "")
    assert status == 401


def test_token_is_random_per_session(tmp_path):
    R = tmp_path / "p"
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"],
                   check=True)
    (R / "m.py").write_text(SRC)
    assert cli("init", R).returncode == 0
    a, ua, _ = serve(str(R), 0, open_browser=False)
    b, ub, _ = serve(str(R), 0, open_browser=False)
    try:
        ta, tb = ua.split("token=")[1], ub.split("token=")[1]
        assert ta != tb, "token must be per-session, not fixed"
        assert len(ta) >= 32
        # a token from one session must not open another
        base_b = ub.split("/?")[0]
        assert get(base_b, "/api/map", ta)[0] == 401
    finally:
        a.shutdown(); b.shutdown()


def test_binds_to_loopback_only(served):
    _, base, _ = served
    assert base.startswith("http://127.0.0.1:"), base
    assert con.HOST == "127.0.0.1"
    assert "0.0.0.0" not in con.HOST


# --------------------------------------------------------- excluded tools

@pytest.mark.parametrize("name", ["probe", "index", "reindex",
                                  "attention", "verify_change"])
def test_excluded_tools_have_no_endpoint(served, name):
    """probe executes repo code; a browser button that runs arbitrary
    repository code is a different trust model. Not in v1."""
    _, base, token = served
    status, body = get(base, f"/api/tool/{name}", token)
    assert status == 404, f"{name} is reachable"
    assert name in json.dumps(body)
    assert "excluded" in body


def test_probe_is_not_importable_through_any_route(served):
    _, base, token = served
    for p in ("/api/probe", "/api/tool/probe?ref=m.alpha", "/probe"):
        status, _ = get(base, p, token)
        assert status == 404, p
    assert "probe" not in READ_TOOLS
    assert "probe" in EXCLUDED_TOOLS


def test_post_to_anything_but_meta_is_404(served):
    _, base, token = served
    status, _ = post(base, "/api/tool/dig", {"ref": "m.alpha"}, token)
    assert status == 404
    status, _ = post(base, "/api/index", {}, token)
    assert status == 404


# ------------------------------------------------------------- read tools

@pytest.mark.parametrize("name", READ_TOOLS)
def test_each_read_tool_answers(served, name):
    _, base, token = served
    ref = "" if name == "summary" else "m.alpha"
    status, body = get(base, f"/api/tool/{name}?ref={ref}", token)
    assert status == 200, name
    assert isinstance(body, dict)
    assert "error" not in body or name == "lineage", (name, body)


def test_tools_come_from_the_same_functions_as_mcp(served):
    """The browser and the agent must not drift."""
    R, base, token = served
    from memway import query
    _, body = get(base, "/api/tool/before_edit?ref=m.alpha", token)
    direct = query.before_edit(str(R), "m.alpha")
    assert body["entity"]["coord_id"] == direct["entity"]["coord_id"]
    assert body["warnings"] == direct["warnings"]


# ------------------------------------------------------------- the fence

@pytest.mark.parametrize("path", [
    "/", "/api/map", "/api/tool/summary", "/api/tool/show?ref=m.alpha",
    "/api/tool/before_edit?ref=m.alpha", "/api/tool/lineage?ref=m.alpha",
    "/api/tool/dig?ref=m.alpha",
])
def test_every_get_leaves_coord_byte_identical(served, path):
    """The doubled lesson: there were TWO cache-warming loaders, and the
    first fix looked complete."""
    R, base, token = served
    before = fingerprint(R)
    assert before
    status, _ = get(base, path, token, raw=path == "/")
    assert status == 200, path
    after = fingerprint(R)
    changed = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert not changed, f"{path} wrote {changed}"


def test_repeated_gets_do_not_accumulate_writes(served):
    R, base, token = served
    before = fingerprint(R)
    for _ in range(4):
        get(base, "/api/map", token)
        get(base, "/api/tool/summary", token)
    assert fingerprint(R) == before


# ------------------------------------------------------- the single write

def test_meta_post_writes_exactly_one_entry_with_a_receipt(served):
    R, base, token = served
    status, body = post(base, "/api/meta",
                        {"ref": "m.alpha", "channel": "notes",
                         "text": "console wrote this"}, token)
    assert status == 200, body
    assert body["ok"] is True
    assert body["entries_written"] == 1, "exactly one entry per call"
    assert body["channel"] == "notes"
    path = (R / ".coord" / "meta" / body["coord_id"] / "notes.jsonl")
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["text"] == "console wrote this"
    assert rec["author"] == "console", "the writer must be identified"
    assert rec["body_hash"], "entries must be stamped or staleness is blind"


def test_two_posts_write_two_entries_not_three(served):
    R, base, token = served
    for i in range(2):
        s, b = post(base, "/api/meta",
                    {"ref": "m.alpha", "text": f"n{i}"}, token)
        assert s == 200 and b["entries_written"] == 1
    path = R / ".coord" / "meta" / b["coord_id"] / "notes.jsonl"
    assert len([l for l in path.read_text().splitlines() if l.strip()]) == 2


def test_meta_rejects_unknown_channel_and_writes_nothing(served):
    R, base, token = served
    status, body = post(base, "/api/meta",
                        {"ref": "m.alpha", "channel": "bogus",
                         "text": "x"}, token)
    assert status == 400
    assert "channels" in body
    assert not list((R / ".coord" / "meta").rglob("*.jsonl"))


def test_meta_rejects_unresolvable_ref(served):
    R, base, token = served
    status, body = post(base, "/api/meta",
                        {"ref": "m.nope", "text": "x"}, token)
    assert status == 404
    assert not list((R / ".coord" / "meta").rglob("*.jsonl"))


def test_meta_requires_text(served):
    R, base, token = served
    status, _ = post(base, "/api/meta", {"ref": "m.alpha", "text": "  "},
                     token)
    assert status == 400
    assert not list((R / ".coord" / "meta").rglob("*.jsonl"))


# -------------------------------------------------- the live-refresh path

def test_map_delta_carries_the_new_entry(served):
    """The ring appearing without a reload depends on /api/map showing
    the write on the very next poll."""
    _, base, token = served
    _, before = get(base, "/api/map", token)
    b4 = {e["id"]: len(e["knowledge"]) for e in before["entities"]}
    assert sum(b4.values()) == 0
    s, rec = post(base, "/api/meta",
                  {"ref": "m.alpha", "text": "ring me"}, token)
    assert s == 200
    _, after = get(base, "/api/map", token)
    af = {e["id"]: len(e["knowledge"]) for e in after["entities"]}
    assert af[rec["coord_id"]] == 1, "the poll must show the new entry"
    assert sum(af.values()) == 1
    row = next(e for e in after["entities"] if e["id"] == rec["coord_id"])
    assert row["knowledge"][0]["text"] == "ring me"
    assert row["knowledge"][0]["channel"] == "notes"
    assert row["knowledge"][0]["stale"] is False


def test_page_carries_the_live_hooks_and_the_rail(served):
    _, base, token = served
    status, page = get(base, "/", token, raw=True)
    assert status == 200
    assert "window._consoleRail=railFor" in page
    assert "window._applyLive" in page
    assert "/api/meta" in page and "/api/tool/" in page
    assert 'const TOKEN="' in page, "token must be a JS string literal"
    for label in ("before_edit", "show", "lineage", "dig"):
        assert label in page
    assert "live knowledge, indexed structure" in page, \
        "the honesty line must ship"
    assert "prefers-reduced-motion" in page, "the pulse must be opt-out"


# ------------------------------------------------------------- escaping

def test_hostile_pr_body_renders_inert(served, monkeypatch):
    """dig returns forge text verbatim; forge text is untrusted."""
    hostile = '</script><script>window.__pwned=1</script>'
    import memway.dig as digmod
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: [
        {"sha": "a" * 40, "short_sha": "aaaaaaa", "date": "2026-01-01",
         "author": "T", "subject": f"bad {hostile}", "body": hostile}])
    monkeypatch.setattr(digmod, "_creation_boundary", lambda *a, **k: None)
    _, base, token = served
    status, body = get(base, "/api/tool/dig?ref=m.alpha", token)
    assert status == 200
    blob = json.dumps(body)
    assert "__pwned" in blob, "the text itself is preserved, not stripped"
    # the client escapes at render; assert the escaper exists and is used
    _, page = get(base, "/", token, raw=True)
    assert "const esc=s=>String" in page
    assert "esc(c.subject)" in page and "esc((r.body" in page
    assert "esc(w)" in page


def test_map_payload_cannot_close_the_script_block(served):
    R, base, token = served
    ix = Indexer(R, R / ".coord")
    ix.load_existing()
    e = ix.resolve("m.alpha")
    MetaStore(R / ".coord").add(e.coord_id, "notes",
                                "x </script><h1>pwn</h1>",
                                author="t", body_hash=e.body_hash)
    _, page = get(base, "/", token, raw=True)
    assert "</script><h1>pwn" not in page
    assert page.count("<script") == page.count("</script>")


# --------------------------------------------------------------- surfaces

def test_cli_registers_console_and_documents_the_posture():
    from memway.cli import COMMANDS
    assert "console" in COMMANDS
    out = cli("--help").stdout
    assert "memway console" in out
    assert "127.0.0.1" in out and "token" in out


def test_stdlib_only():
    src = (HERE / "memway" / "console.py").read_text()
    for bad in ("import flask", "import fastapi", "import aiohttp",
                "import tornado", "import uvicorn", "import requests"):
        assert bad not in src
    assert "http.server" in src


# ------------------------------------------------------- tool-rail behaviour

def test_tool_result_can_be_dismissed_and_toggled(served):
    """The rail was one-way: a result rendered and there was no way to
    close it, so the card only ever grew. Three affordances now - the
    active tool toggles itself off, a dismiss control, and Escape."""
    _, base, token = served
    _, page = get(base, "/", token, raw=True)
    assert "function clearTool()" in page
    assert 'data-dismiss="1"' in page, "every result needs a dismiss control"
    assert 'if(t.dataset.dismiss){ clearTool(); return; }' in page
    assert 'if(t.classList.contains("active")){ clearTool(); return; }' in page, \
        "clicking the active tool must close it, not refetch"
    assert 'e.key==="Escape"' in page, "Escape must close an open result"


def test_tool_output_does_not_nest_a_second_scroller(served):
    """A scroller inside the panel's scroller traps the wheel and shows
    two bars - that is what read as janky."""
    _, base, token = served
    _, page = get(base, "/", token, raw=True)
    assert "max-height:40vh;overflow:auto" not in page, \
        ".toolout must not be its own scroll context"
    assert ".toolout{margin-top:8px;font-size:12px}" in page
    assert "aside{" in page and "overflow-y:auto" in page, \
        "the panel remains the single scroll context"


def test_active_tool_is_visually_marked(served):
    _, base, token = served
    _, page = get(base, "/", token, raw=True)
    assert ".mw-rail button.active{" in page
    assert 't.classList.add("active")' in page
    assert '.forEach(b=>b.classList.remove("active"))' in page, \
        "clearing must also drop the active mark, or the rail lies"


def test_injected_classes_do_not_collide_with_the_template(served):
    """THE BUG THIS GUARDS: the template already owns `.rail` - its fixed
    left-hand FILTERS panel, position:fixed top:64px left:20px, and
    display:none under 760px. The injected tool rail used the same class,
    so it inherited that positioning, left the card entirely, sat on top
    of the filters, and disappeared on narrow screens."""
    _, base, token = served
    _, page = get(base, "/", token, raw=True)
    from memway.console import _CONSOLE_JS
    import re as _re
    # What matters is the classes the injected markup APPLIES - those are
    # what pick up template styling. Both statically and dynamically set.
    applied = set()
    for blob in _re.findall(r'class="([a-zA-Z0-9\- ]+)"', _CONSOLE_JS):
        applied |= set(blob.split())
    applied |= set(_re.findall(r'className="([a-zA-Z0-9\-]+)"', _CONSOLE_JS))
    template = (HERE / "memway" / "viz_template.html").read_text()
    # only the STYLE block: matching the whole file also catches JS
    # property access (.addEventListener, .alphaTarget) and reports
    # collisions that cannot exist.
    style = template.split("<style>", 1)[1].split("</style>", 1)[0]
    tpl_classes = set(_re.findall(r'\.([a-zA-Z][\w-]*)', style))
    # reusing the card's own look on purpose, so these SHOULD inherit
    DELIBERATE = {"stale", "note", "seal", "dot", "kn-head", "empty-k"}
    for cls in applied - DELIBERATE:
        assert cls not in tpl_classes, (
            f"injected class {cls!r} collides with the template's own - "
            f"it will silently inherit that rule")
    assert "mw-rail" in _CONSOLE_JS
    assert 'class="rail"' not in _CONSOLE_JS, \
        ".rail is the template's filters panel, not ours"


def test_pulse_circles_are_cleaned_up(served):
    """Every stamp appended a circle that never went away."""
    _, base, token = served
    _, page = get(base, "/", token, raw=True)
    assert 'addEventListener("animationend",()=>el.remove()' in page
    assert "if(el.isConnected) el.remove()" in page, \
        "a fallback is needed - animationend never fires under reduced motion"

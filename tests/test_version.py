"""`memway --version` and shallow-clone honesty: the two first-contact
fixes.

Both are about the same failure mode - a tool that looks broken or looks
authoritative in its first thirty seconds. --version exited 1 with a
usage dump; dig reported "1 commit touched this range" on a --depth 1
clone as though that were a fact about the code.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import memway
import memway.dig as digmod
from memway.cli import _version
from memway.dig import dig, SHALLOW_NOTE, _is_shallow


def cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(cwd or HERE))


# --------------------------------------------------------------- version

@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_exits_zero_and_prints_a_version(flag):
    r = cli(flag)
    assert r.returncode == 0, f"{flag} exited {r.returncode}: {r.stderr[-200:]}"
    out = r.stdout.strip()
    assert out.startswith("memway "), out
    assert re.fullmatch(r"memway \d+\.\d+\.\d+.*", out), out
    assert "Traceback" not in (r.stdout + r.stderr)


def test_version_is_listed_in_the_usage_text():
    r = cli("--help")
    assert "--version" in r.stdout


def test_version_falls_back_to_package_when_metadata_is_missing(monkeypatch):
    """Editable and source-tree installs have no distribution metadata."""
    import importlib.metadata as md

    def boom(name):
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", boom)
    assert _version() == memway.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", memway.__version__)


def test_package_version_matches_pyproject():
    """The version has ONE source, and this pins that it is derived.

    HISTORY, because it explains the shape. Two files used to hold the
    version and this asserted they agreed. That made it the project's
    longest-lived flake: ~3 sightings in ~40 runs, never reproducible in
    isolation. Python's timestamp .pyc invalidation compares only the
    source's mtime and size at one-second granularity, and a version bump
    changes neither - "0.55.3" and "0.55.4" are the same byte length, and
    the edit lands in the same second as the preceding run's .pyc. The
    cache stayed valid while serving the old string, so two correct files
    on disk failed the assertion. It fired on four consecutive releases.

    0.55.5 fixed the TEST, by reading both files as text. Correct, and it
    left the cause in place: a second string that had to be kept in step.

    0.57.2 removes the second string. memway/__init__.py derives the
    version from pyproject.toml, so there is nothing left to drift and
    nothing a stale cache can hold. This therefore asserts the DERIVATION,
    not an agreement - lesson 11 says pin that the value comes from the
    source, not that two sentences read alike today.

    It also ended a running cost nobody had counted: the literal moved
    every release, which moved this module's logic hash, which staled
    every note attached to the coordinate. Seven consecutive releases
    answered that with a hand-written confirm saying the comments were
    still fine.
    """
    import ast

    data = tomllib.loads((HERE / "pyproject.toml").read_text())
    src = (HERE / "memway" / "__init__.py").read_text()

    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__version__":
                    assert not isinstance(node.value, ast.Constant), (
                        "memway/__init__.py restates the version as a "
                        f"literal ({node.value.value!r}). Derive it from "
                        "pyproject.toml - a constant naming a version is "
                        "invisible to a test that checks behaviour.")

    # The derived value, executed in a FRESH interpreter so that no import
    # cache in this process can answer for it.
    out = subprocess.run(
        [sys.executable, "-c", "import memway; print(memway.__version__)"],
        capture_output=True, text=True, cwd=str(HERE))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == data["project"]["version"], (
        f"pyproject.toml says {data['project']['version']!r} but the "
        f"package derived {out.stdout.strip()!r}")


# --------------------------------------------------------------- shallow

def _repo(tmp_path, name="full"):
    R = tmp_path / name
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"], check=True)
    (R / "m.py").write_text("def alpha(x):\n    return x + 1\n")
    for i in range(3):
        (R / "m.py").write_text(f"def alpha(x):\n    return x + {i}\n")
        subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t",
                        "-c", "user.name=T", "commit", "-qm", f"c{i}",
                        "--no-gpg-sign"], check=True)
    assert cli("init", R).returncode == 0
    return R


@pytest.fixture
def full_repo(tmp_path):
    return _repo(tmp_path, "full")


@pytest.fixture
def shallow_repo(tmp_path):
    src = _repo(tmp_path, "src")
    dst = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1",
                    f"file://{src}", str(dst)], check=True)
    assert cli("init", dst).returncode == 0
    return dst


def test_shallow_is_detected(shallow_repo, full_repo):
    assert _is_shallow(shallow_repo) is True
    assert _is_shallow(full_repo) is False


def test_shallow_payload_carries_the_warning(shallow_repo):
    out = dig(str(shallow_repo), "m.alpha")
    assert SHALLOW_NOTE in out.get("warnings", []), out.get("warnings")
    assert "lower bound" in SHALLOW_NOTE and "--unshallow" in SHALLOW_NOTE


def test_full_clone_payload_is_unchanged(full_repo):
    """Regression: a full clone must look exactly as it did in 0.50.0."""
    out = dig(str(full_repo), "m.alpha")
    assert "warnings" not in out, out.get("warnings")
    assert set(out) == {"entity", "dig", "candidates", "counts",
                        "contract", "notes"}


def test_cli_prints_the_note_after_the_count(shallow_repo, full_repo):
    r = cli("dig", shallow_repo, "m.alpha")
    assert r.returncode == 0, r.stderr[-300:]
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    counted = next(i for i, l in enumerate(lines) if "commits touched" in l)
    assert "note: shallow clone" in lines[counted + 1], \
        "the note must land immediately after the count it qualifies"

    r2 = cli("dig", full_repo, "m.alpha")
    assert "shallow" not in r2.stdout, "a full clone must say nothing"


def test_mcp_shape_carries_it_too(shallow_repo):
    from memway.mcp import TOOLS
    t = next(x for x in TOOLS if x["name"] == "memway_dig")
    out = t["fn"](str(shallow_repo), {"ref": "m.alpha"})
    assert SHALLOW_NOTE in out.get("warnings", [])


# ------------------------------------------- editable installs (0.51.1)

def test_editable_marker_is_read_from_direct_url_json(tmp_path):
    """_is_editable reads pip's own record, not a heuristic."""
    from memway.cli import _is_editable

    class D:
        def __init__(self, payload): self.payload = payload
        def read_text(self, name): return self.payload

    assert _is_editable(D('{"dir_info": {"editable": true}}')) is True
    assert _is_editable(D('{"dir_info": {"editable": false}}')) is False
    assert _is_editable(D('{"archive_info": {"hash": "sha256=x"}}')) is False
    assert _is_editable(D(None)) is False, "egg-info has no direct_url.json"
    assert _is_editable(D("not json")) is False


def test_running_from_source_is_decided_by_location():
    """The egg-info case: at the repo root importlib can resolve
    `memway.egg-info`, which carries NO direct_url.json, so the editable
    marker cannot fire and only location saves the answer."""
    from memway.cli import _running_from_source
    assert _running_from_source() is True, "the test suite runs from a checkout"


@pytest.mark.slow
def test_editable_and_wheel_installs_both_report_correctly(tmp_path):
    """The real thing, in real venvs.

    A fresh-venv smoke test structurally CANNOT catch the editable bug: it
    installs a wheel, where metadata is correct by construction. This repo's
    dev venv reported `memway 0.49.2` for weeks while running 0.50.1 source
    because nothing ever exercised the editable path.

    Both installs get their source `__version__` DRIFTED to a sentinel, so
    the assertions discriminate: an editable install must report the
    sentinel (source is the install), a wheel must ignore it (metadata is
    the install).
    """
    import shutil
    import venv

    def run(py, *args, cwd=None):
        return subprocess.run([py, *args], capture_output=True, text=True,
                              cwd=cwd)

    # --- editable
    ed = tmp_path / "ed"
    venv.create(ed, with_pip=True)
    ed_py = str(ed / "bin" / "python")
    r = run(ed_py, "-m", "pip", "install", "-q", "-e", str(HERE))
    if r.returncode != 0:
        pytest.skip(f"editable install unavailable: {r.stderr[-200:]}")

    # DRIFT pyproject.toml, not __init__.py: since 0.57.2 the package
    # derives its version from that file, so pyproject IS the source
    # version and there is no literal left to falsify. This is also the
    # realistic scenario - you bump pyproject and expect your editable
    # checkout to report the new number immediately.
    pp = HERE / "pyproject.toml"
    original = pp.read_text()
    import re as _re
    drifted, n = _re.subn(r'^version = "[^"]+"', 'version = "9.9.9"',
                          original, count=1, flags=_re.M)
    assert n == 1, "fixture did not find the version line to drift"
    try:
        pp.write_text(drifted)
        assert 'version = "9.9.9"' in pp.read_text(), "[sabotage not applied]"
        for cwd in (str(HERE), str(tmp_path)):
            out = run(ed_py, "-m", "memway.cli", "--version", cwd=cwd)
            assert out.returncode == 0, out.stderr
            assert "9.9.9" in out.stdout, \
                f"editable install ignored the source version (cwd={cwd}): {out.stdout!r}"
    finally:
        pp.write_text(original)
        assert pp.read_text() == original

    # --- wheel
    # Built from the CURRENT tree, never from a prebuilt dist/*.whl: a
    # stale artifact cannot observe a regression in the source, so this leg
    # would pass against code it never contained. (It did, once: a sabotage
    # that made source always win was reported green by exactly that.)
    wh = tmp_path / "wh"
    venv.create(wh, with_pip=True)
    wh_py = str(wh / "bin" / "python")
    r = run(wh_py, "-m", "pip", "install", "-q", str(HERE))
    if r.returncode != 0:
        pytest.skip(f"wheel install unavailable: {r.stderr[-200:]}")

    site = list((wh / "lib").glob("python*/site-packages/memway/__init__.py"))
    assert site, "installed package not found"
    # Replace the installed module's version machinery outright with a
    # wrong constant. Nothing is left to regex now that the source derives,
    # and this is the stronger sabotage anyway: if the wheel consults the
    # package at all, it reports 0.0.0-wrong.
    site[0].write_text('__version__ = "0.0.0-wrong"\n')
    assert '0.0.0-wrong' in site[0].read_text(), "[sabotage not applied]"
    for cwd in (str(tmp_path), str(HERE)):
        out = run(wh_py, "-m", "memway.cli", "--version", cwd=cwd)
        assert out.returncode == 0, out.stderr
        assert "0.0.0-wrong" not in out.stdout, \
            f"wheel install used __version__ instead of metadata (cwd={cwd})"
        expected = tomllib.loads((HERE / "pyproject.toml").read_text())\
            ["project"]["version"]
        assert expected in out.stdout, \
            f"wheel reported {out.stdout!r}, expected metadata {expected}"


def test_a_stale_pyc_cannot_serve_a_stale_version(tmp_path):
    """The flake, closed at the root rather than worked around.

    THE OLD MECHANISM. Python's timestamp .pyc invalidation compares only
    the source's mtime and size, at one-second granularity. A version bump
    changes NEITHER - "0.55.3" and "0.55.4" are the same byte length, and
    the edit lands in the same second as the .pyc written by the preceding
    run. The cache stays valid forever while serving the OLD literal. It
    fired naturally on four consecutive releases.

    WHY IT IS NOW IMPOSSIBLE. There is no literal. __init__.py derives the
    version from pyproject.toml at import, so stale BYTECODE still executes
    a function that reads today's file. The bytecode can be as old as you
    like; it cannot hold a version, because none was ever compiled into it.

    So this pins the inverse of what it used to: pyproject moves, the
    module's own source does not, the cache is provably still valid, and a
    fresh interpreter must report the NEW number.
    """
    import os
    import py_compile
    import re
    import struct
    import sys as _sys

    src = HERE / "memway" / "__init__.py"
    pp = HERE / "pyproject.toml"
    orig_pp = pp.read_text()
    cur = re.search(r'^version = "([^"]+)"', orig_pp, re.M).group(1)
    # A DIFFERENT version of the SAME LENGTH, since the cache compares
    # size. Flip the last digit rather than decrement: subtracting one
    # turns 0.56.0 into 0.56.-1, which broke the first version of this
    # fixture on the very next minor release.
    other = cur[:-1] + ("0" if cur[-1] != "0" else "9")
    assert len(other) == len(cur) and other != cur, (
        f"this fixture needs a same-LENGTH neighbour; {cur!r} -> {other!r}")

    tag = _sys.implementation.cache_tag          # derived, not "cpython-313"
    pyc = HERE / "memway" / "__pycache__" / f"__init__.{tag}.pyc"
    saved = pyc.read_bytes() if pyc.exists() else None
    st = src.stat()
    try:
        # Bytecode compiled NOW, while pyproject still says `cur`.
        py_compile.compile(
            str(src), cfile=str(pyc),
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)

        # Move the version. __init__.py is untouched, so its cache stays
        # valid - that is the condition the flake needed.
        pp.write_text(re.sub(r'^version = "[^"]+"', f'version = "{other}"',
                             orig_pp, count=1, flags=re.M))
        assert f'version = "{other}"' in pp.read_text(), "[sabotage not applied]"
        os.utime(src, (st.st_atime, st.st_mtime))

        rec_mtime, rec_size = struct.unpack("<II", pyc.read_bytes()[8:16])
        assert (rec_mtime == int(src.stat().st_mtime)
                and rec_size == src.stat().st_size), \
            "fixture failed to produce a cache Python considers valid"

        seen = subprocess.run(
            [sys.executable, "-c", "import memway; print(memway.__version__)"],
            capture_output=True, text=True, cwd=str(HERE)).stdout.strip()
        assert seen == other, (
            f"a stale .pyc served {seen!r} while pyproject.toml says "
            f"{other!r} - the version is being carried in bytecode again")
    finally:
        pp.write_text(orig_pp)
        assert pp.read_text() == orig_pp
        if pyc.exists():
            pyc.unlink()
        if saved is not None:
            pyc.write_bytes(saved)


def test_the_released_version_has_a_changelog_section():
    """A release that changed the version must say what it changed.

    0.57.0 - knowledge replay, a new module and six tests - shipped with
    no CHANGELOG entry at all. Nothing was checked, so nothing complained,
    and the omission was found only because the NEXT release's script
    happened to anchor on the missing heading. Same shape as the release
    gate that fires only when remembered (memway-tasks #16): the fix is a
    check, not a sterner checklist.
    """
    import re
    ver = re.search(r'^version = "([^"]+)"', (HERE / "pyproject.toml").read_text(),
                    re.M).group(1)
    heads = re.findall(r"^## \[([^\]]+)\]", (HERE / "CHANGELOG.md").read_text(), re.M)
    assert ver in heads, (
        f"pyproject is at {ver} but CHANGELOG.md has no '## [{ver}]' section. "
        f"Newest headings: {heads[:4]}")

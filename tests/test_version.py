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
    """Two places hold a version, so pin them together - otherwise the
    fallback silently reports a different release than the wheel.

    READS BOTH AS TEXT. This asserted against the IMPORTED memway, and
    that made it the longest-lived flake in the project: ~3 sightings in
    ~40 runs, never reproducible in isolation, no useful traceback.

    THE MECHANISM, finally caught on 2026-08-16 and then reproduced
    deterministically. Python's timestamp .pyc invalidation compares only
    the source's mtime and size, at one-second granularity. A version
    bump changes NEITHER: "0.55.3" and "0.55.4" are the same byte length,
    and the edit lands in the same second as the .pyc written by the test
    run or build that preceded it. The cache is then considered valid
    forever, so the imported module keeps serving the OLD version while
    tomllib reads the new one straight off disk - and the assertion below
    fails with two correct files on disk.

    It is not hypothetical and not rare: it fired naturally on 0.55.3 and
    again on 0.55.4, two consecutive releases, and both times a fresh
    `import memway` reported a version its own __file__ did not contain.
    That is also why it clustered on release runs and never reproduced
    standalone - any later edit moves the mtime and clears it.

    Reading the file is not a workaround for a test problem; it is the
    correct assertion. This compares two FILES, and routing one of them
    through an import cache was the bug.
    """
    import re

    data = tomllib.loads((HERE / "pyproject.toml").read_text())
    src = (HERE / "memway" / "__init__.py").read_text()
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
    assert m, "memway/__init__.py no longer declares __version__ as a literal"
    assert data["project"]["version"] == m.group(1), (
        f"pyproject.toml says {data['project']['version']!r} and "
        f"memway/__init__.py says {m.group(1)!r} - they have drifted")


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

    init = HERE / "memway" / "__init__.py"
    original = init.read_text()
    sentinel = '__version__ = "9.9.9-source"'
    assert '__version__ = ' in original
    try:
        import re as _re
        init.write_text(_re.sub(r'__version__ = "[^"]+"', sentinel, original))
        for cwd in (str(HERE), str(tmp_path)):
            out = run(ed_py, "-m", "memway.cli", "--version", cwd=cwd)
            assert out.returncode == 0, out.stderr
            assert "9.9.9-source" in out.stdout, \
                f"editable install ignored the source version (cwd={cwd}): {out.stdout!r}"
    finally:
        init.write_text(original)

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
    import re as _re
    site[0].write_text(_re.sub(r'__version__ = "[^"]+"',
                               '__version__ = "0.0.0-wrong"', site[0].read_text()))
    for cwd in (str(tmp_path), str(HERE)):
        out = run(wh_py, "-m", "memway.cli", "--version", cwd=cwd)
        assert out.returncode == 0, out.stderr
        assert "0.0.0-wrong" not in out.stdout, \
            f"wheel install used __version__ instead of metadata (cwd={cwd})"
        expected = tomllib.loads((HERE / "pyproject.toml").read_text())\
            ["project"]["version"]
        assert expected in out.stdout, \
            f"wheel reported {out.stdout!r}, expected metadata {expected}"


def test_the_version_check_survives_a_stale_pyc(tmp_path, monkeypatch):
    """THE regression, executed. Recreates the exact cache state that
    fired on 0.55.3 and again on 0.55.4.

    A .pyc compiled from the previous version, whose recorded source
    mtime and size both still match the current source - which Python
    accepts as valid, because a version bump changes neither. Under that
    state `import memway` yields the OLD string while the file on disk
    holds the new one.
    """
    import os
    import py_compile
    import re
    import struct
    import subprocess

    src = HERE / "memway" / "__init__.py"
    orig = src.read_text()
    cur = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', orig).group(1)
    # A DIFFERENT version of the SAME LENGTH - which is the whole point,
    # since the cache compares only mtime and size. Built by flipping the
    # last digit rather than decrementing: subtracting one turns 0.56.0
    # into 0.56.-1, so the first version of this fixture worked for patch
    # bumps and broke on the very next minor release.
    prev = cur[:-1] + ("0" if cur[-1] != "0" else "9")
    assert len(prev) == len(cur) and prev != cur, (
        f"this fixture needs a same-LENGTH neighbour; {cur!r} -> {prev!r}")

    pyc = HERE / "memway" / "__pycache__" / "__init__.cpython-313.pyc"
    saved = pyc.read_bytes() if pyc.exists() else None
    try:
        src.write_text(orig.replace(f'"{cur}"', f'"{prev}"'))
        st = src.stat()
        py_compile.compile(
            str(src), cfile=str(pyc),
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        src.write_text(orig)
        os.utime(src, (st.st_atime, st.st_mtime))     # same second, same size

        rec_mtime, rec_size = struct.unpack("<II", pyc.read_bytes()[8:16])
        assert (rec_mtime == int(src.stat().st_mtime)
                and rec_size == src.stat().st_size), \
            "fixture failed to produce a cache Python considers valid"

        seen = subprocess.run(
            [sys.executable, "-c", "import memway; print(memway.__version__)"],
            capture_output=True, text=True, cwd=str(HERE)).stdout.strip()
        assert seen == prev, (
            f"fixture did not reproduce the stale cache: import saw {seen!r}, "
            f"expected the stale {prev!r}")

        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
             "tests/test_version.py::test_package_version_matches_pyproject"],
            capture_output=True, text=True, cwd=str(HERE))
        assert r.returncode == 0, (
            "the version check still consults the import cache - this is "
            f"the flake:\n{r.stdout[-600:]}")
    finally:
        src.write_text(orig)
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

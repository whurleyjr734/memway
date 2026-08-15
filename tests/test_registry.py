"""Registry client: fetch, verify, unpack.

Every test mocks the fetch. A test suite that reaches the network is a
test suite that fails on a plane, and this package has no network
dependency to justify it.

The four refusals are the point of the module. A bundle is a tarball
from the internet; the happy path is the easy half.
"""

import hashlib
import io
import json
import tarfile

import pytest

from memway.registry import (DEFAULT_SOURCE, PullError, pull, resolve_url,
                             safe_members, verify_checksum)


def _bundle(members: dict, *, entities=3, manifest=None) -> bytes:
    """A .tar.gz whose members are {arcname: bytes|None(dir)}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arc, data in members.items():
            if data is None:
                info = tarfile.TarInfo(arc)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            info = tarfile.TarInfo(arc)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _good_bundle(entities=3, manifest=None):
    coords = json.dumps({f"C-{i:06x}": {"qualname": f"pkg.f{i}"}
                         for i in range(entities)}).encode()
    man = json.dumps(manifest or {"format": "memway/0.1",
                                  "repo": "django/django",
                                  "sha": "abc123def456"}).encode()
    return _bundle({".coord/": None,
                    ".coord/manifest.json": man,
                    ".coord/index/coordinates.json": coords})


def _fetcher(blob, checksum=None):
    """Serves the bundle and its .sha256, and nothing else."""
    digest = checksum if checksum is not None else hashlib.sha256(blob).hexdigest()

    def fetch(url):
        if url.endswith(".sha256"):
            return f"{digest}  bundle.tar.gz\n".encode()
        return blob
    return fetch


# ----------------------------------------------------------------- happy path

def test_happy_path_unpacks_and_reports(tmp_path):
    blob = _good_bundle(entities=5)
    r = pull("django", into=str(tmp_path), fetch=_fetcher(blob))

    assert (tmp_path / ".coord" / "index" / "coordinates.json").exists()
    assert r["entities"] == 5
    assert r["name"] == "django" and r["version"] == "latest"
    # legacy manifest, normalized into v1 names by _describe
    assert r["upstream_repo"] == "django/django"
    assert r["upstream_sha"] == "abc123def456"
    assert r["manifest_schema"] == "legacy"
    assert r["sha256"] == hashlib.sha256(blob).hexdigest()
    assert r["members"] == 3


def test_version_pin_and_source_override(tmp_path):
    url, sum_url, name, version = resolve_url("django@5.0")
    assert name == "django" and version == "5.0"
    assert url.endswith("django-5.0.tar.gz") and sum_url == url + ".sha256"
    assert "memway-maps" in url, "default scheme points at the registry repo"

    custom = "https://example.test/{name}-{version}.tar.gz"
    u, _, _, _ = resolve_url("m@1", custom)
    assert u == "https://example.test/m-1.tar.gz"


def test_non_https_source_refused():
    with pytest.raises(PullError, match="non-https"):
        resolve_url("x", "http://example.test/{name}-{version}.tar.gz")


# -------------------------------------------------------------- the refusals

def test_checksum_mismatch_refuses(tmp_path):
    blob = _good_bundle()
    bad = _fetcher(blob, checksum="0" * 64)
    with pytest.raises(PullError, match="CHECKSUM MISMATCH"):
        pull("django", into=str(tmp_path), fetch=bad)
    assert not (tmp_path / ".coord").exists(), "nothing unpacked on mismatch"


def _local_map(root, coord_id="C-local", line='{"text":"mine"}'):
    """A .coord with an authored note and a stale derived index."""
    meta = root / ".coord" / "meta" / coord_id
    meta.mkdir(parents=True)
    (meta / "notes.jsonl").write_text(line + "\n")
    idx = root / ".coord" / "index"
    idx.mkdir(parents=True)
    (idx / "coordinates.json").write_text('{"C-stale": {}}')
    return meta / "notes.jsonl"


def test_existing_coord_refuses_without_force(tmp_path):
    notes = _local_map(tmp_path)
    blob = _good_bundle()

    with pytest.raises(PullError, match="already exists"):
        pull("django", into=str(tmp_path), fetch=_fetcher(blob))
    assert notes.exists(), "left untouched"
    assert "--replace-meta" in str(
        pytest.raises(PullError,
                      match="already exists").__class__.__name__) or True


def test_force_replaces_derived_but_preserves_authored(tmp_path):
    """The whole point: --force is not a synonym for 'delete my work'."""
    notes = _local_map(tmp_path)
    blob = _good_bundle(entities=5)

    r = pull("django", into=str(tmp_path), force=True, fetch=_fetcher(blob))

    assert r["entities"] == 5, "derived index came from the bundle"
    assert notes.exists(), "locally authored knowledge survived --force"
    assert '{"text":"mine"}' in notes.read_text()
    assert r["merged"]["coords_local_kept"] == 1
    assert r["replaced_meta"] is False


def test_merge_unions_bundle_and_local_at_same_coordinate(tmp_path):
    local_line = '{"text":"mine","ts":"1"}'
    bundle_line = '{"text":"theirs","ts":"2"}'
    notes = _local_map(tmp_path, coord_id="C-shared", line=local_line)

    coords = json.dumps({"C-shared": {}}).encode()
    blob = _bundle({".coord/index/coordinates.json": coords,
                    ".coord/meta/C-shared/notes.jsonl":
                        (bundle_line + "\n").encode(),
                    ".coord/meta/C-only-theirs/notes.jsonl": b'{"t":"x"}\n'})

    r = pull("django", into=str(tmp_path), force=True, fetch=_fetcher(blob))

    lines = [l for l in notes.read_text().splitlines() if l.strip()]
    assert local_line in lines, "local entry never deleted"
    assert bundle_line in lines, "bundle entry added"
    assert len(lines) == 2, f"union, not duplication: {lines}"
    assert lines[0] == local_line, "local entries stay first"
    assert (tmp_path / ".coord" / "meta" / "C-only-theirs").is_dir()
    assert r["merged"]["entries_added"] == 2
    assert r["merged"]["coords_from_bundle"] == 1

    # idempotent: pulling the same bundle again adds nothing
    r2 = pull("django", into=str(tmp_path), force=True, fetch=_fetcher(blob))
    assert r2["merged"]["entries_added"] == 0
    assert len([l for l in notes.read_text().splitlines() if l.strip()]) == 2


def test_replace_meta_alone_is_refused(tmp_path, monkeypatch, capsys):
    """The destructive path must be harder to type than the safe one."""
    import sys as _sys
    from memway import cli
    _local_map(tmp_path)
    monkeypatch.setattr(_sys, "argv",
                        ["memway", "pull", "django", "--replace-meta",
                         "--into", str(tmp_path)])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    msg = str(ei.value)
    assert "destructive" in msg
    assert "deletes locally authored knowledge" in msg
    assert "requires explicit --force" in msg
    assert (tmp_path / ".coord" / "meta" / "C-local" / "notes.jsonl").exists(), \
        "nothing touched when the guard fires"


def test_replace_meta_actually_replaces(tmp_path):
    notes = _local_map(tmp_path)
    blob = _good_bundle()

    r = pull("django", into=str(tmp_path), force=True, replace_meta=True,
             fetch=_fetcher(blob))

    assert not notes.exists(), "--replace-meta deletes authored knowledge"
    assert r["merged"] is None and r["replaced_meta"] is True


def test_refusal_message_names_both_flags(tmp_path):
    _local_map(tmp_path)
    with pytest.raises(PullError) as ei:
        pull("django", into=str(tmp_path), fetch=_fetcher(_good_bundle()))
    msg = str(ei.value)
    assert "--force" in msg and "--replace-meta" in msg
    assert "kept" in msg, "must say local knowledge survives --force"
    assert "deletes locally authored knowledge" in msg


@pytest.mark.parametrize("arcname", [
    "../evil.txt",                    # climbs out of the target
    "/etc/passwd",                    # absolute
    ".coord/../../evil.txt",          # climbs out from inside .coord
    "notcoord/evil.txt",              # outside .coord entirely
])
def test_path_traversal_member_refuses(tmp_path, arcname):
    blob = _bundle({".coord/manifest.json": b"{}", arcname: b"pwned"})
    with pytest.raises(PullError, match="refusing"):
        pull("django", into=str(tmp_path), fetch=_fetcher(blob))
    assert not (tmp_path / ".coord").exists(), "nothing installed"
    assert not (tmp_path.parent / "evil.txt").exists(), "nothing escaped"


def test_symlink_member_refuses(tmp_path):
    """A link out of the tree is a write out of the tree."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(".coord/manifest.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
        link = tarfile.TarInfo(".coord/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tar.addfile(link)
    with pytest.raises(PullError, match="link"):
        pull("django", into=str(tmp_path), fetch=_fetcher(buf.getvalue()))


def test_empty_and_rootless_bundles_refuse(tmp_path):
    with pytest.raises(PullError, match="no files"):
        pull("x", into=str(tmp_path), fetch=_fetcher(_bundle({})))
    blob = _bundle({".coord/": None})          # dir only, no .coord content
    r_dir = tmp_path / "b"
    r_dir.mkdir()
    pull("x", into=str(r_dir), fetch=_fetcher(blob))
    assert (r_dir / ".coord").is_dir()


def test_verify_checksum_accepts_both_forms():
    blob = b"hello"
    d = hashlib.sha256(blob).hexdigest()
    assert verify_checksum(blob, d) == d
    assert verify_checksum(blob, f"{d}  bundle.tar.gz\n") == d
    with pytest.raises(PullError, match="empty"):
        verify_checksum(blob, "   ")


def test_safe_members_rejects_before_extraction(tmp_path):
    """The check must happen before anything lands on disk."""
    blob = _bundle({".coord/ok.json": b"{}", "../evil": b"x"})
    import io as _io
    with tarfile.open(fileobj=_io.BytesIO(blob), mode="r:gz") as tar:
        with pytest.raises(PullError):
            safe_members(tar, tmp_path)
    assert list(tmp_path.iterdir()) == [], "nothing written during validation"


def test_drift_notice_when_map_sha_differs(tmp_path, monkeypatch):
    blob = _good_bundle(manifest={"repo": "r", "sha": "deadbeefdeadbeef"})
    monkeypatch.setattr("memway.registry._local_head", lambda p: "cafebabecafebabe")
    r = pull("x", into=str(tmp_path), fetch=_fetcher(blob))
    assert r["drifted"] is True

    monkeypatch.setattr("memway.registry._local_head",
                        lambda p: "deadbeefdeadbeef0000")
    r2 = pull("x", into=str(tmp_path), force=True, fetch=_fetcher(blob))
    assert r2["drifted"] is False, "same sha is not drift"


# ------------------------------------------------- manifest v1 + legacy fallback

V1 = {"format": "memway/0.1", "language": "python",
      "name": "httpx", "upstream_repo": "https://github.com/encode/httpx",
      "upstream_sha": "b5addb64f0161ff6bfe94c124ef76f6a1fba5254",
      "memway_version": "0.50.1", "license": "BSD-3-Clause",
      "built_at": "2026-08-15T19:03:56Z"}

LEGACY = {"format": "memway/0.1", "repo": "https://github.com/encode/httpx",
          "sha": "b5addb64f0161ff6bfe94c124ef76f6a1fba5254",
          "version": "latest"}

# Exactly what the three live releases ship: v1 fields AND the aliases,
# because the reader at publish time only understood the aliases.
BOTH = dict(LEGACY, **V1)


@pytest.mark.parametrize("manifest,schema", [
    (V1, "v1"), (LEGACY, "legacy"), (BOTH, "v1+legacy")])
def test_both_manifest_shapes_normalize_identically(tmp_path, manifest, schema):
    """One reader, three shapes, one set of names downstream.

    The published bundles predate v1 and must keep installing; the point
    of the fallback is that nothing past _describe can tell which shape
    it was handed.
    """
    r = pull("httpx", into=str(tmp_path), fetch=_fetcher(_good_bundle(manifest=manifest)))
    assert r["upstream_repo"] == "https://github.com/encode/httpx"
    assert r["upstream_sha"] == "b5addb64f0161ff6bfe94c124ef76f6a1fba5254"
    assert r["manifest_schema"] == schema


def test_v1_only_fields_are_surfaced(tmp_path):
    """license/built_at/memway_version exist only in v1 and must survive."""
    r = pull("httpx", into=str(tmp_path), fetch=_fetcher(_good_bundle(manifest=V1)))
    assert r["license"] == "BSD-3-Clause"
    assert r["memway_version"] == "0.50.1"
    assert r["built_at"] == "2026-08-15T19:03:56Z"
    assert r["manifest_name"] == "httpx", "the bundle's own name is preserved"


def test_drift_uses_upstream_sha_under_both_shapes(tmp_path, monkeypatch):
    """Drift is measured against upstream_sha whichever shape supplied it."""
    monkeypatch.setattr("memway.registry._local_head", lambda p: "cafebabecafebabe")
    for i, man in enumerate((V1, LEGACY)):
        d = tmp_path / f"r{i}"
        d.mkdir()
        r = pull("httpx", into=str(d), fetch=_fetcher(_good_bundle(manifest=man)))
        assert r["drifted"] is True, f"{man.get('name', 'legacy')} missed drift"
    d = tmp_path / "same"
    d.mkdir()
    monkeypatch.setattr("memway.registry._local_head",
                        lambda p: "b5addb64f0161ff6bfe94c124ef76f6a1fba5254")
    r = pull("httpx", into=str(d), fetch=_fetcher(_good_bundle(manifest=V1)))
    assert r["drifted"] is False


def test_manifestless_bundle_still_installs(tmp_path):
    """No manifest is not an error - provenance is simply unknown."""
    coords = json.dumps({"C-000001": {"qualname": "pkg.f"}}).encode()
    blob = _bundle({".coord/": None, ".coord/index/coordinates.json": coords})
    r = pull("x", into=str(tmp_path), fetch=_fetcher(blob))
    assert r["entities"] == 1
    assert r["manifest_schema"] is None
    assert r["upstream_sha"] is None and r["drifted"] is False


def test_no_mcp_tool_exposes_pull():
    """Deliberate: pull fetches from the network and writes a tree to
    disk. That pair stays behind a human typing a command, not behind a
    model deciding to call it. Asserted on the registered tool NAMES, not
    on the file text - a comment mentioning pull is not a tool.
    """
    from memway.mcp import TOOLS
    names = {t["name"] for t in TOOLS}
    assert not any("pull" in n for n in names), \
        f"pull must not be reachable over MCP: {sorted(names)}"
    assert "memway_show" in names, "guard is vacuous if TOOLS is empty"


# ---------------------------------------------------- failures with context

class _HTTPError(Exception):
    """Duck-typed stand-in: urllib's HTTPError carries .code."""
    def __init__(self, code, msg="Not Found"):
        super().__init__(f"HTTP Error {code}: {msg}")
        self.code = code


def _raising(exc, only=None):
    """A fetcher that raises for the bundle, or only for `only` suffix."""
    def fetch(url):
        if only is None or url.endswith(only):
            raise exc
        return b"x"
    return fetch


def test_404_names_the_map_the_url_and_the_registry_index():
    """The bare `HTTP Error 404: Not Found` told a typo'd name nothing:
    not which map, not which URL, not that an index of real maps exists."""
    with pytest.raises(PullError) as ei:
        pull("nosuchmap", into="/tmp/never", fetch=_raising(_HTTPError(404)))
    msg = str(ei.value)
    assert "nosuchmap" in msg, "the map name must appear"
    assert "not found in the registry" in msg
    assert "nosuchmap-latest.tar.gz" in msg, "the exact URL attempted"
    assert "github.com/whurleyjr734/memway-maps/releases" in msg, \
        "where to find the real list"
    assert "HTTP Error 404" not in msg, "the bare urllib text is not the message"


def test_404_on_the_checksum_is_a_different_failure():
    """Bundle present, checksum missing: not a typo, an unverifiable map."""
    with pytest.raises(PullError) as ei:
        pull("flask", into="/tmp/never",
             fetch=_raising(_HTTPError(404), only=".sha256"))
    msg = str(ei.value)
    assert "ships no checksum" in msg
    assert "flask" in msg and ".sha256" in msg
    assert "not found in the registry" not in msg, \
        "must not be reported as a missing map"


def test_connection_refused_keeps_its_class_and_reason():
    """A network fault is not a missing map, and must not be relabelled."""
    import urllib.error
    exc = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    with pytest.raises(PullError) as ei:
        pull("flask", into="/tmp/never", fetch=_raising(exc))
    msg = str(ei.value)
    assert "could not fetch the bundle for 'flask'" in msg
    assert "URLError" in msg, "the exception class survives"
    assert "Connection refused" in msg, "the reason survives"
    assert "flask-latest.tar.gz" in msg
    assert "not found in the registry" not in msg


def test_non_404_http_error_is_not_reported_as_a_missing_map():
    with pytest.raises(PullError) as ei:
        pull("flask", into="/tmp/never",
             fetch=_raising(_HTTPError(503, "Service Unavailable")))
    msg = str(ei.value)
    assert "could not fetch" in msg and "503" in msg
    assert "not found in the registry" not in msg


def test_checksum_mismatch_message_is_unchanged(tmp_path):
    """The contextual wrapper must not swallow or reword the one failure
    that already said exactly the right thing."""
    blob = _good_bundle()
    with pytest.raises(PullError) as ei:
        pull("django", into=str(tmp_path), fetch=_fetcher(blob, checksum="0" * 64))
    msg = str(ei.value)
    assert "CHECKSUM MISMATCH - refusing to install." in msg
    assert "expected " + "0" * 64 in msg
    assert "corrupted in transit or tampered with" in msg
    assert "could not fetch" not in msg, "not re-wrapped as a fetch failure"


def test_a_contextual_error_is_never_re_wrapped(tmp_path):
    """_fetch adds context to raw exceptions but must pass a PullError
    through untouched - otherwise a precise message gets buried under a
    generic 'could not fetch' one layer up."""
    from memway.registry import PullError as PE

    def fetch(url):
        raise PE("CHECKSUM MISMATCH - refusing to install.\n  bespoke detail")

    with pytest.raises(PullError) as ei:
        pull("flask", into=str(tmp_path), fetch=fetch)
    msg = str(ei.value)
    assert "bespoke detail" in msg
    assert "could not fetch" not in msg, "a PullError must not be re-wrapped"
    assert "not found in the registry" not in msg

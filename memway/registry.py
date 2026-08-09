"""
Registry client: fetch a published map and install it locally.

`memway pull <name>` downloads a map bundle, verifies its checksum,
and unpacks it into `.coord/`. The point is that a map is worth more
when you do not have to build it: someone indexes django once, and
everyone else inherits the coordinates and whatever knowledge was
attached to them.

Trust boundary. A bundle is a tarball from the network, so it is
treated as hostile until proven otherwise:

  - the checksum must match before anything is unpacked
  - every member is validated before extraction, not after
  - members must resolve inside the target and live under .coord/
  - links and devices are refused outright

A tar member named ../../.ssh/authorized_keys is the classic attack;
so is a symlink to /etc followed by a write through it. Both are
rejected here rather than delegated to tarfile, whose safe extraction
filter only exists on Python 3.12+ and this package supports 3.10.

Stdlib only - urllib, tarfile, hashlib. The zero-dependency claim is
load-bearing and this module does not get to break it.
"""

from __future__ import annotations

import hashlib
import json

import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

# The registry does not exist yet. This is the scheme it will publish
# under, overridable with --source so the client is testable (and
# usable against any other host) before then.
DEFAULT_SOURCE = ("https://github.com/whurleyjr734/memway-maps/releases/"
                  "download/{name}-{version}/{name}-{version}.tar.gz")
DEFAULT_VERSION = "latest"
CHECKSUM_SUFFIX = ".sha256"

# Derived from the tree: regenerates on `memway index`, safe to replace
# wholesale. Everything NOT in here under .coord/ is treated as authored
# until proven otherwise - which today means meta/, the one directory
# whose contents nobody can reconstruct.
DERIVED_DIRS = ("index", "cache", "metrics", "versions")


class PullError(Exception):
    """Anything that should stop an install, phrased for a human."""


def _http_get(url: str) -> bytes:
    from urllib.request import Request, urlopen
    req = Request(url, headers={"User-Agent": "memway-pull"})
    with urlopen(req, timeout=60) as resp:      # noqa: S310 - scheme checked
        return resp.read()


def resolve_url(name: str, source: str = DEFAULT_SOURCE) -> tuple:
    """(bundle_url, checksum_url, name, version) from a `name` or `name@version`."""
    if "@" in name:
        base, _, version = name.partition("@")
    else:
        base, version = name, DEFAULT_VERSION
    if not base or "/" in base or ".." in base:
        raise PullError(f"invalid map name {name!r}")
    url = source.format(name=base, version=version)
    if not url.startswith(("https://", "file://")):
        raise PullError(
            f"refusing a non-https source: {url}\n"
            "  a map bundle is executable-adjacent data; fetch it over TLS")
    return url, url + CHECKSUM_SUFFIX, base, version


def verify_checksum(blob: bytes, checksum_text: str) -> str:
    """Return the verified digest, or raise. Accepts bare or `sha  name` form."""
    want = (checksum_text or "").strip().split()
    if not want:
        raise PullError("checksum file was empty - refusing to install")
    want = want[0].lower()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise PullError(
            "CHECKSUM MISMATCH - refusing to install.\n"
            f"  expected {want}\n  got      {got}\n"
            "  the bundle was corrupted in transit or tampered with")
    return got


def safe_members(tar: tarfile.TarFile, dest: Path) -> list:
    """Every member, validated. Raises on the first unsafe one.

    Checked before extraction, never after: a member that escapes the
    target has already done its damage by the time you notice the file.
    """
    dest = dest.resolve()
    out = []
    for m in tar.getmembers():
        where = f"bundle member {m.name!r}"
        if m.issym() or m.islnk():
            raise PullError(f"{where} is a link - refusing "
                            "(a link out of the tree is a write out of the tree)")
        if not (m.isfile() or m.isdir()):
            raise PullError(f"{where} is not a regular file or directory - refusing")
        posix = PurePosixPath(m.name)
        if posix.is_absolute() or ".." in posix.parts:
            raise PullError(f"{where} escapes the target directory - refusing")
        if not posix.parts or posix.parts[0] != ".coord":
            raise PullError(f"{where} lives outside .coord/ - refusing")
        target = (dest / m.name).resolve()
        if target != dest and dest not in target.parents:
            raise PullError(f"{where} resolves outside the target - refusing")
        # Normalize modes rather than trusting the archive: a bundle can
        # ship a directory with no execute bit (unreadable once written)
        # or a setuid file. Python 3.12's `data` filter does this; the
        # 3.10 floor means doing it here.
        m.mode = 0o755 if m.isdir() else 0o644
        m.uid = m.gid = 0
        m.uname = m.gname = ""
        out.append(m)
    if not out:
        raise PullError("bundle contained no files - refusing")
    return out


def _describe(coord: Path) -> dict:
    """Entity count and whatever provenance the bundle declares."""
    info = {"entities": None, "repo": None, "sha": None, "version": None}
    try:
        ents = json.loads((coord / "index" / "coordinates.json").read_text())
        info["entities"] = len(ents) if isinstance(ents, dict) else None
    except (OSError, ValueError):
        pass
    try:
        man = json.loads((coord / "manifest.json").read_text())
        for k in ("repo", "sha", "version", "source"):
            if man.get(k):
                info[k if k != "source" else "repo"] = man[k]
    except (OSError, ValueError):
        pass
    return info


def _local_head(repo: Path) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _merge_meta(bundle_meta: Path, local_meta: Path) -> dict:
    """Union bundle knowledge into local knowledge. Never deletes.

    Knowledge is authored - somebody's reason for something, and the one
    part of a map that cannot be regenerated from the tree. A pull is an
    inheritance, not a replacement: entries the bundle brings are added,
    entries only you have stay, and an entry present in both appears
    once. Channels are append-only JSONL, so union is well defined and
    dedup on the exact line is enough.
    """
    stats = {"coords_from_bundle": 0, "entries_added": 0,
             "coords_local_kept": 0}
    local_meta.mkdir(parents=True, exist_ok=True)
    already = {p.name for p in local_meta.iterdir() if p.is_dir()}
    # counted before the early return: a bundle with no knowledge of its
    # own still preserves yours, and the report should say so
    stats["coords_local_kept"] = len(already)
    if not bundle_meta.is_dir():
        return stats
    for cdir in sorted(bundle_meta.iterdir()):
        if not cdir.is_dir():
            continue
        if cdir.name not in already:
            stats["coords_from_bundle"] += 1
        target = local_meta / cdir.name
        target.mkdir(parents=True, exist_ok=True)
        for f in sorted(cdir.glob("*.jsonl")):
            tf = target / f.name
            local_lines = ([l for l in tf.read_text().splitlines() if l.strip()]
                           if tf.exists() else [])
            merged, seen = list(local_lines), set(local_lines)
            for line in f.read_text().splitlines():
                if line.strip() and line not in seen:
                    merged.append(line)
                    seen.add(line)
                    stats["entries_added"] += 1
            tf.write_text("\n".join(merged) + "\n" if merged else "")
    return stats


def pull(name: str, into: str = ".", source: str = DEFAULT_SOURCE,
         force: bool = False, replace_meta: bool = False, fetch=None) -> dict:
    """Fetch, verify, and unpack a map bundle. Returns a report dict.

    `fetch` is injectable so the whole path is testable without network.
    """
    fetch = fetch or _http_get
    into = Path(into).resolve()
    coord = into / ".coord"

    if coord.exists() and not force:
        raise PullError(
            f"{coord} already exists - refusing to install over it.\n"
            "  --force replaces the derived index and MERGES the bundle's\n"
            "  knowledge into your own; locally authored entries are kept.\n"
            "  --replace-meta additionally deletes locally authored knowledge.")

    url, sum_url, base, version = resolve_url(name, source)
    blob = fetch(url)
    digest = verify_checksum(blob, fetch(sum_url).decode("utf-8", "replace"))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "bundle.tar.gz").write_bytes(blob)
        staged = tmp / "staged"
        staged.mkdir()
        with tarfile.open(tmp / "bundle.tar.gz", "r:gz") as tar:
            members = safe_members(tar, staged)
            # members are already validated and mode-normalized; pass the
            # stdlib filter too where it exists (3.12+) for belt and braces
            try:
                tar.extractall(staged, members=members, filter="data")
            except TypeError:                          # Python < 3.12
                tar.extractall(staged, members=members)   # noqa: S202
        src = staged / ".coord"
        if not src.is_dir():
            raise PullError("bundle has no .coord/ at its root - refusing")
        merged = None
        if not coord.exists():
            shutil.move(str(src), str(coord))
        elif replace_meta:
            shutil.rmtree(coord)
            shutil.move(str(src), str(coord))
        else:
            # Derived is replaced; authored is merged. Same line the
            # storage guidance and the CI pipeline design both land on.
            for item in src.iterdir():
                if item.name == "meta":
                    continue
                dest_item = coord / item.name
                if dest_item.is_dir():
                    shutil.rmtree(dest_item)
                elif dest_item.exists():
                    dest_item.unlink()
                shutil.move(str(item), str(dest_item))
            merged = _merge_meta(src / "meta", coord / "meta")

    report = _describe(coord)
    report.update({"name": base, "version": version, "url": url,
                   "sha256": digest, "installed_to": str(coord),
                   "members": len(members), "merged": merged,
                   "replaced_meta": bool(replace_meta and merged is None)})
    head = _local_head(into)
    report["drifted"] = bool(head and report.get("sha")
                             and not head.startswith(str(report["sha"])))
    report["local_head"] = head
    return report

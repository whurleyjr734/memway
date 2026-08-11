"""Access cache: fast warm loads of recently used index data.

JSON stays the source of truth (human-readable, diffable). The pickle
is a disposable acceleration layer whose validity is a single
fingerprint - (mtime_ns, size) of the source file - so freshness is
answered the same way everything in memway answers it: one fact,
checked cheaply. Stale or corrupt cache silently falls back to JSON
and rewrites itself. Measured at django scale: 2366ms -> 339ms (7x).

Trust note: pickles are read only from the repo's own .coord/cache
directory, the same trust domain as the index that produced them.
"""

import json
import os
import pickle
from pathlib import Path


def _fingerprint(src: Path):
    st = src.stat()
    return (st.st_mtime_ns, st.st_size)


def load_json_cached(src: Path, coord_dir: Path, *, write: bool = True):
    """Parse src (JSON), using/refreshing a fingerprint-keyed pickle.

    `write=False` still USES a valid cache but never creates or refreshes
    one. Read-only tools need it: `memway dig`, `memway viz`, `memway
    evidence` and the console's GET endpoints all promise they do not
    touch .coord, and warming a cache is a write like any other - it
    breaks a read-only checkout and makes "did anything change?"
    unanswerable for the caller.
    """
    if not src.exists():
        return None
    cache_dir = coord_dir / "cache"
    pkl = cache_dir / (src.stem + ".pkl")
    fp = _fingerprint(src)
    if pkl.exists():
        try:
            with pkl.open("rb") as fh:
                stamp, data = pickle.load(fh)
            if stamp == fp:
                return data
        except Exception:
            pass                       # corrupt/old cache: fall through
    data = json.loads(src.read_text())
    if not write:
        return data
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = pkl.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump((fp, data), fh, protocol=5)
        os.replace(tmp, pkl)           # atomic, like every write here
    except Exception:
        pass                           # caching is optional, never fatal
    return data

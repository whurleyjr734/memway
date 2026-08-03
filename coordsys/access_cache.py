"""Access cache: fast warm loads of recently used index data.

JSON stays the source of truth (human-readable, diffable). The pickle
is a disposable acceleration layer whose validity is a single
fingerprint - (mtime_ns, size) of the source file - so freshness is
answered the same way everything in coordsys answers it: one fact,
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


def load_json_cached(src: Path, coord_dir: Path):
    """Parse src (JSON), using/refreshing a fingerprint-keyed pickle."""
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
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = pkl.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump((fp, data), fh, protocol=5)
        os.replace(tmp, pkl)           # atomic, like every write here
    except Exception:
        pass                           # caching is optional, never fatal
    return data

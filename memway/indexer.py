"""
Coordinate indexer.

Walks a codebase and assigns a stable coordinate ID to every code
entity: package, module, class, function, method, attribute. Python is
parsed with the standard library's ast; Go, JavaScript, TypeScript and
Java through tree-sitter plugins (see parsers.py). Said "a Python
codebase" until 2026-08-16, by which point four more languages had
arrived and the map of this very repo held .go and .ts entities.

IDs are stable: once an entity gets an ID it keeps it across re-indexing.
New entities get new IDs. Renamed/moved entities are matched to their
old ID via body-hash similarity (handled in lineage.py).

Each entity carries multiple hashes: body_hash for identity matching,
shape_hash (name-insensitive structure) for clone detection,
logic_hash (behavior-only, ignoring comments/docstrings) for detecting
real changes vs cosmetic edits, and comment_hash for rot detection.
Minhash sketches enable fast similarity estimation without storing source.
"""

import hashlib
import os
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


ENTITY_KINDS = ("package", "module", "class", "function", "method", "attribute")


@dataclass
class Entity:
    coord_id: str            # stable ID, e.g. "C-a3f19c"
    kind: str                # one of ENTITY_KINDS
    qualname: str            # e.g. "src.auth.login.LoginService.authenticate"
    path: str                # file path relative to repo root
    lineno: int              # current location (volatile, informational)
    body_hash: str           # hash of normalized source body (for lineage matching)
    parent: Optional[str] = None   # coord_id of parent entity
    signature: str = ""      # for functions/methods
    shape_hash: str = ""     # body hash with the entity's own name stripped
    logic_hash: str = ""     # cosmetics-insensitive hash (docstrings/comments ignored)
    param_types: dict = field(default_factory=dict)  # declared annotations
    return_type: str = ""
    comments: list = field(default_factory=list)     # line-level "why"
    comment_hash: str = ""
    comment_rot: bool = False   # comments unchanged across a logic change
                             # (survives renames: catches name+body change)
    sketch: Optional[list] = None   # minhash sketch of body tokens - lets
                                    # detect_lineage estimate similarity
                                    # WITHOUT the old source text
    n_shingles: int = 0             # shingle count (for containment estimates)
    end_lineno: int = 0      # exact extent from the parser (D1)
    loc: int = 0             # lines of code, from parser body text (D1)
    complexity: int = 1      # cyclomatic approx, from parser body text (D1)

    def to_dict(self):
        return asdict(self)


def _hash_body(text: str) -> str:
    """Hash of whitespace-normalized source, used for identity matching."""
    normalized = "".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


_SKETCH_K = 48

# Bumped when the SHINGLE HASH changes, which makes every stored sketch
# incomparable with every new one. Distinct from PARSE_SCHEMA_VERSION:
# that invalidates the parse CACHE (recomputing the new side), while this
# describes sketches already written into coordinates.json, which cannot
# be recomputed because the old source text is gone.
#
#   1  builtin hash() - randomized per process, never comparable across runs
#   2  blake2b, stable forever
SKETCH_VERSION = 2


def stored_sketch_version(coord_dir) -> int:
    """Which shingle hash produced the sketches currently on disk.

    Absent means 1: maps written before the stamp existed used builtin
    hash(). Defaulting to "current" instead would make every pre-0.54 map
    claim comparability it does not have - the one lie this whole
    mechanism exists to prevent.
    """
    try:
        man = json.loads((Path(coord_dir) / "manifest.json").read_text())
        return int(man.get("sketch_version", 1))
    except (OSError, ValueError, TypeError):
        return 1


def record_sketch_version(coord_dir) -> None:
    """Stamp the manifest. Additive, like freshness's indexed_at_sha."""
    p = Path(coord_dir) / "manifest.json"
    try:
        man = json.loads(p.read_text())
        if not isinstance(man, dict):
            man = {}
    except (OSError, ValueError):
        man = {}
    man["sketch_version"] = SKETCH_VERSION
    try:
        p.write_text(json.dumps(man, indent=1) + "\n")
    except OSError:
        pass                    # a map that cannot stamp is still a map


def _sketch(body_text: str):
    """Minhash sketch over token 3-gram shingles. ~100 bytes per entity,
    supports Jaccard AND containment estimation from the index alone.

    THE SHINGLE HASH MUST BE STABLE ACROSS PROCESSES. This used builtin
    hash(), which Python randomizes per process, and sketches are
    PERSISTED - so every stored sketch was compared against values from a
    different seed. Measured 2026-08-16: two fresh clones of one sha
    produced byte-identical .coord except `sketch`, which differed on
    888/888 entities; and lineage, whose largest single signal is
    sketch_jaccard (weight 0.30), reported `deleted` for a rename it
    reported as `merged - needs confirmation` when the seed was pinned.
    Randomization turned "flagged for a human" into "silently deleted".

    blake2b at digest_size=6 gives exactly the 48 bits the permutation
    below already masks to. Benchmarked against the alternatives on
    6,880 real shingles: crc32 was faster but 32-bit, and adler32 mixed
    so poorly it lost distinct values (4,809 vs 4,821). The cost is ~3ms
    per 7k shingles, which is noise beside parsing.

    Tokens cannot contain NUL (the regex admits neither whitespace nor
    control characters), so joining on it is injective - ("ab","c") and
    ("a","bc") cannot collide into one string.
    """
    import re as _re
    from hashlib import blake2b
    toks = _re.findall(r"[A-Za-z_]\w*|[^\sA-Za-z_]", body_text)
    sh = {int.from_bytes(
              blake2b("\x00".join(toks[i:i + 3]).encode(),
                      digest_size=6).digest(), "big")
          for i in range(max(1, len(toks) - 2))}
    if not sh:
        return [0] * _SKETCH_K, 0
    mins = []
    for seed in range(_SKETCH_K):
        mins.append(min((s ^ (seed * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFF
                        for s in sh))
    return mins, len(sh)


def sketch_jaccard(a, b):
    if not a or not b:
        return 0.0
    # normalize by the compared length, not the constant: production
    # sketches are always _SKETCH_K long (identical result), but ad-hoc
    # or truncated sketches no longer silently score identical inputs
    # at k/_SKETCH_K instead of 1.0.
    k = min(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x == y) / k


def sketch_containment(child, parent, n_child, n_parent):
    """Estimated |child ∩ parent| / |child| from minhash Jaccard + set sizes."""
    j = sketch_jaccard(child, parent)
    if j == 0 or n_child == 0:
        return 0.0
    inter = j / (1.0 + j) * (n_child + n_parent)
    return min(1.0, inter / n_child)


def _structure_hash(body_text: str, short_name: str) -> str:
    """Name-insensitive structural hash.

    AST node-type sequence when the body parses as Python: identical
    structure hashes identically regardless of ANY identifier, so a
    rename matches even when the old name lingers in a docstring,
    comment, or recursive call (the failure mode of name-stripping).
    Falls back to name-stripped text hashing for non-Python bodies.
    """
    import ast as _ast
    import textwrap as _tw
    try:
        tree = _ast.parse(_tw.dedent(body_text))
        shape = "|".join(type(n).__name__ for n in _ast.walk(tree))
        return hashlib.sha256(shape.encode()).hexdigest()[:16]
    except SyntaxError:
        return _hash_body(body_text.replace(short_name, ""))


def _logic_hash(body_text: str) -> str:
    """Behavior-sensitive, cosmetics-insensitive hash.

    The question staleness and caching actually ask is "did the LOGIC
    change?" - a text hash says yes for comment edits, docstring tweaks,
    and reformatting, which over-invalidates caches and cries wolf on
    stale flags (and a flag that cries wolf stops being trusted).

    Implementation: the AST with docstrings stripped, serialized WITH
    identifiers kept (unlike shape_hash, which strips names). This is
    bytecode-equivalent semantically but deterministic across
    interpreter versions and portable in principle to other languages.
    Falls back to the text hash for non-Python bodies (honest
    degradation: logic tier collapses to body tier).
    """
    import ast as _ast
    import textwrap as _tw
    try:
        tree = _ast.parse(_tw.dedent(body_text))
    except SyntaxError:
        return _hash_body(body_text)
    # normalize the entity's OWN name: a pure rename is identity, not a
    # logic change (internal identifiers stay - renaming a variable IS
    # a logic-relevant edit; recursive self-calls fall to the similarity scorer).
    # Combined with docstring stripping above, this hash changes ONLY when
    # behavior changes, enabling comment-rot detection and staleness checking.
    if tree.body and hasattr(tree.body[0], "name"):
        tree.body[0].name = "_"
    for node in _ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body
                and isinstance(body[0], _ast.Expr)
                and isinstance(body[0].value, _ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [_ast.Pass()]
    return hashlib.sha256(_ast.dump(tree).encode()).hexdigest()[:16]


def _annotations(body_text: str):
    """Harvest declared parameter/return types into structured data.

    Annotations are ground truth the author wrote down - on typed
    codebases they resolve the single largest static-analysis gap
    (untyped receivers: obj.meth() where obj's class is unknowable).
    Returns ({param: type_str}, return_type_str); empty on plain defs.
    """
    import ast as _ast
    import textwrap as _tw
    try:
        tree = _ast.parse(_tw.dedent(body_text))
    except SyntaxError:
        return {}, ""
    if not tree.body or not isinstance(
            tree.body[0], (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        return {}, ""
    fn = tree.body[0]
    params = {}
    for a in (fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs):
        if a.annotation is not None:
            try:
                params[a.arg] = _ast.unparse(a.annotation)
            except Exception:
                pass
    ret = ""
    if fn.returns is not None:
        try:
            ret = _ast.unparse(fn.returns)
        except Exception:
            pass
    return params, ret


_C_STYLE_EXTS = (".go", ".js", ".jsx", ".ts", ".tsx", ".java")


def _c_style_comments(body_text: str) -> list:
    """Harvest // and /* */ comments for brace-family languages.

    Python's tokenize sees no COMMENT token in Go/TS/Java source, so
    without this every non-Python entity harvests zero comments - and
    since rot detection is gated on a non-empty comment list, the entire
    non-Python tier is silently exempt from drift detection.
    """
    out, in_block = [], False
    for i, line in enumerate(body_text.splitlines(), start=1):
        s = line.strip()
        if in_block:
            text = s[:s.index("*/")] if "*/" in s else s
            text = text.lstrip("*").strip()
            if text:
                out.append({"line": i, "text": text[:200], "kind": "comment"})
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            in_block = "*/" not in s
            text = s[2:s.index("*/")] if "*/" in s else s[2:]
            text = text.strip().lstrip("*").strip()
            if text:
                out.append({"line": i, "text": text[:200], "kind": "comment"})
            continue
        # only a line whose CODE part ends before the // counts; a // inside
        # a string literal would otherwise be harvested as intent.
        idx = s.find("//")
        if idx >= 0 and s.count('"', 0, idx) % 2 == 0 \
                and s.count("'", 0, idx) % 2 == 0:
            text = s[idx + 2:].strip()
            if text:
                out.append({"line": i, "text": text[:200], "kind": "comment"})
    return out


def _comments(body_text: str, ext: str = ".py") -> list:
    """Harvest comments with entity-relative line numbers.

    Comments are the ORIGINAL edit-time intent capture - written at the
    moment identity was certain - and the one knowledge form the AST
    (and therefore logic_hash) deliberately ignores. Harvesting them
    gives agents the line-level "why"; hashing them separately enables
    comment-rot detection (see Entity.comment_rot).

    Dispatches on file extension: Python source goes through tokenize
    plus its docstring, brace-family source through _c_style_comments.
    """
    import io as _io
    import tokenize as _tk
    if ext in _C_STYLE_EXTS:
        return _c_style_comments(body_text)
    out = []
    try:
        for tok in _tk.generate_tokens(_io.StringIO(body_text).readline):
            if tok.type == _tk.COMMENT:
                text = tok.string.lstrip("#").strip()
                if text:
                    out.append({"line": tok.start[0], "text": text[:200],
                                "kind": "comment"})
    except (_tk.TokenizeError, IndentationError, SyntaxError):
        pass
    # Docstrings are STRING tokens, never COMMENT, so the loop above cannot
    # see them - and rot detection is gated on a non-empty result, which left
    # every docstring-only entity permanently exempt from drift detection.
    # They are the same kind of edit-time intent, and logic_hash already
    # ignores them, so the comparison machinery works on them unchanged.
    try:
        import ast as _ast
        import textwrap as _tw
        _tree = _ast.parse(_tw.dedent(body_text))
        _node = _tree.body[0] if _tree.body else None
        if isinstance(_node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                              _ast.ClassDef)):
            _doc, _line = _ast.get_docstring(_node), _node.body[0].lineno
        else:                       # module entity: body_text is a whole file
            _doc = _ast.get_docstring(_tree)
            _line = _tree.body[0].lineno if _tree.body else 1
        if _doc and _doc.strip():
            out.insert(0, {"line": _line,
                           "text": " ".join(_doc.split())[:200],
                           "kind": "docstring"})
    except (SyntaxError, IndentationError, ValueError, RecursionError):
        pass
    return out


def _new_id(qualname: str, salt: str = "") -> str:
    h = hashlib.sha256((qualname + salt).encode()).hexdigest()[:6]
    return f"C-{h}"


class Indexer:
    """Builds/refreshes the coordinate index for a repo.

    Maintains stable coordinate IDs across re-indexing and tracks
    entity metadata including hashes, complexity, and structural sketches."""

    def __init__(self, repo_root: str, coord_dir: str):
        self.repo_root = Path(repo_root)
        self.coord_dir = Path(coord_dir)
        self.entities: dict[str, Entity] = {}          # coord_id -> Entity
        self.by_qualname: dict[str, str] = {}          # qualname -> coord_id
        # True when the sketches already on disk were produced by a
        # different shingle hash than this build's. Set by load_existing,
        # read by cmd_index, and it makes lineage stop trusting a signal
        # it can no longer compute. See stored_sketch_version().
        self.stale_sketches = False

    # ------------------------------------------------------------------ load

    def load_existing(self, write_cache: bool = True):
        """write_cache=False makes this a pure read - no pickle warmed.
        Required by read-only tools - see the fences in memway/dig.py,
        memway/viz.py and memway/console.py."""
        db = self.coord_dir / "index" / "coordinates.json"
        # Read the generation BEFORE anything overwrites it. A map written
        # by an older memway has no stamp at all, and that is the case
        # that matters: it means SKETCH_VERSION 1, the randomized one.
        if db.exists():
            self.stale_sketches = stored_sketch_version(
                self.coord_dir) != SKETCH_VERSION
        if db.exists():
            from .access_cache import load_json_cached
            try:
                data = load_json_cached(db, self.coord_dir,
                                        write=write_cache)
            except (json.JSONDecodeError, EOFError):
                data = self._recover_from_snapshot()
            for cid, e in data.items():
                self.entities[cid] = Entity(**e)
                self.by_qualname[e["qualname"]] = cid

    def _recover_from_snapshot(self):
        """A corrupted index is recoverable: version snapshots are
        intact copies. Restoring identities from the latest snapshot
        preserves every coordinate ID - and therefore every attached
        piece of knowledge - across the corruption."""
        vd = self.coord_dir / "versions"
        if vd.is_dir():
            for v in sorted(vd.iterdir(),
                            key=lambda p: -int(p.name[1:])):
                try:
                    data = json.loads(
                        (v / "coordinates.json").read_text())
                    print(f"  index corrupted - recovered "
                          f"{len(data)} identities from snapshot "
                          f"{v.name}")
                    return data
                except Exception:
                    continue
        print("  index corrupted, no usable snapshot - "
              "rebuilding identities fresh")
        return {}

    # ----------------------------------------------------------------- index

    def index(self, *, persist: bool = True) -> dict:
        """Re-scan the repo. Returns a report of added/removed/changed.

        persist=False computes everything in memory and writes NOTHING -
        specifically it does not refresh the parse cache, which is the
        only write this method makes on its own. Read surfaces that need
        current-tree hashes (verify_change, attention) use it; they pair
        it with load_existing(write_cache=False) and never call save().

        Keyword-only and defaulting to True because 41 callers depend on
        the current behaviour, and because a positional flag here would
        eventually be passed by accident. Refreshing the cache is a write
        like any other - see the read fence.
        """
        old_entities = dict(self.entities)
        old_by_qualname = dict(self.by_qualname)

        self.entities = {}
        self.by_qualname = {}

        from .parsers import get_parsers
        parsers = get_parsers()
        from .parsers import _PARSER_ERRORS
        self._parser_errors = dict(_PARSER_ERRORS)
        self._raw_edges = []
        # Parse cache: per-file artifacts keyed by content hash AND parser
        # schema version, so unchanged files skip parsing entirely (only changed
        # coordinates get recomputed - the memoization invariant extended upstream
        # from metrics to parsing itself). Cache is versioned to invalidate when
        # the parser logic changes - see PARSE_SCHEMA_VERSION. Named, not
        # line-numbered: this said "lines 284-295" until 2026-08-16, by
        # which point those lines were _new_id() and a class header.
        cache_file = self.coord_dir / "index" / "parse_cache.json"
        cache = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                cache = {}
        # cache entries are keyed by FILE content - but the PARSER can
        # change too. An upgraded parser with a warm cache silently
        # replays stale edges forever (found live: the Mac kept its old
        # graph after the scope-aware-resolution upgrade). A schema
        # version stamps the cache; mismatch discards it wholesale.
        from .parsers import PARSE_SCHEMA_VERSION
        if cache.get("_schema") != PARSE_SCHEMA_VERSION:
            if cache:
                print(f"  parse cache: schema "
                      f"{cache.get('_schema', 0)} -> "
                      f"{PARSE_SCHEMA_VERSION}, re-parsing all files")
            cache = {}
        self._cache_hits = self._cache_misses = 0
        self._parse_errors = []
        new_cache = {}
        SKIP_DIRS = {"node_modules", "dist", "build", "vendor",
                     ".git", "__pycache__", ".next", "coverage"}
        for f in sorted(self.repo_root.rglob("*")):
            try:
                _rel = f.relative_to(self.repo_root).parts
            except ValueError:
                _rel = f.parts
            if any(part in SKIP_DIRS for part in _rel):
                continue
            if ".min." in f.name:
                continue                     # explicitly minified
            if f.suffix in (".js", ".mjs", ".ts", ".jsx", ".tsx"):
                try:
                    head = f.open("rb").read(2048).decode(errors="replace")
                    if any(len(line) > 500 for line in head.splitlines()):
                        continue             # minified/bundled by shape
                except OSError:                  # pragma: no cover
                    continue                     # unreadable (untestable as root)
            if not f.is_file() or f.suffix not in parsers:
                continue
            # dot-dir / vendored skip must consider ONLY the path
            # RELATIVE to the repo root - ancestors above the root
            # (e.g. macOS temp dirs, a user's ~/.config/... prefix)
            # are not the repo's business and must never gate its files.
            try:
                rel_parts = f.relative_to(self.repo_root).parts
            except ValueError:
                rel_parts = f.parts
            if any(part.startswith(".") or part in ("__pycache__",
                                                    "node_modules")
                   for part in rel_parts):
                continue
            try:
                self._index_file(f, parsers[f.suffix],
                                 old_by_qualname, old_entities,
                                 cache, new_cache)
            except Exception as e:
                # containment: a broken, binary, or hostile file must
                # never kill the run. Recorded and reported, not silent.
                self._parse_errors.append(
                    (str(f.relative_to(self.repo_root)),
                     f"{type(e).__name__}: {e}"))
        if persist:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            _t = tmp.with_suffix('.tmp')

            from .parsers import PARSE_SCHEMA_VERSION
            new_cache["_schema"] = PARSE_SCHEMA_VERSION
            _t.write_text(json.dumps(new_cache))

            os.replace(_t, tmp)
            os.replace(tmp, cache_file)

        added = [c for c in self.entities if c not in old_entities]
        removed = [c for c in old_entities if c not in self.entities]
        changed = [
            c for c in self.entities
            if c in old_entities
            and self.entities[c].body_hash != old_entities[c].body_hash
        ]
        return {
            "parse_errors": self._parse_errors,
            "parser_errors": getattr(self, "_parser_errors", {}),
            "added": added,
            "removed": removed,
            "changed": changed,
            "removed_entities": {c: old_entities[c] for c in removed},
        }

    def _index_file(self, path: Path, parser,
                    old_by_qualname: dict, old_entities: dict,
                    cache: dict = None, new_cache: dict = None):
        """Index a single file, using parse cache when file unchanged."""
        rel = str(path.relative_to(self.repo_root))
        raw = path.read_bytes()
        fsha = hashlib.sha256(raw).hexdigest()[:16]

        cached = (cache or {}).get(rel)
        if cached and cached.get("sha") == fsha:
            # cache hit: no parse, no hashing, no complexity - assign
            # stable IDs straight from the stored records
            self._cache_hits += 1
            qual_to_cid = {}
            for r in cached["entities"]:
                parent_cid = qual_to_cid.get(r["parent_qualname"])
                cid = self._assign(
                    r["qualname"], r["kind"], rel, r["lineno"],
                    r["body_hash"], parent_cid,
                    old_by_qualname, old_entities, r["signature"],
                    r["shape_hash"], logic_hash=r.get("logic_hash", ""),
                    param_types=r.get("param_types", {}),
                    return_type=r.get("return_type", ""),
                    comments=r.get("comments", []),
                    comment_hash=r.get("comment_hash", ""),
                    comment_rot=r.get("comment_rot", False),
                    end_lineno=r["end_lineno"],
                    loc=r["loc"], complexity=r["complexity"],
                    sketch=r.get("sketch"), n_shingles=r.get("n_shingles", 0))
                qual_to_cid[r["qualname"]] = cid
            from .parsers import RawEdge
            self._raw_edges.extend(RawEdge(**e) for e in cached["edges"])
            if new_cache is not None:
                new_cache[rel] = cached
            return

        self._cache_misses += 1
        entities, raw_edges = parser.parse(path, self.repo_root)
        from .metrics import complexity_of
        qual_to_cid = {}
        records = []
        for re_ in entities:
            parent_cid = qual_to_cid.get(re_.parent_qualname)
            body_hash = _hash_body(re_.body_text)
            shape_hash = _structure_hash(re_.body_text, re_.short_name)
            logic_hash = _logic_hash(re_.body_text)
            p_types, r_type = _annotations(re_.body_text)
            cmts = _comments(re_.body_text, Path(rel).suffix)
            # the leading doc block sits ABOVE the declaration, so it is not
            # in body_text; line 0 marks it as preceding the entity.
            _doc = getattr(re_, "doc", "")
            if _doc:
                _dt = " ".join(c["text"] for c in _c_style_comments(_doc))
                if _dt:
                    cmts.insert(0, {"line": 0, "text": _dt[:200],
                                    "kind": "docstring"})
            c_hash = hashlib.sha256(
                "\n".join(c["text"] for c in cmts).encode()
            ).hexdigest()[:16] if cmts else ""
            sk, n_sh = _sketch(re_.body_text)
            loc = re_.body_text.count("\n") + 1
            cx = complexity_of(re_.body_text, rel)
            cid = self._assign(
                re_.qualname, re_.kind, rel, re_.lineno,
                body_hash, parent_cid,
                old_by_qualname, old_entities, re_.signature,
                shape_hash, logic_hash=logic_hash,
                param_types=p_types, return_type=r_type,
                comments=cmts, comment_hash=c_hash,
                end_lineno=re_.end_lineno,
                loc=loc, complexity=cx, sketch=sk, n_shingles=n_sh)
            qual_to_cid[re_.qualname] = cid
            records.append({
                "qualname": re_.qualname, "kind": re_.kind,
                "lineno": re_.lineno, "end_lineno": re_.end_lineno,
                "signature": re_.signature,
                "parent_qualname": re_.parent_qualname,
                "body_hash": body_hash, "shape_hash": shape_hash,
                "logic_hash": logic_hash,
                "param_types": p_types, "return_type": r_type,
                "comments": cmts, "comment_hash": c_hash,
                "comment_rot": self.entities[cid].comment_rot,
                "loc": loc, "complexity": cx,
                "sketch": sk, "n_shingles": n_sh})
        self._raw_edges.extend(raw_edges)
        if new_cache is not None:
            from dataclasses import asdict
            new_cache[rel] = {"sha": fsha, "entities": records,
                              "edges": [asdict(e) for e in raw_edges]}

    def _assign(self, qualname, kind, path, lineno, body_hash, parent,
                old_by_qualname, old_entities, signature="",
                shape_hash="", logic_hash="", param_types=None,
                return_type="", comments=None, comment_hash="",
                comment_rot=False, end_lineno=0, loc=0, complexity=1,
                sketch=None, n_shingles=0) -> str:
        # Duplicate qualname within THIS pass (@overload stubs, try/except
        # defs, TYPE_CHECKING branches): salt with an occurrence index
        # instead of silently overwriting the earlier entity. File order
        # is deterministic, so occurrence keys persist across re-indexing.
        if qualname in self.by_qualname:
            occ = 2
            while f"{qualname}#{occ}" in self.by_qualname:
                occ += 1
            qualname = f"{qualname}#{occ}"
        # Same qualname as before -> keep the same coordinate ID.
        prev_e = old_entities.get(old_by_qualname.get(qualname, ""))
        if prev_e is not None and comments:
            prev_lh = getattr(prev_e, "logic_hash", "")
            prev_ch = getattr(prev_e, "comment_hash", "")
            if prev_ch != comment_hash:
                comment_rot = False          # comments touched: rot cleared
            elif prev_lh and logic_hash and prev_lh != logic_hash:
                comment_rot = True           # logic moved, comments did not
            else:
                comment_rot = getattr(prev_e, "comment_rot", False)
        if qualname in old_by_qualname:
            cid = old_by_qualname[qualname]
        else:
            cid = _new_id(qualname)
            # avoid collision with a live ID for a different qualname
            while cid in self.entities and self.entities[cid].qualname != qualname:
                cid = _new_id(qualname, salt=cid)
        self.entities[cid] = Entity(
            coord_id=cid, kind=kind, qualname=qualname, path=path,
            lineno=lineno, end_lineno=end_lineno, loc=loc,
            complexity=complexity, body_hash=body_hash, parent=parent,
            signature=signature, shape_hash=shape_hash, logic_hash=logic_hash,
            param_types=param_types or {}, return_type=return_type,
            comments=comments or [], comment_hash=comment_hash,
            comment_rot=comment_rot,
            sketch=sketch, n_shingles=n_shingles)
        self.by_qualname[qualname] = cid
        return cid

    # ------------------------------------------------------------------ save

    def save(self):
        idx_dir = self.coord_dir / "index"
        idx_dir.mkdir(parents=True, exist_ok=True)
        db = idx_dir / "coordinates.json"
        db.write_text(json.dumps(
            {cid: e.to_dict() for cid, e in sorted(self.entities.items())},
            indent=2))
        # D8: persist raw edges so harvest never re-indexes
        from dataclasses import asdict as _asdict
        _t = (idx_dir / "raw_edges.tmp")
        _t.write_text(json.dumps(
            [_asdict(r) for r in getattr(self, "_raw_edges", [])]))
        os.replace(_t, idx_dir / "raw_edges.json")

    def load_raw_edges(self):
        """Load raw edges, IGNORING fields this build does not know.

        A newer memway may have added a field - 0.54.3 added
        RawEdge.via_attr - and an older process reading that file used to
        die on `unexpected keyword argument`. Measured the hard way: a
        long-running MCP server, started before the upgrade, crashed on
        every call the moment the map was re-indexed by the new build.

        Unknown keys are dropped rather than refused. The old build then
        behaves exactly as it did before the field existed, which is the
        honest degradation - it cannot use information it has no code for,
        and refusing to start is a worse answer than working without it.
        """
        f = self.coord_dir / "index" / "raw_edges.json"
        if f.exists():
            from dataclasses import fields as _fields
            from .parsers import RawEdge
            known = {fl.name for fl in _fields(RawEdge)}
            self._raw_edges = [
                RawEdge(**{k: v for k, v in r.items() if k in known})
                for r in json.loads(f.read_text())]

    # ---------------------------------------------------------------- lookup

    def resolve(self, ref: str) -> Optional[Entity]:
        """Resolve a coordinate ID or qualname (or suffix of one).
        Phase A: suffix lookups go through a last-segment index instead
        of scanning all qualnames (O(1) vs O(entities) per call - the
        difference between 45s and unbounded on 59k-entity repos).

        Hybrid refs (file.py:name or path/file.py:name): strip the leading
        path component ending in .py and resolve the remainder against
        qualnames in that file's module."""
        # Hybrid ref support: "ratelimit.py:refill_bucket" or "path/ratelimit.py:refill_bucket"
        # Tiered matching: exact last-component beats suffix; shortest qualname wins ties.
        if ":" in ref and ref.count(":") == 1:
            path_part, name_part = ref.split(":", 1)
            # If path_part ends with .py, treat as hybrid ref
            if path_part.endswith(".py"):
                # Strip leading path components to get just the filename
                filename = path_part.split("/")[-1]
                # Collect all entities in the matching file
                exact_matches = []
                suffix_matches = []
                for q, cid in self.by_qualname.items():
                    e = self.entities[cid]
                    # Check if this entity's path matches the filename
                    if e.path.endswith(path_part) or e.path.endswith(filename):
                        # Tier 1: exact last-component match
                        if q.rsplit(".", 1)[-1] == name_part:
                            exact_matches.append((len(q), q, cid))
                        # Tier 2: suffix match (but not already exact)
                        elif q.endswith(name_part) or q.endswith("." + name_part):
                            suffix_matches.append((len(q), q, cid))
                # Prefer exact matches, then suffix matches; shortest qualname wins
                if exact_matches:
                    exact_matches.sort()  # sort by (length, qualname, cid)
                    return self.entities[exact_matches[0][2]]
                if suffix_matches:
                    suffix_matches.sort()
                    return self.entities[suffix_matches[0][2]]
                # If no match found with hybrid ref, fall through to normal resolution

        if ref in self.entities:
            return self.entities[ref]
        if ref in self.by_qualname:
            return self.entities[self.by_qualname[ref]]
        if not hasattr(self, "_suffix_index") or \
                len(self._suffix_seen) != len(self.by_qualname):
            from collections import defaultdict
            self._suffix_index = defaultdict(list)
            for q, cid in self.by_qualname.items():
                self._suffix_index[q.rsplit(".", 1)[-1]].append((q, cid))
            self._suffix_seen = dict(self.by_qualname)
        last = ref.rsplit(".", 1)[-1]
        matches = [cid for q, cid in self._suffix_index.get(last, [])
                   if q.endswith(ref)]
        if len(matches) == 1:
            return self.entities[matches[0]]
        # D11b: ambiguous bare names - if exactly one candidate is
        # production code, prefer it over test fixtures/helpers
        if len(matches) > 1:
            from .verify import is_test_entity
            prod = [c for c in matches
                    if not is_test_entity(self.entities[c])]
            if len(prod) == 1:
                return self.entities[prod[0]]
        return None

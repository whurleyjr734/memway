"""
probe: exec on steroids - run an entity with real values and get the
data flow back as coordinates, not just a return value.

An agent calls probe(ref, args) and receives:
  - the return value (or the exception, with the COORDINATE that raised)
  - the call tree: every repo entity the flow entered, in order, with
    depth, argument reprs, and per-entity return reprs
  - flow_vs_map: observed caller->callee pairs diffed against the static
    edge graph:
      confirmed  - runtime agreed with a static edge
      discovered - runtime saw a call the static graph could not resolve
                   (untyped receivers, dynamic dispatch). With
                   record=True these are written back as
                   resolution="runtime", confidence 1.0 - execution
                   closing the static-analysis gap.

Value passing: args/kwargs are plain JSON values, or strings of the
form "$expr:<python>" evaluated in the namespace built by `setup`
(e.g. setup="from app.models import User; u=User('x')",
args=["$expr:u"]). This is how agents probe methods: pass the instance
as the first arg.

Trust boundary: identical to exec - this RUNS repo code with the given
values. Point it only at code you would run anyway.
"""
from __future__ import annotations

import importlib
import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

_MAX_REPR = 120


def _r(v) -> str:
    try:
        s = repr(v)
    except Exception as e:                                  # repr can raise
        s = f"<unrepr-able {type(v).__name__}: {e}>"
    return s if len(s) <= _MAX_REPR else s[: _MAX_REPR - 3] + "..."


def _entity_index(indexer):
    """(path, lineno-range) -> entity, for mapping frames to coordinates."""
    by_file: dict[str, list] = {}
    for cid, e in indexer.entities.items():
        if e.kind in ("function", "method"):
            by_file.setdefault(e.path, []).append(e)
    for lst in by_file.values():
        lst.sort(key=lambda e: e.lineno)
    return by_file


def _locate(by_file, repo_root: Path, filename: str, lineno: int):
    try:
        rel = str(Path(filename).resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return None
    best = None
    for e in by_file.get(rel, []):
        if e.lineno <= lineno <= (e.end_lineno or e.lineno):
            if best is None or e.lineno > best.lineno:      # innermost wins
                best = e
    return best


def _resolve_callable(indexer, ref, namespaces):
    ent = indexer.resolve(ref)
    if ent is None:
        raise LookupError(f"no entity matches {ref!r}")
    parts = ent.qualname.split("#")[0].split(".")
    # walk module path -> attribute path; also try skipping leading
    # path components ("src." in src-layouts is not an importable pkg)
    for start in range(min(3, len(parts))):
        for split in range(len(parts), start, -1):
            mod_name = ".".join(parts[start:split])
            try:
                obj = importlib.import_module(mod_name)
            except ImportError:
                continue
            # namespace-package trap: "src.flask.testing" imports the SAME
            # file as "flask.testing" but as a DUPLICATE module identity
            # (src/ has no __init__, so it "imports" as a namespace pkg).
            # A method resolved through the duplicate breaks super() and
            # isinstance against objects built in `setup`. Reject any
            # resolution whose top-level package is a namespace package.
            top = sys.modules.get(mod_name.split(".")[0])
            if top is not None and getattr(top, "__file__", None) is None:
                continue    # namespace pkg root: the walk would reach a
                            # duplicate class identity - reject outright
            try:
                for attr in parts[split:]:
                    obj = getattr(obj, attr)
                return ent, obj
            except AttributeError:
                continue
    # last resort: the setup namespace may hold it
    for ns in namespaces:
        if parts[-1] in ns:
            return ent, ns[parts[-1]]
    raise ImportError(f"could not import a callable for {ent.qualname}")


def probe(indexer, edges, repo_root, ref: str,
          args: list | None = None, kwargs: dict | None = None,
          setup: str = "", record: bool = False,
          max_events: int = 2000) -> dict:
    repo_root = Path(repo_root)
    for p in (repo_root, repo_root / "src"):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))

    ns: dict = {}
    if setup:
        exec(setup, ns)                                     # agent-supplied

    def _val(v):
        if isinstance(v, str) and v.startswith("$expr:"):
            return eval(v[6:], ns)
        return v

    args = [_val(a) for a in (args or [])]
    kwargs = {k: _val(v) for k, v in (kwargs or {}).items()}

    ent, fn = _resolve_callable(indexer, ref, [ns])
    by_file = _entity_index(indexer)

    events: list[dict] = []
    observed: set[tuple[str, str]] = set()
    stack: list = []                                        # entity per frame
    truncated = False

    def tracer(frame, event, arg):
        nonlocal truncated
        if len(events) >= max_events:
            truncated = True
            return None
        if event == "call":
            e = _locate(by_file, repo_root, frame.f_code.co_filename,
                        frame.f_lineno)
            if e is None:
                stack.append(None)
                return tracer
            caller = next((s for s in reversed(stack) if s), None)
            if caller and caller.coord_id != e.coord_id:
                observed.add((caller.coord_id, e.coord_id))
            try:
                arg_r = {k: _r(v) for k, v in list(frame.f_locals.items())[:6]}
            except Exception:
                arg_r = {}
            events.append({"event": "enter", "qualname": e.qualname,
                           "coord": e.coord_id, "depth": len(
                               [s for s in stack if s]) + 1, "args": arg_r})
            stack.append(e)
            return tracer
        if event == "return" and stack:
            e = stack.pop()
            if e is not None:
                events.append({"event": "return", "qualname": e.qualname,
                               "value": _r(arg)})
        elif event == "exception" and stack:
            e = stack[-1]
            if e is not None:
                events.append({"event": "exception", "qualname": e.qualname,
                               "error": _r(arg[1])})
        return tracer

    out_buf = io.StringIO()
    result: dict = {"target": ent.qualname, "coord": ent.coord_id}
    sys.settrace(tracer)
    try:
        with redirect_stdout(out_buf):
            rv = fn(*args, **kwargs)
        result["return"] = _r(rv)
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["exception"] = {"type": type(e).__name__, "message": str(e)}
        tb = traceback.extract_tb(e.__traceback__)
        for fr in reversed(tb):
            loc = _locate(by_file, repo_root, fr.filename, fr.lineno)
            if loc:
                result["exception"]["raised_at"] = \
                    f"{loc.qualname} ({loc.coord_id}) line {fr.lineno}"
                break
    finally:
        sys.settrace(None)

    static = {(e["src"], e["dst"]) for e in edges if e["kind"] == "calls"}
    confirmed = observed & static
    discovered = observed - static
    qn = lambda cid: getattr(indexer.entities.get(cid), "qualname", cid)
    result.update({
        "flow": events,
        "truncated": truncated,
        "stdout": out_buf.getvalue()[-500:],
        "flow_vs_map": {
            "entities_touched": len({ev["coord"] for ev in events
                                     if ev["event"] == "enter"}),
            "confirmed_edges": len(confirmed),
            "discovered_edges": [
                {"src": qn(s), "dst": qn(d)} for s, d in sorted(discovered)],
        },
    })

    if record and discovered:
        new = [{"src": s, "dst": d, "kind": "calls",
                "confidence": 1.0, "resolution": "runtime"}
               for s, d in discovered]
        from .edges import EdgeBuilder
        existing = EdgeBuilder.load(repo_root / ".coord") or list(edges)
        seen_keys = {(e["src"], e["dst"], e["kind"]) for e in existing}
        added = [e for e in new
                 if (e["src"], e["dst"], e["kind"]) not in seen_keys]
        if added:
            eb = EdgeBuilder(indexer)
            eb.edges = existing + added
            eb.save(repo_root / ".coord")
        result["flow_vs_map"]["recorded"] = len(added)

    return result

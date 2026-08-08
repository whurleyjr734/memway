"""
Language plugins.

Everything above this layer (coordinates, metadata, lineage, versioning,
agents, traversal) is language-agnostic. A language plugin only has to
answer two questions about a source file:

  1. What entities exist?   -> list of RawEntity
  2. How do they connect?   -> list of RawEdge

To add a language: subclass LanguageParser, implement parse(), and
register it in PARSERS. Tree-sitter grammars exist for ~40 languages,
so most plugins are a node-type mapping plus an import/call convention.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RawEntity:
    kind: str          # module | class | function | method | attribute
    qualname: str      # dotted path, language-normalized
    lineno: int
    end_lineno: int    # exact extent from the parser (D1: never re-derived)
    body_text: str     # raw source of the entity (hashed upstream)
    short_name: str    # for shape-hash rename detection
    signature: str = ""
    parent_qualname: str = ""
    doc: str = ""      # leading doc comment. Brace-family languages put
                       # documentation ABOVE the declaration, so unlike a
                       # Python docstring it is not inside body_text and
                       # has to be carried out of the parser explicitly.


@dataclass
class RawEdge:
    src_qualname: str
    dst_ref: str       # qualname, bare name (resolved upstream), or EVT:<name>
    kind: str          # imports | calls | emits | consumes


class LanguageParser:
    extensions: tuple = ()
    language: str = ""

    def parse(self, path: Path, repo_root: Path) -> tuple[list, list]:
        """Return (entities, edges) for one file."""
        raise NotImplementedError

    @staticmethod
    def module_qualname(path: Path, repo_root: Path) -> str:
        return ".".join(path.relative_to(repo_root).with_suffix("").parts)


# --------------------------------------------------------------------------
# Python plugin: stdlib ast (no dependencies, richest resolution)
# --------------------------------------------------------------------------

import ast

EVENT_EMIT_FUNCS = {"emit", "publish", "dispatch"}
EVENT_SUB_FUNCS = {"subscribe", "on", "consume", "register_handler"}


class PythonParser(LanguageParser):
    extensions = (".py",)
    language = "python"

    def parse(self, path: Path, repo_root: Path):
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [], []
        lines = source.splitlines()
        mod = self.module_qualname(path, repo_root)
        entities = [RawEntity("module", mod, 1, len(lines) or 1, source,
                              mod.rsplit(".", 1)[-1])]
        edges: list[RawEdge] = []

        def seg(node):
            return "\n".join(lines[node.lineno - 1: node.end_lineno])

        def walk(node, parent_qual, in_class):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{parent_qual}.{child.name}"
                    kind = "method" if in_class else "function"
                    sig = f"{child.name}({', '.join(a.arg for a in child.args.args)})"
                    entities.append(RawEntity(kind, q, child.lineno,
                                              child.end_lineno,
                                              seg(child), child.name, sig,
                                              parent_qual))
                    walk(child, q, False)
                elif isinstance(child, ast.ClassDef):
                    q = f"{parent_qual}.{child.name}"
                    entities.append(RawEntity("class", q, child.lineno,
                                              child.end_lineno,
                                              seg(child), child.name, "",
                                              parent_qual))
                    walk(child, q, True)
                elif in_class and isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = (child.targets if isinstance(child, ast.Assign)
                               else [child.target])
                    for t in targets:
                        if isinstance(t, ast.Name):
                            entities.append(RawEntity(
                                "attribute", f"{parent_qual}.{t.id}",
                                child.lineno, child.end_lineno,
                                seg(child), t.id, "",
                                parent_qual))

        walk(tree, mod, False)

        # imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    edges.append(RawEdge(mod, a.name, "imports"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                edges.append(RawEdge(mod, node.module, "imports"))
                for a in node.names:
                    edges.append(RawEdge(mod, f"{node.module}.{a.name}",
                                         "imports"))

        # calls / events attributed to enclosing scope
        class V(ast.NodeVisitor):
            def __init__(v):
                v.stack = [mod]
                v.class_stack = []          # quals of enclosing classes
                v.var_types = [{}]          # per-function: name -> Type

            def visit_ClassDef(v, n):
                q = f"{v.stack[-1]}.{n.name}"
                v.stack.append(q)
                v.class_stack.append(q)
                v.generic_visit(n)
                v.class_stack.pop()
                v.stack.pop()

            def visit_FunctionDef(v, n):
                v.stack.append(f"{v.stack[-1]}.{n.name}")
                # annotation-assisted typing: `def f(s: Session)` means
                # s.request() is Session.request - the type fact is
                # ALREADY in the source; using it is not inference.
                scope = {}
                for a in (list(n.args.posonlyargs) + list(n.args.args)
                          + list(n.args.kwonlyargs)):
                    ann = a.annotation
                    # NOTE (2026-08): blind unwrap of Optional/union/string
                    # annotations was tried here and REVERTED - A/B on flask
                    # showed it manufacturing 12 guess-tier edges (common
                    # annotation words -> dotted refs -> unique-bare cascade).
                    # The parser cannot verify a type resolves to a real
                    # in-repo class; only edge-building can. Forward-ref
                    # resolution belongs in the edges 'annotated' tier,
                    # which has that guard.
                    if isinstance(ann, ast.Name):
                        scope[a.arg] = ann.id
                    elif (isinstance(ann, ast.Attribute)):
                        scope[a.arg] = ann.attr
                v.var_types.append(scope)
                v.generic_visit(n)
                v.var_types.pop()
                v.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(v, n):
                fname = None
                dst_override = None
                if isinstance(n.func, ast.Name):
                    fname = n.func.id
                elif isinstance(n.func, ast.Attribute):
                    fname = n.func.attr
                    base = n.func.value
                    if isinstance(base, ast.Name):
                        if base.id in ("self", "cls") and v.class_stack:
                            # self.x() -> the ENCLOSING class's x,
                            # fully qualified: exact resolution, no
                            # cross-class ambiguity possible.
                            dst_override = f"{v.class_stack[-1]}.{fname}"
                        elif base.id in v.var_types[-1]:
                            # annotated param: qualify by declared type
                            dst_override = (f"{v.var_types[-1][base.id]}"
                                            f".{fname}")
                if not fname:
                    return v.generic_visit(n)
                src = v.stack[-1]
                if fname in EVENT_EMIT_FUNCS | EVENT_SUB_FUNCS:
                    kind = ("emits" if fname in EVENT_EMIT_FUNCS
                            else "consumes")
                    if n.args and isinstance(n.args[0], ast.Constant) \
                            and isinstance(n.args[0].value, str):
                        edges.append(RawEdge(src, f"EVT:{n.args[0].value}",
                                             kind))
                    else:
                        # dynamic event name (f-string, variable,
                        # concatenation): the edge target is unknowable
                        # statically. Record VISIBLE uncertainty rather
                        # than silently dropping the edge - blast and
                        # traversal surface it as an honest unknown.
                        edges.append(RawEdge(src, "EVT:<dynamic>", kind))
                else:
                    edges.append(RawEdge(src, dst_override or fname,
                                         "calls"))
                v.generic_visit(n)

        V().visit(tree)
        return entities, edges


# --------------------------------------------------------------------------
# JavaScript plugin: tree-sitter
# --------------------------------------------------------------------------

class JavaScriptParser(LanguageParser):
    """JS/TS extraction: declarations, classes, methods, arrow functions,
    imports and call edges - plus each entity's signature and its leading
    doc comment, which TS puts above the declaration rather than inside
    the body the way Python does."""
    extensions = (".js", ".mjs", ".jsx")
    language = "javascript"

    def __init__(self):
        import tree_sitter_javascript
        from tree_sitter import Language, Parser
        self.parser = Parser(Language(tree_sitter_javascript.language()))

    def parse(self, path: Path, repo_root: Path):
        source = path.read_bytes()
        tree = self.parser.parse(source)
        text = source.decode(errors="replace")
        mod = self.module_qualname(path, repo_root)
        entities = [RawEntity("module", mod, 1,
                              text.count("\n") + 1, text,
                              mod.rsplit(".", 1)[-1])]
        edges: list[RawEdge] = []

        def node_text(n):
            return source[n.start_byte:n.end_byte].decode(errors="replace")

        def name_of(n):
            for c in n.children:
                if c.type in ("identifier", "property_identifier",
                              "type_identifier"):
                    return node_text(c)
            return None

        def leading_doc(n):
            """Contiguous comment lines immediately above a declaration."""
            out, line, pv = [], n.start_point[0], n.prev_sibling
            while pv is not None and pv.type == "comment" \
                    and pv.end_point[0] >= line - 1:
                out.append(node_text(pv))
                line, pv = pv.start_point[0], pv.prev_sibling
            return "\n".join(reversed(out))

        def js_sig(n, nm, value=None):
            """name(params): ret - the caller-visible contract.

            The TypeScript grammar exposes return_type; plain JS simply has
            none, so the annotation is included when the source declares it
            and omitted when it does not. Without this the whole .js/.ts
            tier carries signature="" and before_edit cannot describe an
            entity it is briefing you on.
            """
            src = value if value is not None else n
            params = src.child_by_field_name("parameters")
            if params is None:
                params = src.child_by_field_name("parameter")
            out = nm + (node_text(params) if params is not None else "()")
            ret = src.child_by_field_name("return_type")
            if ret is not None:
                out += node_text(ret)
            return " ".join(out.split())

        def walk(node, parent_qual, in_class):
            for c in node.children:
                t = c.type
                if t in ("function_declaration", "generator_function_declaration"):
                    nm = name_of(c)
                    if nm:
                        q = f"{parent_qual}.{nm}"
                        entities.append(RawEntity(
                            "function", q, c.start_point[0] + 1,
                            c.end_point[0] + 1,
                            node_text(c), nm, signature=js_sig(c, nm),
                            doc=leading_doc(c), parent_qualname=parent_qual))
                        walk(c, q, False)
                        continue
                elif t == "class_declaration":
                    nm = name_of(c)
                    if nm:
                        q = f"{parent_qual}.{nm}"
                        entities.append(RawEntity(
                            "class", q, c.start_point[0] + 1,
                            c.end_point[0] + 1,
                            node_text(c), nm, parent_qualname=parent_qual))
                        walk(c, q, True)
                        continue
                elif t == "method_definition" and in_class:
                    nm = name_of(c)
                    if nm:
                        q = f"{parent_qual}.{nm}"
                        entities.append(RawEntity(
                            "method", q, c.start_point[0] + 1,
                            c.end_point[0] + 1,
                            node_text(c), nm, signature=js_sig(c, nm),
                            doc=leading_doc(c), parent_qualname=parent_qual))
                        walk(c, q, False)
                        continue
                elif t in ("lexical_declaration", "variable_declaration"):
                    # const f = () => {} / const f = function () {}
                    for d in c.children:
                        if d.type == "variable_declarator":
                            nm = name_of(d)
                            val = d.child_by_field_name("value")
                            if nm and val is not None and val.type in (
                                    "arrow_function", "function_expression",
                                    "function"):
                                q = f"{parent_qual}.{nm}"
                                entities.append(RawEntity(
                                    "function", q, c.start_point[0] + 1,
                                    c.end_point[0] + 1,
                                    node_text(c), nm,
                                    signature=js_sig(c, nm, val),
                                    doc=leading_doc(c),
                                    parent_qualname=parent_qual))
                                walk(val, q, False)
                walk(c, parent_qual, in_class)

        walk(tree.root_node, mod, False)

        # imports + calls + events via a second pass with a scope stack
        def scope_walk(node, stack):
            for c in node.children:
                t = c.type
                child_stack = stack
                if t in ("function_declaration", "class_declaration",
                         "method_definition"):
                    nm = name_of(c)
                    if nm:
                        child_stack = stack + [nm]
                if t == "import_statement":
                    src_node = c.child_by_field_name("source")
                    if src_node is not None:
                        spec = node_text(src_node).strip("'\"")
                        # normalize ./relative paths to dotted qualnames
                        if spec.startswith("."):
                            base = (path.parent / spec).resolve()
                            try:
                                rel = base.relative_to(repo_root.resolve())
                                spec = ".".join(rel.with_suffix("").parts)
                            except ValueError:
                                pass
                        edges.append(RawEdge(mod, spec, "imports"))
                elif t == "call_expression":
                    fn = c.child_by_field_name("function")
                    fname = None
                    if fn is not None:
                        if fn.type == "identifier":
                            fname = node_text(fn)
                        elif fn.type == "member_expression":
                            prop = fn.child_by_field_name("property")
                            fname = node_text(prop) if prop is not None else None
                    if fname:
                        src_qual = (".".join([mod] + child_stack[1:])
                                    if len(child_stack) > 1 else mod)
                        args = c.child_by_field_name("arguments")
                        first_str = None
                        if args is not None:
                            for a in args.children:
                                if a.type == "string":
                                    first_str = node_text(a).strip("'\"")
                                    break
                        if fname in EVENT_EMIT_FUNCS and first_str:
                            edges.append(RawEdge(src_qual,
                                                 f"EVT:{first_str}", "emits"))
                        elif fname in EVENT_EMIT_FUNCS:
                            edges.append(RawEdge(src_qual,
                                                 "EVT:<dynamic>", "emits"))
                        elif fname in EVENT_SUB_FUNCS and first_str:
                            edges.append(RawEdge(src_qual,
                                                 f"EVT:{first_str}", "consumes"))
                        elif fname in EVENT_SUB_FUNCS:
                            edges.append(RawEdge(src_qual,
                                                 "EVT:<dynamic>", "consumes"))
                        else:
                            edges.append(RawEdge(src_qual, fname, "calls"))
                scope_walk(c, child_stack)

        scope_walk(tree.root_node, [mod])
        return entities, edges


# --------------------------------------------------------------------------

class TypeScriptParser(JavaScriptParser):
    """Deep TS: the JS extraction walks TS's superset AST; interfaces
    and type aliases become entities so knowledge can attach to
    contracts, not just code."""
    extensions = (".ts", ".tsx")
    language = "typescript"

    def __init__(self):
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
        self.parser = Parser(
            Language(tree_sitter_typescript.language_typescript()))
        self._tsx = Parser(
            Language(tree_sitter_typescript.language_tsx()))

    def parse(self, path: Path, repo_root: Path):
        main = self.parser
        if path.suffix == ".tsx":
            self.parser = self._tsx
        try:
            ents, edges = super().parse(path, repo_root)
        finally:
            self.parser = main
        source = path.read_bytes()
        tree = (self._tsx if path.suffix == ".tsx"
                else self.parser).parse(source)
        mod = self.module_qualname(path, repo_root)

        def walk(n):
            if n.type in ("interface_declaration",
                          "type_alias_declaration"):
                name = None
                for c in n.children:
                    if c.type == "type_identifier":
                        name = source[c.start_byte:c.end_byte].decode()
                        break
                if name:
                    body = source[n.start_byte:n.end_byte].decode(
                        errors="replace")
                    ents.append(RawEntity(
                        "class", f"{mod}.{name}",
                        n.start_point[0] + 1, n.end_point[0] + 1,
                        body, name))
                    edges.append(RawEdge(mod, f"{mod}.{name}",
                                         "contains"))
            for c in n.children:
                walk(c)
        walk(tree.root_node)
        return ents, edges


class GoParser(LanguageParser):
    """Deep Go: functions, receiver-qualified methods (Type.Method),
    structs/interfaces, imports, call edges. Each callable also carries
    its `name(params) result` signature and the doc comment block
    immediately above its declaration."""
    extensions = (".go",)
    language = "go"

    def __init__(self):
        import tree_sitter_go
        from tree_sitter import Language, Parser
        self.parser = Parser(Language(tree_sitter_go.language()))

    def parse(self, path: Path, repo_root: Path):
        source = path.read_bytes()
        tree = self.parser.parse(source)
        text = source.decode(errors="replace")
        mod = self.module_qualname(path, repo_root)
        ents = [RawEntity("module", mod, 1, text.count("\n") + 1,
                          text, mod.rsplit(".", 1)[-1])]
        edges: list[RawEdge] = []

        def ntext(n):
            return source[n.start_byte:n.end_byte].decode(
                errors="replace")

        def leading_doc(n, txt):
            """Contiguous comment lines immediately above a declaration."""
            out, line, pv = [], n.start_point[0], n.prev_sibling
            while pv is not None and pv.type == "comment" \
                    and pv.end_point[0] >= line - 1:
                out.append(txt(pv))
                line, pv = pv.start_point[0], pv.prev_sibling
            return "\n".join(reversed(out))

        def sig(n, nm):
            """name(params) result - the caller-visible contract.

            Without this every Go entity carries signature="", so a
            before_edit briefing cannot state what the function takes or
            returns and the agent has to open the file anyway.
            """
            params = n.child_by_field_name("parameters")
            out = ntext(nm) + (ntext(params) if params is not None else "()")
            result = n.child_by_field_name("result")
            if result is not None:
                out += " " + ntext(result)
            return " ".join(out.split())

        def add_calls(scope, n):
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None:
                    callee = ntext(fn).split("(")[0].rsplit(".", 1)[-1]
                    if callee and callee[0].isalpha():
                        edges.append(RawEdge(scope, callee, "calls"))
            for c in n.children:
                add_calls(scope, c)

        def walk(n):
            if n.type == "function_declaration":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    q = f"{mod}.{ntext(nm)}"
                    ents.append(RawEntity("function", q,
                        n.start_point[0] + 1, n.end_point[0] + 1,
                        ntext(n), ntext(nm), signature=sig(n, nm),
                        doc=leading_doc(n, ntext)))
                    edges.append(RawEdge(mod, q, "contains"))
                    add_calls(q, n)
                return
            if n.type == "method_declaration":
                nm = n.child_by_field_name("name")
                recv = n.child_by_field_name("receiver")
                rtype = ""
                if recv is not None:
                    rtype = ntext(recv).strip("()").split()[-1]                         .lstrip("*")
                if nm is not None:
                    q = (f"{mod}.{rtype}.{ntext(nm)}" if rtype
                         else f"{mod}.{ntext(nm)}")
                    ents.append(RawEntity("method", q,
                        n.start_point[0] + 1, n.end_point[0] + 1,
                        ntext(n), ntext(nm), signature=sig(n, nm),
                        doc=leading_doc(n, ntext)))
                    edges.append(RawEdge(mod, q, "contains"))
                    add_calls(q, n)
                return
            if n.type == "type_declaration":
                for spec in n.children:
                    if spec.type == "type_spec":
                        nm = spec.child_by_field_name("name")
                        if nm is not None:
                            q = f"{mod}.{ntext(nm)}"
                            ents.append(RawEntity("class", q,
                                spec.start_point[0] + 1,
                                spec.end_point[0] + 1,
                                ntext(spec), ntext(nm)))
                            edges.append(RawEdge(mod, q, "contains"))
            if n.type == "import_spec":
                p = n.child_by_field_name("path")
                if p is not None:
                    edges.append(RawEdge(mod,
                        ntext(p).strip('"').rsplit("/", 1)[-1],
                        "imports"))
            for c in n.children:
                walk(c)
        walk(tree.root_node)
        return ents, edges


class JavaParser(LanguageParser):
    """Deep Java: packages, classes (incl. nested, dotted), interfaces,
    enums, methods with OVERLOAD DISAMBIGUATION (arity suffix:
    Type.method/2), constructors, imports, call edges. Generics and
    annotations are treated as body text - honest scope."""
    extensions = (".java",)
    language = "java"

    def __init__(self):
        import tree_sitter_java
        from tree_sitter import Language, Parser
        self.parser = Parser(Language(tree_sitter_java.language()))

    def parse(self, path: Path, repo_root: Path):
        source = path.read_bytes()
        tree = self.parser.parse(source)
        text = source.decode(errors="replace")
        mod = self.module_qualname(path, repo_root)
        ents = [RawEntity("module", mod, 1, text.count("\n") + 1,
                          text, mod.rsplit(".", 1)[-1])]
        edges: list[RawEdge] = []

        def ntext(n):
            return source[n.start_byte:n.end_byte].decode(
                errors="replace")

        def add_calls(scope, n):
            if n.type == "method_invocation":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    edges.append(RawEdge(scope, ntext(nm), "calls"))
            if n.type == "object_creation_expression":
                t = n.child_by_field_name("type")
                if t is not None:
                    edges.append(RawEdge(scope,
                        ntext(t).split("<")[0], "calls"))
            for c in n.children:
                add_calls(scope, c)

        def arity(n):
            ps = n.child_by_field_name("parameters")
            if ps is None:
                return 0
            return sum(1 for c in ps.children
                       if c.type in ("formal_parameter",
                                     "spread_parameter"))

        def walk(n, owner):
            if n.type in ("class_declaration", "interface_declaration",
                          "enum_declaration", "record_declaration"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    q = f"{owner}.{ntext(nm)}"
                    ents.append(RawEntity("class", q,
                        n.start_point[0] + 1, n.end_point[0] + 1,
                        ntext(n), ntext(nm)))
                    edges.append(RawEdge(owner, q, "contains"))
                    body = n.child_by_field_name("body")
                    if body is not None:
                        for c in body.children:
                            walk(c, q)
                return
            if n.type in ("method_declaration",
                          "constructor_declaration"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    name = ntext(nm)
                    a = arity(n)
                    # overload disambiguation: arity suffix only when
                    # needed would require two passes; always-suffix
                    # keeps identity stable as overloads come and go
                    q = f"{owner}.{name}/{a}"
                    ents.append(RawEntity("method", q,
                        n.start_point[0] + 1, n.end_point[0] + 1,
                        ntext(n), name))
                    edges.append(RawEdge(owner, q, "contains"))
                    add_calls(q, n)
                return
            if n.type == "import_declaration":
                edges.append(RawEdge(mod,
                    ntext(n).replace("import", "").strip(" ;")
                    .rsplit(".", 1)[-1], "imports"))
            for c in n.children:
                walk(c, owner)

        walk(tree.root_node, mod)
        return ents, edges


# Bump whenever ANY parser's extraction logic changes: a warm parse
# cache from an older parser silently replays stale entities/edges,
# so the cache is versioned by this schema and discarded on mismatch.
PARSE_SCHEMA_VERSION = 3      # 2: scope-aware call resolution
                              # 3: signatures + leading doc comments
                              #    for go/js/ts entities

PARSERS: dict[str, LanguageParser] = {}
_PARSER_ERRORS: dict[str, str] = {}


def get_parsers(verbose: bool = False) -> dict[str, LanguageParser]:
    """Extension -> parser instance, lazily constructed.

    A grammar can fail to load two ways: not installed (ImportError) or
    a binary/runtime version mismatch (ValueError from tree-sitter, e.g.
    'Incompatible Language version'). BOTH are caught so one bad grammar
    never kills the others - but the failure is RECORDED and surfaced,
    never silent (a silently-missing language looks like a repo with no
    code in it, which is worse than an error).
    """
    if not PARSERS:
        for cls in (PythonParser, JavaScriptParser,
                    TypeScriptParser, GoParser,
                    JavaParser):
            try:
                p = cls()
            except ImportError as e:
                _PARSER_ERRORS[cls.language] = f"not installed ({e})"
                continue
            except Exception as e:            # version mismatch, etc.
                _PARSER_ERRORS[cls.language] = f"{type(e).__name__}: {e}"
                continue
            for ext in cls.extensions:
                PARSERS[ext] = p
    if verbose and _PARSER_ERRORS:
        import sys
        for lang, err in _PARSER_ERRORS.items():
            sys.stderr.write(f"memway: {lang} parser unavailable - "
                             f"{err}\n")
    return PARSERS

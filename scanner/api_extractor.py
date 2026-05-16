"""
Extract HTTP API endpoints from source code, by regex on the file
contents. Two ecosystems covered:

  - Spring (Java): @RestController / @Controller classes with
    @RequestMapping at class level + @GetMapping / @PostMapping /
    @PutMapping / @DeleteMapping / @PatchMapping / @RequestMapping at
    method level. The resulting endpoint path is class_mapping +
    method_mapping.

  - Express (TS/JS): app.get / router.post / app.put / etc.

Other frameworks (FastAPI, Flask, Gin) are extension points listed in
PROGRESS.md design-doc-future-work.

Output shape: list of dicts:
  {
    "method":  "GET" | "POST" | "PUT" | "DELETE" | "PATCH",
    "path":    "/api/pets/{id}/visits",
    "handler": "VisitController.findByPet",     # class.method or function name
    "file":    "src/main/java/.../VisitController.java",  # where DECLARED
    "line":    87,
    "framework": "spring" | "express" | "openapi",
    "source_file": "src/.../VisitRestController.java",    # where IMPLEMENTED
    "registered":  True,   # False = spec'd but no handler found in code
  }

`source_file` + `registered` are filled by _annotate_impl: for an
openapi-spec endpoint the spec only states the contract, so we locate
the hand-written controller that implements it. An endpoint with no
implementation (`registered: False`) is one the verify slice should not
generate tests for.
"""

from __future__ import annotations

import os
import re
import json
from pathlib import Path

try:
    import yaml  # PyYAML — optional, only needed for OpenAPI YAML
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ---------- Spring patterns ---------------------------------------------

_SPRING_CONTROLLER_RE = re.compile(
    r"@(?:Rest)?Controller\b",
    re.MULTILINE,
)
_SPRING_CLASS_MAPPING_RE = re.compile(
    # Matches @RequestMapping at class level with optional path/value attr.
    # Stops at the closing paren or quote.
    r"@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\"",
)
_SPRING_CLASS_DECL_RE = re.compile(
    r"\b(?:public\s+)?(?:abstract\s+|final\s+)?class\s+(\w+)",
)

# Per-method mapping annotations. The verb is captured from the annotation name.
_SPRING_METHOD_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch)Mapping"
    r"(?:\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\")?",
)
# Generic @RequestMapping at method level (rare but possible).
_SPRING_GENERIC_METHOD_RE = re.compile(
    r"@RequestMapping\s*\(\s*"
    r"(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\""
    r"[^)]*?method\s*=\s*RequestMethod\.(GET|POST|PUT|DELETE|PATCH)",
    re.DOTALL,
)
_SPRING_METHOD_DECL_RE = re.compile(
    # Matches `public ResponseEntity<X> doSomething(...`
    r"(?:public|protected|private)\s+(?:[\w<>?,\s\[\]]+\s+)?(\w+)\s*\("
)


def _extract_spring(file_path: Path, content: str, repo_root: Path) -> list[dict]:
    if not _SPRING_CONTROLLER_RE.search(content):
        return []

    # Class-level base path. Default to "" if not declared.
    class_match = _SPRING_CLASS_DECL_RE.search(content)
    class_name = class_match.group(1) if class_match else file_path.stem

    base_path = ""
    for m in _SPRING_CLASS_MAPPING_RE.finditer(content):
        # Walk back from the match to confirm it's at class level (not inside
        # a method). Heuristic: if it appears before the `class X` declaration,
        # it's class-level.
        if class_match and m.start() < class_match.start():
            base_path = m.group(1)
            break

    apis: list[dict] = []
    rel_path = str(file_path.relative_to(repo_root))

    # Per-method @*Mapping annotations
    for m in _SPRING_METHOD_RE.finditer(content):
        verb = m.group(1).upper()
        path = m.group(2) or ""
        method_name = _find_method_after(content, m.end())
        line = content.count("\n", 0, m.start()) + 1
        apis.append({
            "method":   verb,
            "path":     _join_paths(base_path, path),
            "handler":  f"{class_name}.{method_name}" if method_name else class_name,
            "file":     rel_path,
            "line":     line,
            "framework": "spring",
        })

    # Generic @RequestMapping(method=...) at method level
    for m in _SPRING_GENERIC_METHOD_RE.finditer(content):
        path = m.group(1)
        verb = m.group(2).upper()
        method_name = _find_method_after(content, m.end())
        line = content.count("\n", 0, m.start()) + 1
        apis.append({
            "method":   verb,
            "path":     _join_paths(base_path, path),
            "handler":  f"{class_name}.{method_name}" if method_name else class_name,
            "file":     rel_path,
            "line":     line,
            "framework": "spring",
        })

    return apis


def _find_method_after(content: str, offset: int) -> str:
    """Find the next method declaration after `offset`. Returns the method
    name, or empty string if not found within a reasonable window."""
    window = content[offset:offset + 800]
    m = _SPRING_METHOD_DECL_RE.search(window)
    return m.group(1) if m else ""


def _join_paths(base: str, sub: str) -> str:
    base = base.rstrip("/")
    sub  = sub.lstrip("/")
    if not base and not sub:
        return "/"
    if not base:
        return "/" + sub
    if not sub:
        return base
    return f"{base}/{sub}"


# ---------- Express patterns -------------------------------------------

# Matches things like:
#   app.get('/foo', handler)
#   router.post("/bar/:id", handler)
_EXPRESS_RE = re.compile(
    r"\b(?:app|router)\."
    r"(get|post|put|delete|patch)\s*\("
    r"\s*['\"]([^'\"]+)['\"]"
)

# Surrounding function name (rough): the closest preceding `function NAME` or
# `const NAME = ` within the same file before the route declaration.
_NEAREST_HANDLER_RE = re.compile(
    r"(?:function\s+|const\s+|let\s+|var\s+)(\w+)\s*[=(]"
)


def _extract_express(file_path: Path, content: str, repo_root: Path) -> list[dict]:
    if "app." not in content and "router." not in content:
        return []
    rel_path = str(file_path.relative_to(repo_root))
    apis: list[dict] = []
    for m in _EXPRESS_RE.finditer(content):
        verb = m.group(1).upper()
        path = m.group(2)
        line = content.count("\n", 0, m.start()) + 1
        # Best-effort handler: nearest preceding named function/const.
        prefix = content[:m.start()]
        handlers = _NEAREST_HANDLER_RE.findall(prefix)
        handler = handlers[-1] if handlers else file_path.stem
        apis.append({
            "method":   verb,
            "path":     path,
            "handler":  handler,
            "file":     rel_path,
            "line":     line,
            "framework": "express",
        })
    return apis


# ---------- Public entry point -----------------------------------------

# File extensions we'll inspect. Other languages need a dedicated extractor.
_INSPECT_EXTS = {".java", ".ts", ".tsx", ".js", ".jsx"}
_SKIP_PARTS = {
    "node_modules", "target", "build", "dist", ".git",
    "venv", "__pycache__", ".idea", ".vscode",
}


def extract_apis(repo_root: str, files: list[str] | None = None) -> list[dict]:
    """
    Walk `repo_root` (or just `files` if provided as relative paths) and
    return the list of detected APIs. Files are read fresh — we don't
    rely on the existing scan's truncated content because controllers
    can be long and we don't want to miss endpoints past the 8KB cap.

    Three extraction paths, in priority order:
      1. OpenAPI YAML/JSON spec at any depth (most accurate; modern Spring
         projects use openapi-codegen-cli to generate the interface layer
         from this spec, so my Java regex misses them otherwise).
      2. Spring annotations (regex on controller files).
      3. Express routes (regex on TS/JS files).

    When OpenAPI is present, it wins for the paths it covers. Java/Express
    regex extraction still runs to catch endpoints declared outside the
    OpenAPI spec.
    """
    root = Path(repo_root)
    all_apis: list[dict] = []

    # Path 1: OpenAPI spec, if any
    all_apis.extend(_extract_openapi(root))

    # Path 2 + 3: source-level regex
    if files is None:
        targets = list(_walk_targets(root))
    else:
        targets = [(root / f) for f in files if (root / f).exists()]

    for fp in targets:
        if fp.suffix not in _INSPECT_EXTS:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if fp.suffix == ".java":
            all_apis.extend(_extract_spring(fp, text, root))
        else:
            all_apis.extend(_extract_express(fp, text, root))

    # De-duplicate. Same (method, path) can match both OpenAPI and a regex
    # path — prefer the OpenAPI entry (more accurate metadata).
    seen: dict[tuple, dict] = {}
    for api in all_apis:
        key = (api["method"], api["path"])
        if key not in seen:
            seen[key] = api
        else:
            # Prefer openapi > spring > express (most reliable first)
            ranks = {"openapi": 0, "spring": 1, "express": 2}
            if ranks.get(api["framework"], 99) < ranks.get(seen[key]["framework"], 99):
                seen[key] = api

    finalized = list(seen.values())
    # Resolve which controller actually implements each endpoint.
    _annotate_impl(finalized, root)
    return sorted(finalized, key=lambda a: (a["file"], a.get("line", 0), a["method"]))


# ---------- endpoint → implementing-controller resolution --------------

def _annotate_impl(apis: list[dict], root: Path) -> None:
    """
    Annotate every API dict in place with:
      - source_file: repo-relative path of the controller that IMPLEMENTS
                     the endpoint ("" if no implementation is found)
      - registered:  True when an implementation was located, else False

    For spring/express endpoints the declaring file IS the implementation,
    so source_file = file. For openapi-spec endpoints (the codegen case)
    the spec only states the contract — the handler lives in a hand-written
    controller, located here with two signals so openapi-codegen apps
    (operationId methods) and plain Spring apps (@*Mapping) are both
    covered:
      1. the endpoint's operationId appears as an identifier in a
         @(Rest)Controller source file, or
      2. a @*Mapping in such a file matches the endpoint's verb + path.
    """
    # Index @(Rest)Controller source files once (skip generated/build dirs).
    controllers: list[tuple[str, str, set]] = []
    for fp in _walk_targets(root):
        if fp.suffix != ".java":
            continue
        if "target" in fp.parts or "build" in fp.parts:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not _SPRING_CONTROLLER_RE.search(text):
            continue
        rel = str(fp.relative_to(root))
        verb_paths = {
            (e["method"], (e["path"] or "/").rstrip("/") or "/")
            for e in _extract_spring(fp, text, root)
        }
        controllers.append((rel, text, verb_paths))

    for api in apis:
        if api.get("framework") in ("spring", "express"):
            api["source_file"] = api.get("file", "")
            api["registered"] = True
            continue
        # openapi — resolve against the controller index
        op = (api.get("handler") or "").strip()
        if op == "<openapi>":
            op = ""
        op_re = re.compile(r"\b" + re.escape(op) + r"\b") if op else None
        ep = (api.get("method", ""), (api.get("path") or "/").rstrip("/") or "/")
        match = ""
        for rel, text, verb_paths in controllers:
            if (op_re and op_re.search(text)) or ep in verb_paths:
                match = rel
                break
        api["source_file"] = match
        api["registered"] = bool(match)


def _walk_targets(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in _INSPECT_EXTS:
                yield p


# ---------- OpenAPI YAML / JSON spec -----------------------------------

_OPENAPI_NAMES = (
    "openapi.yml", "openapi.yaml", "openapi.json",
    "api.yml", "api.yaml", "api.json",
    "swagger.yml", "swagger.yaml", "swagger.json",
)


def _find_openapi_specs(root: Path) -> list[Path]:
    """Walk the repo (skipping noise dirs) and return any file whose name
    matches an OpenAPI convention. Capped at 5 hits to avoid a multi-spec
    surprise eating the scan."""
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS
                       and d not in {"node_modules", "target", "build", "dist"}]
        for fn in filenames:
            if fn.lower() in _OPENAPI_NAMES:
                hits.append(Path(dirpath) / fn)
                if len(hits) >= 5:
                    return hits
    return hits


def _load_spec(spec_path: Path):
    """Read + parse an OpenAPI spec file (JSON or YAML). None on any failure."""
    try:
        text = spec_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if spec_path.suffix == ".json":
        try:
            return json.loads(text)
        except Exception:
            return None
    if not _HAS_YAML:
        # PyYAML not installed — the Java regex path may still find endpoints.
        return None
    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def _schema_ref_name(node) -> str:
    """The schema name a node references — handles a direct $ref and an
    array-of-$ref. Empty string when the node references nothing."""
    if not isinstance(node, dict):
        return ""
    ref = node.get("$ref")
    if isinstance(ref, str) and "/" in ref:
        return ref.rsplit("/", 1)[-1]
    if node.get("type") == "array":
        return _schema_ref_name(node.get("items") or {})
    return ""


def _op_schema_refs(op: dict) -> tuple[str, str]:
    """(request_schema, response_schema) schema names referenced by an op."""
    req = ""
    for media in ((op.get("requestBody") or {}).get("content") or {}).values():
        req = _schema_ref_name((media or {}).get("schema") or {})
        if req:
            break
    resp = ""
    responses = op.get("responses") or {}
    for code in ("200", "201", "2XX", "default"):
        r = responses.get(code)
        if not isinstance(r, dict):
            continue
        for media in (r.get("content") or {}).values():
            resp = _schema_ref_name((media or {}).get("schema") or {})
            if resp:
                break
        if resp:
            break
    return req, resp


def _extract_openapi(root: Path) -> list[dict]:
    out: list[dict] = []
    for spec_path in _find_openapi_specs(root):
        data = _load_spec(spec_path)
        if not isinstance(data, dict):
            continue
        paths = data.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        rel_path = str(spec_path.relative_to(root))
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for verb, op in methods.items():
                if verb.lower() not in ("get", "post", "put", "delete", "patch"):
                    continue
                handler = ""
                req_schema = resp_schema = ""
                if isinstance(op, dict):
                    handler = op.get("operationId", "") or ""
                    req_schema, resp_schema = _op_schema_refs(op)
                out.append({
                    "method":    verb.upper(),
                    "path":      path,
                    "handler":   handler or "<openapi>",
                    "file":      rel_path,
                    "line":      0,
                    "framework": "openapi",
                    "request_schema":  req_schema,
                    "response_schema": resp_schema,
                })
    return out


def extract_api_schemas(repo_root: str) -> dict:
    """
    Merged OpenAPI `components/schemas` across every spec in the repo,
    keyed by schema name. The verify slice feeds these exact DTO field
    shapes into test generation so request payloads aren't guessed.
    Empty dict when there is no spec / PyYAML is unavailable.
    """
    root = Path(repo_root)
    schemas: dict = {}
    for spec_path in _find_openapi_specs(root):
        data = _load_spec(spec_path)
        if not isinstance(data, dict):
            continue
        comps = ((data.get("components") or {}).get("schemas")) or {}
        if isinstance(comps, dict):
            for name, defn in comps.items():
                schemas.setdefault(name, defn)
    return schemas


def extract_api_base_url(repo_root: str) -> str:
    """
    The base URL from the OpenAPI `servers` block (first spec, first
    server). May be absolute (http://host:port/path) or a bare path
    (/api) — the verify slice resolves a bare path against the detected
    port. Returns '' when there is no spec / no servers entry.

    This is what spares the user from hand-setting VERIFY_TARGET_URL:
    e.g. petclinic's spec declares `servers: [{url: .../petclinic/api}]`,
    the context path the regex-based port detection cannot know.
    """
    root = Path(repo_root)
    for spec_path in _find_openapi_specs(root):
        data = _load_spec(spec_path)
        if not isinstance(data, dict):
            continue
        servers = data.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            url = servers[0].get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return ""


# ---------- entity dependency topology ---------------------------------

def entity_name(source_file: str) -> str:
    """Short resource slug for a controller file — also the verify
    test-file basename. .../OwnerRestController.java -> 'owner'."""
    stem = Path(source_file).stem
    for suffix in ("RestController", "Controller"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "", stem.lower()) or "api"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _schema_to_entity(schema_name: str, entities: set) -> str:
    """Map a schema name to a known entity: 'PetType'->'pettype',
    'OwnerFields'->'owner'. '' when it matches no entity."""
    s = _norm(schema_name)
    for suffix in ("fields", "dto"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s if s in entities else ""


def _field_to_entity(field_name: str, entities: set) -> str:
    """Map a foreign-key-ish field to an entity: 'ownerId'->'owner'."""
    f = _norm(field_name)
    if f.endswith("id") and len(f) > 2:
        base = f[:-2]
        return base if base in entities else ""
    return ""


def _flatten_props(node, schemas, seen=None) -> dict:
    """All properties of a schema node, resolving $ref and allOf."""
    if seen is None:
        seen = set()
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {}
        seen.add(name)
        return _flatten_props(schemas.get(name, {}), schemas, seen)
    props: dict = {}
    for sub in node.get("allOf") or []:
        props.update(_flatten_props(sub, schemas, seen))
    if isinstance(node.get("properties"), dict):
        props.update(node["properties"])
    return props


def _entity_deps_of_schema(schema_name, schemas, entities, seen=None) -> set:
    """Entities a schema transitively references — via nested $ref DTOs
    and foreign-key *Id fields."""
    if seen is None:
        seen = set()
    if not schema_name or schema_name in seen:
        return set()
    seen.add(schema_name)
    node = schemas.get(schema_name)
    if not isinstance(node, dict):
        return set()
    deps: set = set()
    for fname, fprop in _flatten_props(node, schemas).items():
        if not isinstance(fprop, dict):
            continue
        # A foreign-key field (ownerId) is a real relationship even when
        # read-only — the dependent resource still must exist first.
        fe = _field_to_entity(fname, entities)
        if fe:
            deps.add(fe)
        # A nested-DTO reference is only a *creation* dependency when the
        # field is writable. A read-only $ref (e.g. Pet.visits) is response
        # data, not something you provide — it must not create an edge.
        if fprop.get("readOnly"):
            continue
        ref = _schema_ref_name(fprop)            # $ref or array-of-$ref
        if ref:
            e = _schema_to_entity(ref, entities)
            if e:
                deps.add(e)
            deps |= _entity_deps_of_schema(ref, schemas, entities, seen)
    return deps


def _topo_sort(nodes: set, deps: dict) -> list:
    """Kahn topological sort — dependencies before dependents. On a cycle
    the remaining nodes are appended (stable) rather than dropped."""
    remaining = set(nodes)
    out: list = []
    while remaining:
        ready = sorted(n for n in remaining
                       if not (deps.get(n, set()) & remaining))
        if not ready:                            # cycle — break deterministically
            ready = [sorted(remaining)[0]]
        out.extend(ready)
        remaining -= set(ready)
    return out


def build_entity_topology(apis: list[dict], schemas: dict) -> dict:
    """
    Derive an entity dependency DAG: which resource must exist before
    another can be created (a pet needs an owner + a pettype). Edges come
    from request-body schema references — nested $ref DTOs and *Id fields.

    Returns {"entities": {name: {"controller": file, "depends_on": [...]}},
             "order": [...]}  — `order` topologically sorted, deps first.
    The verify slice uses this to generate + run tests in dependency order
    and to tear down resources in reverse.
    """
    by_entity: dict[str, str] = {}
    for a in apis:
        sf = a.get("source_file") or ""
        if sf:
            by_entity.setdefault(entity_name(sf), sf)
    entities = set(by_entity)
    if not entities:
        return {"entities": {}, "order": []}

    deps: dict[str, set] = {e: set() for e in entities}
    for a in apis:
        sf = a.get("source_file") or ""
        if not sf:
            continue
        ent = entity_name(sf)
        req = a.get("request_schema") or ""
        if req:
            for d in _entity_deps_of_schema(req, schemas, entities):
                if d != ent:
                    deps[ent].add(d)

    return {
        "entities": {
            e: {"controller": by_entity[e], "depends_on": sorted(deps[e])}
            for e in entities
        },
        "order": _topo_sort(entities, deps),
    }


# ---------- Summary helpers --------------------------------------------

def apis_summary(apis: list[dict]) -> str:
    """One-line summary for scan output."""
    if not apis:
        return "no APIs detected"
    by_method: dict[str, int] = {}
    for a in apis:
        by_method[a["method"]] = by_method.get(a["method"], 0) + 1
    bits = [f"{n} {m}" for m, n in sorted(by_method.items())]
    return f"{len(apis)} endpoint(s) ({', '.join(bits)})"

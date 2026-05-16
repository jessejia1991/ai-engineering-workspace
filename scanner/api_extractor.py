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


def _extract_openapi(root: Path) -> list[dict]:
    specs = _find_openapi_specs(root)
    if not specs:
        return []
    out: list[dict] = []
    for spec_path in specs:
        try:
            text = spec_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if spec_path.suffix == ".json":
            try:
                data = json.loads(text)
            except Exception:
                continue
        else:
            if not _HAS_YAML:
                # PyYAML not installed; skip silently — Java regex path may
                # still pick up endpoints, and the runtime block still
                # records that the spec exists.
                continue
            try:
                data = yaml.safe_load(text)
            except Exception:
                continue
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
                if isinstance(op, dict):
                    handler = op.get("operationId", "") or ""
                out.append({
                    "method":    verb.upper(),
                    "path":      path,
                    "handler":   handler or "<openapi>",
                    "file":      rel_path,
                    "line":      0,
                    "framework": "openapi",
                })
    return out


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

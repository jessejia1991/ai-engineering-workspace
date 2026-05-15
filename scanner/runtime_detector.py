"""
Static detection of the target repo's runtime characteristics:
  - build commands, run commands, test commands
  - languages, frameworks, test frameworks
  - server port (for the verify pipeline)
  - containerization (Dockerfile / docker-compose presence)
  - CI config file presence

Pattern matching is opinionated for the ecosystems this take-home demos
against (Maven + Spring + Java; npm + React/Express + TS/JS;
pyproject/requirements + pytest + Python). Other ecosystems (Go modules,
Cargo, Gradle Kotlin DSL) are extension points documented in the design
doc — they would add a similar detector module and append to the
returned dict.
"""

from __future__ import annotations

import os
import re
import json
from pathlib import Path
from typing import Optional


def detect_runtime(repo_root: str) -> dict:
    """
    Return a dict shaped like:
      {
        "languages":      ["java", "typescript"],
        "frameworks":     ["spring-boot", "react"],
        "test_frameworks":["junit-5", "jest"],
        "build_commands": ["mvn package"],
        "run_commands":   ["mvn spring-boot:run"],
        "test_commands":  ["mvn test", "npm test"],
        "port":           8080,
        "containerization": ["Dockerfile", "docker-compose.yml"],
        "ci_config":      [".github/workflows/build.yml"],
      }
    Empty list when the signal isn't found. Always returns the keys so
    downstream code can rely on them.
    """
    root = Path(repo_root)
    out: dict = {
        "languages":        set(),
        "frameworks":       set(),
        "test_frameworks":  set(),
        "build_commands":   [],
        "run_commands":     [],
        "test_commands":    [],
        "port":             None,
        "containerization": [],
        "ci_config":        [],
    }

    _detect_maven(root, out)
    _detect_npm(root, out)
    _detect_python(root, out)
    _detect_docker(root, out)
    _detect_ci(root, out)

    if out["port"] is None:
        out["port"] = 8080  # safe default for Spring Boot / Express demos

    # Normalize sets → sorted lists for JSON serialization
    out["languages"]       = sorted(out["languages"])
    out["frameworks"]      = sorted(out["frameworks"])
    out["test_frameworks"] = sorted(out["test_frameworks"])

    return out


# ---------- Maven / Spring -------------------------------------------------

_SPRING_BOOT_RE = re.compile(r"spring-boot", re.IGNORECASE)
_JUNIT5_RE      = re.compile(r"junit-jupiter|junit\.jupiter", re.IGNORECASE)
_JUNIT4_RE      = re.compile(r"<artifactId>junit</artifactId>", re.IGNORECASE)


def _detect_maven(root: Path, out: dict) -> None:
    pom = root / "pom.xml"
    if not pom.is_file():
        return
    try:
        text = pom.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    out["languages"].add("java")
    out["build_commands"].append("mvn package")
    out["test_commands"].append("mvn test")

    if _SPRING_BOOT_RE.search(text):
        out["frameworks"].add("spring-boot")
        out["run_commands"].append("mvn spring-boot:run")

    if _JUNIT5_RE.search(text):
        out["test_frameworks"].add("junit-5")
    elif _JUNIT4_RE.search(text):
        out["test_frameworks"].add("junit-4")

    # Spring Boot server.port resolution: application.properties / .yml under
    # src/main/resources. Honor the first port hit; fallback handled later.
    port = _read_spring_port(root)
    if port is not None:
        out["port"] = port


_PROP_PORT_RE = re.compile(r"^\s*server\.port\s*=\s*(\d+)", re.MULTILINE)
_YML_PORT_RE  = re.compile(r"\bport\s*:\s*(\d+)")


def _read_spring_port(root: Path) -> Optional[int]:
    resources = root / "src" / "main" / "resources"
    if not resources.is_dir():
        return None
    candidates = [
        resources / "application.properties",
        resources / "application.yml",
        resources / "application.yaml",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if p.suffix == ".properties":
            m = _PROP_PORT_RE.search(text)
        else:
            m = _YML_PORT_RE.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


# ---------- npm / Node -----------------------------------------------------

def _detect_npm(root: Path, out: dict) -> None:
    pkg_path = root / "package.json"
    # The petclinic frontend lives under client/, so look there too.
    if not pkg_path.is_file():
        pkg_path = root / "client" / "package.json"
        if not pkg_path.is_file():
            return
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return

    out["languages"].add("typescript" if (root / "tsconfig.json").exists()
                         or (root / "client" / "tsconfig.json").exists()
                         else "javascript")

    deps = {}
    for key in ("dependencies", "devDependencies"):
        deps.update(data.get(key) or {})
    if "react" in deps:
        out["frameworks"].add("react")
    if "express" in deps:
        out["frameworks"].add("express")
    if "vite" in deps:
        out["frameworks"].add("vite")
    if "jest" in deps:
        out["test_frameworks"].add("jest")
    if "vitest" in deps:
        out["test_frameworks"].add("vitest")
    if "@playwright/test" in deps or "playwright" in deps:
        out["test_frameworks"].add("playwright")

    scripts = data.get("scripts") or {}
    # Prefer canonical npm script names — `npm run X` is the universal call shape.
    cwd_prefix = "" if pkg_path.parent == root else f"(cd {pkg_path.parent.name} && )"
    def _add(target: str, key: str) -> None:
        if key in scripts:
            cmd = f"npm run {key}" if pkg_path.parent == root else \
                  f"cd {pkg_path.parent.relative_to(root)} && npm run {key}"
            out[target].append(cmd)
    _add("build_commands", "build")
    _add("run_commands",   "start")
    _add("run_commands",   "dev")
    _add("test_commands",  "test")


# ---------- Python ---------------------------------------------------------

def _detect_python(root: Path, out: dict) -> None:
    has_req = (root / "requirements.txt").is_file()
    has_pyp = (root / "pyproject.toml").is_file()
    has_setup = (root / "setup.py").is_file()
    if not (has_req or has_pyp or has_setup):
        return
    out["languages"].add("python")

    if has_pyp:
        try:
            text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "pytest" in text:
                out["test_frameworks"].add("pytest")
            if "[tool.poetry]" in text:
                out["build_commands"].append("poetry install")
                out["test_commands"].append("poetry run pytest")
                return
        except Exception:
            pass

    if has_req:
        out["build_commands"].append("pip install -r requirements.txt")
    out["test_commands"].append("pytest")
    out["test_frameworks"].add("pytest")


# ---------- Docker ---------------------------------------------------------

_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.MULTILINE | re.IGNORECASE)


def _detect_docker(root: Path, out: dict) -> None:
    df = root / "Dockerfile"
    dc1 = root / "docker-compose.yml"
    dc2 = root / "docker-compose.yaml"

    if df.is_file():
        out["containerization"].append("Dockerfile")
        out["build_commands"].append("docker build -t app .")
        out["run_commands"].append("docker run -p 8080:8080 app")
        try:
            text = df.read_text(encoding="utf-8", errors="ignore")
            m = _EXPOSE_RE.search(text)
            if m and out["port"] is None:
                out["port"] = int(m.group(1))
        except Exception:
            pass

    if dc1.is_file() or dc2.is_file():
        out["containerization"].append("docker-compose.yml")
        out["run_commands"].append("docker compose up")


# ---------- CI -------------------------------------------------------------

def _detect_ci(root: Path, out: dict) -> None:
    wf_dir = root / ".github" / "workflows"
    if wf_dir.is_dir():
        for f in wf_dir.iterdir():
            if f.suffix in (".yml", ".yaml"):
                out["ci_config"].append(str(f.relative_to(root)))
    # Other CIs: travis, circle, drone — extension points.
    for name in (".travis.yml", ".circleci/config.yml", ".gitlab-ci.yml"):
        if (root / name).is_file():
            out["ci_config"].append(name)


# ---------- One-line summary for `scan` printout -------------------------

def runtime_summary(runtime: dict) -> str:
    """Compact one-line summary used by `scan` and `repo list`."""
    bits = []
    if runtime["frameworks"]:
        bits.append(" + ".join(runtime["frameworks"]))
    elif runtime["languages"]:
        bits.append(" + ".join(runtime["languages"]))
    if runtime["containerization"]:
        bits.append(", ".join(runtime["containerization"]))
    port = runtime.get("port")
    if port:
        bits.append(f"port {port}")
    return " · ".join(bits) if bits else "no runtime detected"

import os
import json
import subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DEFAULT_REPO_PATH = os.environ.get("PETCLINIC_REPO_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "spring-petclinic-reactjs"
)

CONTEXT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", ".ai-workspace", "repo-context.json"
)

CORRECTIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", ".ai-workspace", "corrections.json"
)

SKIP_DIRS = {
    ".git", "node_modules", "target", "build", "dist",
    ".idea", ".vscode", "__pycache__", "venv", ".mvn", "ai_workspace"
}

# 我们认识的源码文件类型，其他的不读内容
SOURCE_EXTENSIONS = {
    ".java", ".kt",                          # JVM
    ".py",                                   # Python
    ".ts", ".tsx", ".js", ".jsx",            # Frontend
    ".go", ".rs",                            # Go / Rust
    ".properties", ".yml", ".yaml", ".xml",  # Config
    ".sql",                                  # DB
    ".md",                                   # Docs
}

# 单个文件最大读取大小，防止context爆炸
MAX_FILE_SIZE = 8000  # chars


def walk_repo(repo_root: str) -> list[str]:
    """递归找出所有文件，返回相对路径列表"""
    all_files = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, repo_root)
            all_files.append(rel)
    return sorted(all_files)


def read_file(repo_root: str, rel_path: str) -> str:
    """读取文件内容，超出大小截断"""
    fpath = os.path.join(repo_root, rel_path)
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(MAX_FILE_SIZE)
        if len(content) == MAX_FILE_SIZE:
            content += "\n... [truncated]"
        return content
    except Exception as e:
        return f"[unreadable: {e}]"


def classify_files(all_files: list[str]) -> dict:
    """
    把文件按路径特征分类。
    这里只做路径层面的分类，不做任何内容理解。
    """
    classification = {
        "backend": [],
        "frontend": [],
        "test": [],
        "config": [],
        "build": [],
        "other": [],
    }

    for f in all_files:
        parts = f.replace("\\", "/").split("/")
        ext = os.path.splitext(f)[1].lower()
        basename = os.path.basename(f)

        # test文件：路径里有test/tests/__tests__/spec，或文件名含Test/Spec
        is_test = (
            any(p in {"test", "tests", "__tests__", "spec"} for p in parts)
            or basename.endswith(("Test.java", "Tests.java", "Spec.java",
                                  ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
                                  "_test.py", "_spec.py"))
        )

        # build文件
        is_build = basename in {
            "pom.xml", "build.gradle", "build.gradle.kts",
            "package.json", "package-lock.json", "yarn.lock",
            "requirements.txt", "pyproject.toml", "setup.py",
            "Dockerfile", "docker-compose.yml", "Makefile",
            "mvnw", "gradlew",
        }

        # config文件
        is_config = (
            ext in {".properties", ".yml", ".yaml"}
            or basename in {"tsconfig.json", "vite.config.ts",
                            "vite.config.js", "webpack.config.js",
                            ".env", ".env.example"}
        )

        if is_test:
            classification["test"].append(f)
        elif is_build:
            classification["build"].append(f)
        elif is_config:
            classification["config"].append(f)
        elif ext in {".java", ".kt", ".py", ".go", ".rs"}:
            # source文件再按路径判断frontend/backend
            if any(p in {"frontend", "client", "web", "ui", "src/client"} for p in parts):
                classification["frontend"].append(f)
            else:
                classification["backend"].append(f)
        elif ext in {".ts", ".tsx", ".js", ".jsx"}:
            classification["frontend"].append(f)
        elif ext in SOURCE_EXTENSIONS:
            classification["other"].append(f)
        else:
            classification["other"].append(f)

    return classification


def _default_branch(repo_root: str) -> str:
    """
    Detect the default branch (modern repos use `main`, legacy use `master`).
    Falls back to `master` if detection fails so existing petclinic flow keeps
    working. Override with ANTHROPIC_DEFAULT_BRANCH env var if needed.
    """
    forced = os.environ.get("REVIEW_BASE_BRANCH")
    if forced:
        return forced
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        ref = result.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/"):]
    except Exception:
        pass
    # Last resort: try main then master
    for candidate in ("main", "master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_root, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return candidate
    return "master"


def get_diff(repo_root: str, branch: str = None) -> str:
    """获取diff内容，原样返回给LLM"""
    try:
        if branch:
            base = _default_branch(repo_root)
            cmd = ["git", "diff", f"{base}...{branch}"]
        else:
            cmd = ["git", "diff", "HEAD~1"]
        result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        diff = result.stdout
        if len(diff) > 12000:
            diff = diff[:12000] + "\n... [diff truncated]"
        return diff
    except Exception as e:
        return f"[diff unavailable: {e}]"


def get_changed_files(repo_root: str, branch: str = None) -> list[str]:
    """获取changed files列表"""
    try:
        if branch:
            base = _default_branch(repo_root)
            cmd = ["git", "diff", f"{base}...{branch}", "--name-only"]
        else:
            cmd = ["git", "diff", "HEAD~1", "--name-only"]
        result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception:
        return []


def load_corrections() -> list:
    """加载历史上记录的LLM理解偏差"""
    if not os.path.exists(CORRECTIONS_FILE):
        return []
    try:
        with open(CORRECTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_correction(correction_type: str, note: str, example: str = ""):
    """记录一个新的理解偏差"""
    corrections = load_corrections()
    corrections.append({
        "type": correction_type,
        "note": note,
        "example": example,
        "recorded_at": datetime.now().isoformat()
    })
    os.makedirs(os.path.dirname(CORRECTIONS_FILE), exist_ok=True)
    with open(CORRECTIONS_FILE, "w") as f:
        json.dump(corrections, f, indent=2)


def read_key_files(repo_root: str, classification: dict) -> dict:
    """
    读取关键文件的内容。
    config文件全读，backend/frontend只读前N个（按文件名排序）。
    内容原样存储，不做解析。
    """
    key_files = {}

    # config文件全部读
    for f in classification["config"]:
        key_files[f] = read_file(repo_root, f)

    # build文件全部读
    for f in classification["build"]:
        key_files[f] = read_file(repo_root, f)

    return key_files


def scan(repo_root: str = None) -> dict:
    if repo_root is None:
        repo_root = DEFAULT_REPO_PATH

    if not os.path.exists(repo_root):
        raise FileNotFoundError(f"Repo not found: {repo_root}")

    # 1. 找出所有文件
    all_files = walk_repo(repo_root)

    # 2. 按路径特征分类（不理解内容）
    classification = classify_files(all_files)

    # 3. 读取关键文件内容（config + build）
    key_file_contents = read_key_files(repo_root, classification)

    # 4. 加载历史corrections
    corrections = load_corrections()

    # 5. Static runtime + API extraction (verify slice). Both are best-effort —
    #    a failure inside either should not break scan.
    try:
        from scanner.runtime_detector import detect_runtime
        runtime = detect_runtime(repo_root)
    except Exception:
        runtime = {}
    try:
        from scanner.api_extractor import extract_apis
        # Limit extraction to backend files — controllers don't live in tests
        # or build artifacts. Frontend files (Express) are inspected too if
        # they match the pattern.
        candidate_files = classification["backend"] + classification["frontend"]
        apis = extract_apis(repo_root, files=candidate_files)
    except Exception:
        apis = []

    profile = {
        "repo_id": os.path.basename(repo_root),
        "repo_path": repo_root,

        # 文件分类结果（路径列表）
        "files": {
            "backend":  classification["backend"],
            "frontend": classification["frontend"],
            "test":     classification["test"],
            "config":   classification["config"],
            "build":    classification["build"],
            "total":    len(all_files),
        },

        # 关键文件原始内容（LLM自己去理解）
        "key_file_contents": key_file_contents,

        # 历史上记录的理解偏差
        "corrections": corrections,

        # Verify slice: how to build/run/test + what APIs exist
        "runtime": runtime,
        "apis":    apis,

        "scanned_at": datetime.now().isoformat()
    }

    os.makedirs(os.path.dirname(CONTEXT_FILE), exist_ok=True)
    with open(CONTEXT_FILE, "w") as f:
        json.dump(profile, f, indent=2)

    return profile


def load_profile() -> dict:
    if not os.path.exists(CONTEXT_FILE):
        raise FileNotFoundError("Repo not scanned yet. Run: ai-eng scan")
    with open(CONTEXT_FILE) as f:
        return json.load(f)


def get_files_content(repo_root: str, file_list: list[str]) -> dict[str, str]:
    """
    给定文件路径列表，返回每个文件的原始内容。
    Review时agent用这个来读取changed files的内容。
    """
    return {f: read_file(repo_root, f) for f in file_list}

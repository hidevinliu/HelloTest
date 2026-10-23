#!/usr/bin/env python3
"""project-governance 体检与"只许变好"检查脚本。零依赖，只用 Python 标准库。

两个子命令：

  audit    不改项目代码。自动识别技术栈，跑能用的检查工具，在输出目录
           （默认 <项目>/.governance/）写一份 Markdown 体检报告和一份指标 JSON。
           加 --save-baseline 时，同时把这次的指标存成基线 baseline.json。

  ratchet  重跑基线里有的指标，和基线比较：
             退出码 0 = 没有任何指标变多（可以合并）
             退出码 1 = 有指标变多（新代码引入了新问题，拒绝合并）
             退出码 2 = 有指标这次没测出来，无法判断（不能当作通过）
           加 --update 时，把变少的指标写回基线（基线只降不升）。

指标一律"越少越好"：类型错误数、lint 问题数、死代码候选数、重复代码行数、
高危依赖漏洞数、超长文件数。

已核对过输出格式的工具版本（2026-10-09 实测）：
  typescript tsc、knip（--reporter json）、jscpd（--reporters json）、npm audit --json、
  ruff（--output-format json）、pyright（--outputjson）、vulture、pip-audit（-f json）、
  swiftlint 0.65.1（--reporter json，退出码 2 = 有违规，2026-10-23 实测）。
未实测：eslint -f json（实测用的项目没有 eslint 配置）、pnpm audit --json、
poetry export（poetry.lock 转换路径；uv export 已于 2026-10-23 在真实 uv 项目实测）。
"""
from __future__ import annotations

import argparse
import fnmatch
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift",
}
EXCLUDED_DIRS = {
    "node_modules", "dist", "build", ".next", ".nuxt", ".output", "vendor", ".venv", "venv",
    "__pycache__", "coverage", ".git", ".governance", ".turbo", ".cache",
    # Xcode / SwiftPM 构建产物：自动生成，扫进去会污染重复代码和超长文件指标
    ".build", "DerivedData", ".swiftpm", "Index.noindex",
}

# 代码扩展名 → 语言名（用于报告里的技术栈展示，按行数排序）
EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript (JSX)", ".ts": "TypeScript", ".tsx": "TypeScript (TSX)",
    ".vue": "Vue", ".svelte": "Svelte", ".astro": "Astro",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift",
}
JSCPD_FORMATS = (
    "python,javascript,typescript,tsx,jsx,vue,svelte,astro,go,rust,java,kotlin,ruby,php,csharp,swift"
)
LARGE_FILE_LINES = 500
DEFAULT_TIMEOUT = 900

# 指标键 → 中文说明。ratchet 只比较这里列出的键。
METRICS = {
    "ts_errors": "TypeScript 类型错误",
    "eslint_problems": "ESLint 问题（错误 + 警告）",
    "swiftlint_issues": "SwiftLint 问题（错误 + 警告）",
    "knip_issues": "JS/TS 死代码候选（未使用的文件/导出/依赖）",
    "npm_vuln_high": "npm 高危 + 严重漏洞",
    "pyright_errors": "pyright 类型错误",
    "ruff_issues": "Ruff 问题",
    "vulture_items": "Python 死代码候选",
    "pip_vuln": "Python 依赖已知漏洞",
    "dup_lines": "重复代码行数",
    "large_files": f"超过 {LARGE_FILE_LINES} 行的代码文件数",
}


@dataclass
class Check:
    key: str
    value: int | None = None
    status: str = "skipped"  # ok / skipped / failed
    note: str = ""
    command: str = ""
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 运行外部命令

def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    """运行命令并返回 (退出码, stdout, stderr)。命令不存在时返回 127，超时返回 124。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", f"找不到命令：{e.filename}"
    except subprocess.TimeoutExpired:
        return 124, "", f"超过 {timeout} 秒未完成，已中止"


def py_tool(name: str, root: Path | None = None) -> list[str] | None:
    """Python 检查工具的查找顺序：项目虚拟环境里锁定版本的 → uvx 临时运行 → 系统里已装的。
    项目自己装了就用项目的，避免工具升级让指标变化、ratchet 误报。"""
    if root is not None:
        for venv in (".venv", "venv"):
            local = root / venv / "bin" / name
            if local.exists():
                return [str(local)]
    if shutil.which("uvx"):
        return ["uvx", name]
    if shutil.which(name):
        return [name]
    return None


def js_tool(root: Path, binary: str, package: str) -> list[str] | None:
    """JS 检查工具优先用项目自己装的版本，其次用 npx 临时运行。"""
    local = root / "node_modules" / ".bin" / binary
    if local.exists():
        return [str(local)]
    if shutil.which("npx"):
        return ["npx", "-y", "-p", package, binary]
    return None


def tail(text: str, n: int = 3) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " / ".join(lines[-n:])[:400]


# ---------------------------------------------------------------- 识别项目

@dataclass
class Project:
    root: Path
    files: list[Path]
    is_js: bool
    is_ts: bool
    has_eslint: bool
    is_py: bool
    is_swift: bool
    has_node_modules: bool
    is_git: bool
    exclude: list[str] = field(default_factory=list)


def is_excluded(rel: Path, patterns: list[str]) -> bool:
    s = rel.as_posix()
    return any(fnmatch.fnmatch(s, pat) or fnmatch.fnmatch(s, pat.rstrip("/") + "/*") for pat in patterns)


def list_files(root: Path, exclude: list[str]) -> tuple[list[Path], bool]:
    """优先用 git ls-files（自动遵守 .gitignore），不是 git 仓库时遍历目录。"""
    code, out, _ = run(["git", "ls-files"], root, 120)
    if code == 0:
        files, is_git = [Path(p) for p in out.splitlines() if p], True
    else:
        files, is_git = [], False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            for fn in filenames:
                files.append(Path(dirpath, fn).relative_to(root))
    files = [p for p in files if not (set(p.parts) & EXCLUDED_DIRS) and not is_excluded(p, exclude)]
    return files, is_git


def detect(root: Path, exclude: list[str] | None = None) -> Project:
    exclude = list(exclude or [])
    files, is_git = list_files(root, exclude)
    names = {p.name for p in files if len(p.parts) == 1}
    pkg = root / "package.json"
    has_eslint = any(n.startswith(("eslint.config.", ".eslintrc")) for n in names)
    if not has_eslint and pkg.exists():
        try:
            has_eslint = "eslintConfig" in json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[提示] package.json 读取失败，按没有 ESLint 配置处理：{e}", file=sys.stderr)
    py_count = sum(1 for p in files if p.suffix == ".py")
    return Project(
        root=root,
        files=files,
        is_js=pkg.exists(),
        is_ts=(root / "tsconfig.json").exists(),
        has_eslint=has_eslint,
        is_py=bool(names & {"pyproject.toml", "setup.py", "setup.cfg"}) or py_count > 0,
        is_swift=any(f.suffix == ".swift" for f in files),
        has_node_modules=(root / "node_modules").is_dir(),
        is_git=is_git,
        exclude=exclude,
    )


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------- 解析器（纯函数，便于测试）

def parse_tsc(text: str) -> int:
    return len(re.findall(r"error TS\d+", text))


def parse_eslint(text: str) -> tuple[int, list[str]]:
    data = json.loads(text)
    total = sum(f.get("errorCount", 0) + f.get("warningCount", 0) for f in data)
    worst = sorted(data, key=lambda f: f.get("errorCount", 0) + f.get("warningCount", 0), reverse=True)
    details = [f"{f['filePath']}：{f.get('errorCount', 0)} 错误 / {f.get('warningCount', 0)} 警告"
               for f in worst[:10] if f.get("errorCount", 0) + f.get("warningCount", 0)]
    return total, details


def parse_swiftlint(text: str) -> tuple[int, list[str]]:
    """swiftlint --reporter json：一个违规一个对象，severity 为 Error / Warning。"""
    data = json.loads(text)
    by_rule: dict[str, int] = {}
    for d in data:
        by_rule[d.get("rule_id") or "?"] = by_rule.get(d.get("rule_id") or "?", 0) + 1
    top = sorted(by_rule.items(), key=lambda kv: -kv[1])[:10]
    return len(data), ["最多的规则：" + "，".join(f"{r} {n}" for r, n in top)] if top else []


def parse_knip(text: str) -> tuple[int, list[str]]:
    """knip JSON：{"issues":[{"file":..., "<类别>":[...], ...}], 可能还有顶层 "files":[...]}。
    每个类别列表里的每一项算一个候选。"""
    data = json.loads(text)
    by_cat: dict[str, int] = {}
    samples: list[str] = []
    top_files = data.get("files")
    if isinstance(top_files, list):
        by_cat["files"] = len(top_files)
        samples += [f"未使用文件：{f}" for f in top_files[:10]]
    for issue in data.get("issues", []):
        for cat, items in issue.items():
            if not isinstance(items, list) or not items:
                continue
            by_cat[cat] = by_cat.get(cat, 0) + len(items)
            for it in items[:3]:
                name = it.get("name") if isinstance(it, dict) else it
                if len(samples) < 30:
                    samples.append(f"{cat}：{issue.get('file', '?')} → {name}")
    total = sum(by_cat.values())
    summary = "，".join(f"{k} {v}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]))
    return total, ([f"按类别：{summary}"] if summary else []) + samples


def parse_npm_audit(text: str) -> tuple[int, str]:
    vulns = json.loads(text).get("metadata", {}).get("vulnerabilities")
    if not isinstance(vulns, dict):
        raise ValueError("输出里没有 metadata.vulnerabilities 字段")
    high = int(vulns.get("high", 0)) + int(vulns.get("critical", 0))
    return high, "全部等级：" + "，".join(f"{k} {v}" for k, v in vulns.items())


def parse_ruff(text: str) -> tuple[int, list[str]]:
    data = json.loads(text)
    by_code: dict[str, int] = {}
    for d in data:
        by_code[d.get("code") or "?"] = by_code.get(d.get("code") or "?", 0) + 1
    top = sorted(by_code.items(), key=lambda kv: -kv[1])[:10]
    return len(data), ["最多的规则：" + "，".join(f"{c} {n}" for c, n in top)] if top else []


def parse_pyright(text: str) -> int:
    return int(json.loads(text)["summary"]["errorCount"])


def parse_vulture(text: str) -> tuple[int, list[str]]:
    items = [ln for ln in text.splitlines() if re.search(r":\d+: (unused|unreachable|redundant)", ln)]
    return len(items), items[:30]


def parse_pip_audit(text: str) -> tuple[int, list[str]]:
    deps = json.loads(text).get("dependencies", [])
    hit = [d for d in deps if d.get("vulns")]
    return sum(len(d["vulns"]) for d in hit), [f"{d['name']} {d.get('version', '')}：{len(d['vulns'])} 个" for d in hit[:15]]


def parse_jscpd(report: dict) -> tuple[int, list[str]]:
    total = report["statistics"]["total"]
    dups = sorted(report.get("duplicates", []), key=lambda d: -d.get("lines", 0))
    details = [f"重复率 {total.get('percentage', 0):.1f}%，{total.get('clones', 0)} 处重复"]
    for d in dups[:10]:
        a, b = d["firstFile"], d["secondFile"]
        details.append(f"{d.get('lines', 0)} 行：{a['name']}:{a['start']}-{a['end']} ↔ {b['name']}:{b['start']}-{b['end']}")
    return int(total.get("duplicatedLines", 0)), details


# ---------------------------------------------------------------- 各项检查

def _json_check(key: str, cmd: list[str] | None, proj: Project, timeout: int, parser, ok_codes=(0,),
                missing_note: str = "没有可用的运行方式") -> Check:
    if cmd is None:
        return Check(key, status="skipped", note=missing_note)
    c = Check(key, command=" ".join(cmd))
    code, out, err = run(cmd, proj.root, timeout)
    if code not in ok_codes:
        c.status, c.note = "failed", f"退出码 {code}：{tail(err or out)}"
        return c
    try:
        res = parser(out)
    except (ValueError, KeyError, TypeError) as e:
        c.status, c.note = "failed", f"输出无法解析（{e}）：{tail(err or out)}"
        return c
    c.status = "ok"
    if isinstance(res, tuple):
        c.value, extra = res
        if isinstance(extra, list):
            c.details = extra
        else:
            c.note = extra
    else:
        c.value = res
    return c


KNIP_CONFIG_NAMES = (
    "knip.json", ".knip.json", "knip.jsonc", ".knip.jsonc",
    "knip.js", "knip.ts", "knip.config.js", "knip.config.ts",
)


def exclude_globs(p: Project) -> list[str]:
    """内置排除目录 + 用户的 --exclude，统一成 glob。自己扫目录的工具（ruff / knip / jscpd）都用这一份。"""
    globs = [f"**/{d}/**" for d in sorted(EXCLUDED_DIRS)]
    globs += [pat if "*" in pat else pat.rstrip("/") + "/**" for pat in p.exclude]
    return list(dict.fromkeys(globs))


def ruff_args(p: Project) -> list[str]:
    return ["check", ".", "--output-format", "json", "--exit-zero", "--no-cache",
            "--extend-exclude", ",".join(exclude_globs(p))]


def pyright_targets(p: Project) -> list[str]:
    """pyright 命令行没有排除参数，只能把排除后剩下的 Python 文件逐个传进去。"""
    return [f.as_posix() for f in p.files if f.suffix in (".py", ".pyi")]


def knip_config(p: Project, tmp: Path) -> str | None:
    """项目没有 knip 配置时，写一份只含 ignore 的临时配置（其余沿用 knip 默认）；有配置就尊重项目的。"""
    if any((p.root / n).exists() for n in KNIP_CONFIG_NAMES):
        return None
    pkg = p.root / "package.json"
    try:
        if pkg.exists() and "knip" in json.loads(pkg.read_text(encoding="utf-8")):
            return None
    except (OSError, json.JSONDecodeError) as e:
        print(f"[提示] package.json 读取失败，按没有 knip 配置处理：{e}", file=sys.stderr)
    cfg = tmp / "knip.json"
    cfg.write_text(json.dumps({"ignore": exclude_globs(p)}, ensure_ascii=False), encoding="utf-8")
    return str(cfg)


def check_tsc(p: Project, t: int) -> Check:
    if not p.is_ts:
        return Check("ts_errors", note="没有 tsconfig.json")
    cmd = js_tool(p.root, "tsc", "typescript")
    if cmd is None:
        return Check("ts_errors", note="没有 npx")
    cmd = cmd + ["--noEmit", "-p", "."]
    code, out, err = run(cmd, p.root, t)
    c = Check("ts_errors", command=" ".join(cmd))
    if code not in (0, 1, 2):
        c.status, c.note = "failed", f"退出码 {code}：{tail(err or out)}"
        return c
    c.value, c.status = parse_tsc(out + err), "ok"
    if code != 0 and c.value == 0:
        c.status, c.value, c.note = "failed", None, f"tsc 失败但没有输出类型错误：{tail(out + err)}"
    files: dict[str, int] = {}
    for m in re.finditer(r"^(.+?)\(\d+,\d+\): error TS", out, re.M):
        files[m.group(1)] = files.get(m.group(1), 0) + 1
    c.details = [f"{n} 个：{f}" for f, n in sorted(files.items(), key=lambda kv: -kv[1])[:10]]
    return c


def check_eslint(p: Project, t: int) -> Check:
    if not p.has_eslint:
        return Check("eslint_problems", note="项目没有 ESLint 配置（阶段 2 再建）")
    cmd = js_tool(p.root, "eslint", "eslint")
    return _json_check("eslint_problems", cmd and cmd + [".", "-f", "json"], p, t, parse_eslint, ok_codes=(0, 1))


def check_swiftlint(p: Project, t: int) -> Check:
    if not p.is_swift:
        return Check("swiftlint_issues", note="没有 Swift 文件")
    if not shutil.which("swiftlint"):
        return Check("swiftlint_issues", status="skipped", note="没有装 SwiftLint（brew install swiftlint）")
    # 退出码 2 = 有违规，属于正常结果。
    # SwiftLint 没有命令行排除参数，默认也不排除 .build（2026-10-23 实测会把
    # 构建产物算进去），所以和 pyright / vulture 一样只传排除后剩下的 Swift 文件；
    # 项目自己的 .swiftlint.yml 规则配置仍然生效。
    targets = [f.as_posix() for f in p.files if f.suffix == ".swift"]
    if not targets:
        return Check("swiftlint_issues", note="排除之后没有 Swift 文件")
    cmd = ["swiftlint", "lint", "--reporter", "json", "--quiet", *targets]
    c = _json_check("swiftlint_issues", cmd, p, t, parse_swiftlint, ok_codes=(0, 2))
    if c.command:
        c.command = " ".join(["swiftlint", "lint", "--reporter", "json", "--quiet",
                              f"<排除后的 {len(targets)} 个 Swift 文件>"])
    return c


def check_knip(p: Project, t: int) -> Check:
    if not p.is_js:
        return Check("knip_issues", note="不是 JS/TS 项目")
    cmd = js_tool(p.root, "knip", "knip")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = knip_config(p, Path(tmp))
        args = ["--reporter", "json", "--no-exit-code"] + (["-c", cfg] if cfg else [])
        c = _json_check("knip_issues", cmd and cmd + args, p, t, parse_knip)
    if c.status == "ok":
        c.note = ("用临时配置排除了内置目录和 --exclude" if cfg
                  else "用项目自己的 knip 配置；--exclude 不作用于 knip，要排除请写进项目配置的 ignore")
    return c


def check_npm_audit(p: Project, t: int) -> Check:
    if not p.is_js:
        return Check("npm_vuln_high", note="不是 JS/TS 项目")
    if (p.root / "package-lock.json").exists() and shutil.which("npm"):
        cmd = ["npm", "audit", "--json"]
    elif (p.root / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        cmd = ["pnpm", "audit", "--json"]  # 输出格式未实测，解析失败会如实报 failed
    else:
        return Check("npm_vuln_high", note="没有 package-lock.json / pnpm-lock.yaml，或没装对应命令")
    # npm audit 发现漏洞时退出码是 1，属于正常结果
    return _json_check("npm_vuln_high", cmd, p, t, parse_npm_audit, ok_codes=(0, 1))


def check_pyright(p: Project, t: int) -> Check:
    if not p.is_py:
        return Check("pyright_errors", note="不是 Python 项目")
    targets = pyright_targets(p)
    if not targets:
        return Check("pyright_errors", note="排除之后没有 Python 文件")
    cmd = py_tool("pyright", p.root)
    c = _json_check("pyright_errors", cmd and cmd + ["--outputjson", *targets], p, t, parse_pyright,
                    ok_codes=(0, 1), missing_note="没有 uvx，也没装 pyright")
    if c.command:
        c.command = " ".join(cmd + ["--outputjson", f"<排除后的 {len(targets)} 个 Python 文件>"])
    return c


def check_ruff(p: Project, t: int) -> Check:
    if not p.is_py:
        return Check("ruff_issues", note="不是 Python 项目")
    cmd = py_tool("ruff", p.root)
    return _json_check("ruff_issues", cmd and cmd + ruff_args(p),
                       p, t, parse_ruff, missing_note="没有 uvx，也没装 ruff")


def vulture_args(p: Project) -> list[str]:
    """vulture 的 --exclude 拿模式匹配绝对路径：项目本身在 .claude/ 这类被排除的目录下时，整个项目都被排除，
    死代码数误报为 0（2026-10-10 在一个位于 .claude/worktrees/ 下的真实项目复现）。改成只传排除后剩下的文件。"""
    return pyright_targets(p) + ["--min-confidence", "60"]


def check_vulture(p: Project, t: int) -> Check:
    if not p.is_py:
        return Check("vulture_items", note="不是 Python 项目")
    args = vulture_args(p)
    if not args[:-2]:
        return Check("vulture_items", note="排除之后没有 Python 文件")
    cmd = py_tool("vulture", p.root)
    # vulture 发现死代码时退出码是 3，属于正常结果
    c = _json_check("vulture_items", cmd and cmd + args, p, t, parse_vulture, ok_codes=(0, 3),
                    missing_note="没有 uvx，也没装 vulture")
    if c.command:
        c.command = " ".join(cmd + [f"<排除后的 {len(args) - 2} 个 Python 文件>", "--min-confidence", "60"])
    return c


def resolve_requirements(p: Project, tmp: Path, t: int) -> tuple[list[str], str, str]:
    """给 pip-audit 准备 -r 输入。返回 (参数列表, 来源说明, 错误)；错误非空表示没有可检查的输入。
    优先用 requirements*.txt；否则有 uv.lock 用 uv export --frozen 转换，
    有 poetry.lock 用 poetry export 转换（转换结果只含锁定的具体版本）。"""
    reqs = sorted(p.root.glob("requirements*.txt"))
    if reqs:
        args: list[str] = []
        for r in reqs:
            args += ["-r", str(r.relative_to(p.root))]
        return args, "requirements*.txt", ""
    if (p.root / "uv.lock").exists():
        uv = shutil.which("uv")
        if not uv:
            return [], "", "有 uv.lock 但没装 uv，无法转换（brew install uv / pipx install uv）"
        out = tmp / "uv-requirements.txt"
        code, o, e = run([uv, "export", "--frozen", "--no-hashes", "--format", "requirements-txt",
                          "-o", str(out)], p.root, t)
        if code != 0 or not out.exists():
            return [], "", f"uv export 失败（退出码 {code}）：{tail(e or o)}"
        return ["-r", str(out)], "uv.lock（uv export --frozen 转换）", ""
    if (p.root / "poetry.lock").exists():
        poetry = shutil.which("poetry")
        if not poetry:
            return [], "", "有 poetry.lock 但没装 poetry，无法转换（pipx install poetry）"
        out = tmp / "poetry-requirements.txt"
        code, o, e = run([poetry, "export", "--without-hashes", "--format", "requirements.txt",
                          "--output", str(out)], p.root, t)
        if code != 0 or not out.exists():
            return [], "", (f"poetry export 失败（退出码 {code}）：{tail(e or o)}"
                            "（Poetry 2.x 需要装 poetry-plugin-export 插件）")
        return ["-r", str(out)], "poetry.lock（poetry export 转换）", ""
    return [], "", "根目录没有 requirements*.txt / uv.lock / poetry.lock，未检查"


def check_pip_audit(p: Project, t: int) -> Check:
    if not p.is_py:
        return Check("pip_vuln", note="不是 Python 项目")
    cmd = py_tool("pip-audit", p.root)
    if cmd is None:
        return Check("pip_vuln", note="没有 uvx，也没装 pip-audit")
    with tempfile.TemporaryDirectory() as tmp:
        args, source, err = resolve_requirements(p, Path(tmp), t)
        if err:
            return Check("pip_vuln", note=err)
        # --disable-pip --no-deps：不临时建虚拟环境，直接查锁定的具体版本
        c = _json_check("pip_vuln", cmd + args + ["-f", "json", "--progress-spinner", "off",
                                                  "--disable-pip", "--no-deps"], p, t, parse_pip_audit,
                        ok_codes=(0, 1))
    if c.status == "ok":
        c.note = f"只检查锁定的具体版本（{source}）"
    return c


def check_jscpd(p: Project, t: int) -> Check:
    if not shutil.which("npx"):
        return Check("dup_lines", note="没有 npx，无法运行 jscpd")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["npx", "-y", "jscpd", ".", "--format", JSCPD_FORMATS, "--reporters", "json",
               "--output", tmp, "--silent"]
        cmd += ["--ignore", ",".join(exclude_globs(p))]
        c = Check("dup_lines", command=" ".join(cmd[:4]) + " --format <代码格式> --reporters json --output <临时目录> --silent")
        code, out, err = run(cmd, p.root, t)
        report = Path(tmp) / "jscpd-report.json"
        if code != 0 or not report.exists():
            c.status, c.note = "failed", f"退出码 {code}：{tail(err or out)}"
            return c
        try:
            c.value, c.details = parse_jscpd(json.loads(report.read_text(encoding="utf-8")))
            c.status = "ok"
        except (ValueError, KeyError) as e:
            c.status, c.note = "failed", f"报告无法解析：{e}"
        return c


def check_large_files(p: Project) -> tuple[Check, dict[str, int], int]:
    sizes = []
    by_ext: dict[str, int] = {}
    total = 0
    for rel in p.files:
        if rel.suffix not in CODE_EXTS:
            continue
        n = count_lines(p.root / rel)
        total += n
        by_ext[rel.suffix] = by_ext.get(rel.suffix, 0) + n
        if n > LARGE_FILE_LINES:
            sizes.append((n, str(rel)))
    sizes.sort(reverse=True)
    c = Check("large_files", value=len(sizes), status="ok", command="统计代码文件行数（脚本内置）",
              details=[f"{n} 行：{f}" for n, f in sizes[:20]])
    return c, by_ext, total


def hotspots(p: Project, since: str) -> list[str]:
    if not p.is_git:
        return []
    code, out, _ = run(["git", "log", f"--since={since}", "--format=", "--name-only"], p.root, 300)
    if code != 0:
        return []
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if (line and Path(line).suffix in CODE_EXTS and (p.root / line).exists()
                and not is_excluded(Path(line), p.exclude)):
            counts[line] = counts.get(line, 0) + 1
    return [f"{n} 次：{f}" for f, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]]


CHECKS = {
    "ts_errors": check_tsc,
    "eslint_problems": check_eslint,
    "swiftlint_issues": check_swiftlint,
    "knip_issues": check_knip,
    "npm_vuln_high": check_npm_audit,
    "pyright_errors": check_pyright,
    "ruff_issues": check_ruff,
    "vulture_items": check_vulture,
    "pip_vuln": check_pip_audit,
    "dup_lines": check_jscpd,
}


def run_checks(proj: Project, keys: list[str], timeout: int) -> list[Check]:
    results = []
    for key in keys:
        if key == "large_files":
            results.append(check_large_files(proj)[0])
            continue
        print(f"[体检] {METRICS[key]} …", file=sys.stderr, flush=True)
        results.append(CHECKS[key](proj, timeout))
    return results


# ---------------------------------------------------------------- 报告

STATUS_ZH = {"ok": "已测", "skipped": "跳过", "failed": "失败"}


def stack_summary(by_ext: dict[str, int]) -> str:
    """技术栈按各语言代码行数排序展示，不再只看 JS/TS/Python 三个布尔，
    避免以 Swift/Go/Rust 为主、只带了几个脚本的项目被标成 "Python"。"""
    lang_lines: dict[str, int] = {}
    for ext, n in by_ext.items():
        lang = EXT_LANG.get(ext, ext)
        lang_lines[lang] = lang_lines.get(lang, 0) + n
    return "、".join(f"{lang} {n:,} 行" for lang, n in sorted(lang_lines.items(), key=lambda kv: -kv[1])) or "未识别"


def render_report(proj: Project, checks: list[Check], hot: list[str], by_ext: dict[str, int],
                  total_lines: int, since: str, now: str) -> str:
    out = [f"# 项目体检报告 · {proj.root.name}", "",
           f"- 时间：{now}", f"- 路径：`{proj.root}`",
           "- 排除：" + ("、".join(f"`{x}`" for x in proj.exclude) or "无（只排除 node_modules、dist、.build 等常见目录）"),
           f"- 技术栈：{stack_summary(by_ext)}",
           f"- 代码总行数：{total_lines:,}（" + "，".join(f"{e} {n:,}" for e, n in sorted(by_ext.items(), key=lambda kv: -kv[1])) + "）",
           ""]
    if proj.is_js and not proj.has_node_modules:
        out += ["> [!warning] 项目没有 node_modules，TypeScript 和 knip 的结果会偏多或失败。先安装依赖再重跑体检。", ""]
    out += ["## 指标（都是越少越好）", "", "| 指标 | 数值 | 状态 | 说明 |", "|---|---|---|---|"]
    for c in checks:
        val = "—" if c.value is None else f"{c.value:,}"
        out.append(f"| {METRICS[c.key]} | {val} | {STATUS_ZH[c.status]} | {c.note.replace('|', '/')} |")
    out += ["", f"## bug 热点：{since} 以来改动最多的代码文件", ""]
    out += [f"- {h}" for h in hot] or ["- （不是 git 仓库，或这段时间没有提交）"]
    for c in checks:
        if c.details:
            out += ["", f"## {METRICS[c.key]} · 明细", ""] + [f"- {d}" for d in c.details]
    out += ["", "## 运行过的命令", ""] + [f"- `{c.command}`" for c in checks if c.command]
    out += ["", "## 怎么读这份报告", "",
            "- 死代码候选只是候选：按字符串注册的路由、动态加载、配置文件引用的代码，工具会误判。删除前逐条复核，清单交用户确认。",
            "- 热点文件和超长文件的交集，通常就是 bug 最多的地方，测试先补这里。",
            "- 状态为「失败」的指标不能当作 0，修好运行环境后重跑。",
            "- 用 `--save-baseline` 存下的基线，是之后「只许变好」检查的起点。"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 子命令

def resolve_root(raw: str) -> Path:
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"[错误] 项目路径不存在或不是目录：{root}。请传入项目根目录（--root）。")
    return root


def cmd_audit(a: argparse.Namespace) -> int:
    root = resolve_root(a.root)
    out_dir = Path(a.out).expanduser().resolve() if a.out else root / ".governance"
    proj = detect(root, a.exclude)
    keys = [k for k in METRICS if k not in set(a.skip)]
    checks = []
    large, by_ext, total = check_large_files(proj)
    for k in keys:
        checks.append(large if k == "large_files" else run_checks(proj, [k], a.timeout)[0])
    hot = hotspots(proj, a.since)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = {c.key: c.value for c in checks if c.status == "ok"}
    payload = {"created": now, "root": str(root), "exclude": proj.exclude, "metrics": metrics,
               "failed": {c.key: c.note for c in checks if c.status == "failed"}}
    report = out_dir / f"audit-{stamp}.md"
    report.write_text(render_report(proj, checks, hot, by_ext, total, a.since, now), encoding="utf-8")
    (out_dir / f"metrics-{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"体检报告：{report}")
    if a.save_baseline:
        base = out_dir / "baseline.json"
        if base.exists() and not a.force:
            print(f"[未覆盖] 基线已存在：{base}。确认要重设基线时加 --force。", file=sys.stderr)
            return 1
        base.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已保存：{base}（{len(metrics)} 项指标）")
    failed = [c for c in checks if c.status == "failed"]
    if failed:
        print(f"[注意] {len(failed)} 项检查失败，详见报告：" + "、".join(METRICS[c.key] for c in failed), file=sys.stderr)
    return 0


def compare(baseline: dict[str, int], current: dict[str, int | None]) -> tuple[list[str], list[str], list[str]]:
    """返回 (变多的, 变少的, 没测出来的)，每项是给人看的一行文字。纯函数，便于测试。"""
    worse, better, unknown = [], [], []
    for key, base in baseline.items():
        if key not in METRICS or not isinstance(base, int):
            continue
        cur = current.get(key)
        if cur is None:
            unknown.append(f"{METRICS[key]}：基线 {base}，这次没测出来")
        elif cur > base:
            worse.append(f"{METRICS[key]}：{base} → {cur}（多了 {cur - base}）")
        elif cur < base:
            better.append(f"{METRICS[key]}：{base} → {cur}（少了 {base - cur}）")
    return worse, better, unknown


def cmd_ratchet(a: argparse.Namespace) -> int:
    root = resolve_root(a.root)
    base_path = Path(a.baseline).expanduser().resolve() if a.baseline else root / ".governance" / "baseline.json"
    if not base_path.exists():
        print(f"[错误] 找不到基线：{base_path}。先运行 audit --save-baseline。", file=sys.stderr)
        return 2
    data = json.loads(base_path.read_text(encoding="utf-8"))
    baseline = data.get("metrics", {})
    proj = detect(root, data.get("exclude", []))  # 和建基线时用同一套排除规则
    keys = [k for k in METRICS if k in baseline]
    current = {c.key: (c.value if c.status == "ok" else None) for c in run_checks(proj, keys, a.timeout)}
    worse, better, unknown = compare(baseline, current)
    for line in better:
        print(f"[变好] {line}")
    for line in worse:
        print(f"[变差] {line}")
    for line in unknown:
        print(f"[无法判断] {line}")
    if a.update and better and not worse and not unknown:
        for k, v in current.items():
            if v is not None and k in baseline and v < baseline[k]:
                baseline[k] = v
        data["metrics"] = baseline
        data["updated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        base_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已下调：{base_path}")
    if worse:
        print("结果：不通过。新改动让上面的指标变多了，修掉新增的问题再提交。")
        return 1
    if unknown:
        print("结果：无法判断。有指标这次没测出来，修好运行环境后重跑，不能当作通过。")
        return 2
    print("结果：通过。没有任何指标变多。" + ("可以加 --update 把变少的指标写回基线。" if better and not a.update else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="项目体检与「只许变好」检查")
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("audit", help="体检：不改代码，写报告和指标")
    au.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    au.add_argument("--out", help="输出目录（默认 <项目>/.governance）")
    au.add_argument("--since", default="3 months ago", help="热点统计的时间范围（git log --since 的写法）")
    au.add_argument("--skip", nargs="*", default=[], choices=list(METRICS), help="跳过某些检查")
    au.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单项检查超时秒数")
    au.add_argument("--exclude", nargs="*", default=[],
                    help="不参与统计的目录或通配符，如 tests/fixtures generated/**（会存进基线）")
    au.add_argument("--save-baseline", action="store_true", help="把这次指标存成基线")
    au.add_argument("--force", action="store_true", help="基线已存在时允许覆盖")
    au.set_defaults(func=cmd_audit)
    ra = sub.add_parser("ratchet", help="只许变好：和基线比较，变多则退出码 1")
    ra.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    ra.add_argument("--baseline", help="基线文件（默认 <项目>/.governance/baseline.json）")
    ra.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单项检查超时秒数")
    ra.add_argument("--update", action="store_true", help="全部通过且有指标变少时，把基线下调")
    ra.set_defaults(func=cmd_ratchet)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())

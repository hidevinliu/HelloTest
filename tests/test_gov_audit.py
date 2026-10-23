"""gov_audit.py 的测试。运行：python3 -m unittest discover -s tests -v（在 skill 根目录）。
样例输出取自 2026-10-09 在真实项目上实测的工具输出。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import gov_audit as g  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_tsc_counts_each_error(self):
        text = "src/a.ts(3,5): error TS2322: x\nsrc/a.ts(9,1): error TS2304: y\nFound 2 errors."
        self.assertEqual(g.parse_tsc(text), 2)

    def test_tsc_clean(self):
        self.assertEqual(g.parse_tsc(""), 0)

    def test_knip_counts_every_category_item(self):
        text = json.dumps({"issues": [
            {"file": "package.json", "dependencies": [], "devDependencies": [{"name": "wrangler"}, {"name": "x"}]},
            {"file": "src/a.ts", "exports": [{"name": "foo"}], "files": []},
        ]})
        total, details = g.parse_knip(text)
        self.assertEqual(total, 3)
        self.assertIn("devDependencies 2", details[0])

    def test_knip_top_level_files(self):
        total, _ = g.parse_knip(json.dumps({"files": ["src/old.ts"], "issues": []}))
        self.assertEqual(total, 1)

    def test_npm_audit_counts_high_and_critical(self):
        text = json.dumps({"metadata": {"vulnerabilities": {"low": 3, "moderate": 5, "high": 11, "critical": 2}}})
        self.assertEqual(g.parse_npm_audit(text)[0], 13)

    def test_npm_audit_missing_field_raises(self):
        with self.assertRaises(ValueError):
            g.parse_npm_audit(json.dumps({"error": "no lockfile"}))

    def test_ruff(self):
        text = json.dumps([{"code": "F401"}, {"code": "F401"}, {"code": "E501"}])
        total, details = g.parse_ruff(text)
        self.assertEqual(total, 3)
        self.assertIn("F401 2", details[0])

    def test_pyright(self):
        self.assertEqual(g.parse_pyright(json.dumps({"summary": {"errorCount": 114}})), 114)

    def test_vulture_ignores_noise_lines(self):
        text = "a.py:2: unused function 'f' (60% confidence)\nInstalled 1 package\nb.py:9: unreachable code after 'return' (100% confidence)"
        self.assertEqual(g.parse_vulture(text)[0], 2)

    def test_pip_audit(self):
        text = json.dumps({"dependencies": [{"name": "requests", "version": "2.19.0", "vulns": [{}, {}]},
                                            {"name": "ok", "version": "1", "vulns": []}], "fixes": []})
        self.assertEqual(g.parse_pip_audit(text)[0], 2)

    def test_jscpd(self):
        report = {"statistics": {"total": {"duplicatedLines": 45, "clones": 5, "percentage": 2.96}},
                  "duplicates": [{"lines": 12, "firstFile": {"name": "a.ts", "start": 1, "end": 12},
                                  "secondFile": {"name": "b.ts", "start": 3, "end": 14}}]}
        lines, details = g.parse_jscpd(report)
        self.assertEqual(lines, 45)
        self.assertIn("a.ts:1-12 ↔ b.ts:3-14", details[1])

    def test_eslint(self):
        text = json.dumps([{"filePath": "/p/a.js", "errorCount": 2, "warningCount": 1},
                           {"filePath": "/p/b.js", "errorCount": 0, "warningCount": 0}])
        total, details = g.parse_eslint(text)
        self.assertEqual(total, 3)
        self.assertEqual(len(details), 1)

    def test_swiftlint(self):
        text = json.dumps([{"rule_id": "identifier_name", "severity": "Error"},
                           {"rule_id": "identifier_name", "severity": "Error"},
                           {"rule_id": "vertical_whitespace", "severity": "Warning"}])
        total, details = g.parse_swiftlint(text)
        self.assertEqual(total, 3)
        self.assertIn("identifier_name 2", details[0])

    def test_swiftlint_empty(self):
        self.assertEqual(g.parse_swiftlint("[]"), (0, []))


class CompareTests(unittest.TestCase):
    def test_worse_better_unknown(self):
        worse, better, unknown = g.compare(
            {"ts_errors": 10, "dup_lines": 100, "ruff_issues": 5},
            {"ts_errors": 12, "dup_lines": 90, "ruff_issues": None})
        self.assertEqual(len(worse), 1)
        self.assertIn("多了 2", worse[0])
        self.assertEqual(len(better), 1)
        self.assertEqual(len(unknown), 1)

    def test_equal_is_pass(self):
        self.assertEqual(g.compare({"ts_errors": 3}, {"ts_errors": 3}), ([], [], []))

    def test_unknown_keys_ignored(self):
        self.assertEqual(g.compare({"not_a_metric": 1}, {}), ([], [], []))


class RatchetEndToEnd(unittest.TestCase):
    """只用内置的超长文件指标，不依赖外部工具和网络。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "big.py").write_text("x = 1\n" * 600)
        (self.root / "small.py").write_text("y = 2\n")
        self.base = self.root / ".governance" / "baseline.json"
        self.base.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_baseline(self, n):
        self.base.write_text(json.dumps({"metrics": {"large_files": n}}))

    def ratchet(self, *extra):
        return g.main(["ratchet", "--root", str(self.root), *extra])

    def test_pass_when_equal(self):
        self.write_baseline(1)
        self.assertEqual(self.ratchet(), 0)

    def test_fail_when_new_large_file(self):
        self.write_baseline(1)
        (self.root / "big2.py").write_text("z = 3\n" * 700)
        self.assertEqual(self.ratchet(), 1)

    def test_update_only_lowers(self):
        self.write_baseline(3)
        self.assertEqual(self.ratchet("--update"), 0)
        self.assertEqual(json.loads(self.base.read_text())["metrics"]["large_files"], 1)

    def test_missing_baseline_is_exit_2(self):
        self.assertEqual(self.ratchet(), 2)

    def test_exclude_saved_in_baseline_is_reused(self):
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        (fixtures / "huge.py").write_text("a = 1\n" * 900)
        skip = [k for k in g.METRICS if k != "large_files"]
        rc = g.main(["audit", "--root", str(self.root), "--out", str(self.base.parent),
                     "--skip", *skip, "--exclude", "fixtures", "--save-baseline"])
        self.assertEqual(rc, 0)
        data = json.loads(self.base.read_text())
        self.assertEqual(data["metrics"]["large_files"], 1)  # fixtures/huge.py 不算
        self.assertEqual(data["exclude"], ["fixtures"])
        (fixtures / "huge2.py").write_text("b = 1\n" * 900)
        self.assertEqual(self.ratchet(), 0)  # 排除目录里新增的大文件不影响判断


class ExcludeMatching(unittest.TestCase):
    def test_dir_and_glob(self):
        self.assertTrue(g.is_excluded(Path("bench/x/a.py"), ["bench"]))
        self.assertTrue(g.is_excluded(Path("src/gen/a.ts"), ["src/gen/**"]))
        self.assertFalse(g.is_excluded(Path("benchmark.py"), ["bench"]))
        self.assertFalse(g.is_excluded(Path("src/a.ts"), []))


class ExcludeReachesEveryTool(unittest.TestCase):
    """--exclude 和内置排除目录要传到每个自己扫目录的工具，否则指标混进文档、构建产物。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("x = 1\n")
        (self.root / "docs" / "design").mkdir(parents=True)
        (self.root / "docs" / "design" / "b.py").write_text("y = 2\n")
        (self.root / "package.json").write_text("{}")
        self.proj = g.detect(self.root, ["docs"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_globs_cover_builtin_dirs_and_user_patterns(self):
        globs = g.exclude_globs(self.proj)
        self.assertIn("docs/**", globs)
        self.assertIn("**/.next/**", globs)
        self.assertIn("**/node_modules/**", globs)
        self.assertEqual(len(globs), len(set(globs)))

    def test_ruff_gets_extend_exclude(self):
        args = g.ruff_args(self.proj)
        i = args.index("--extend-exclude")
        self.assertIn("docs/**", args[i + 1].split(","))

    def test_pyright_only_checks_files_left_after_exclude(self):
        self.assertEqual(g.pyright_targets(self.proj), ["src/a.py"])

    def test_knip_gets_temp_ignore_config_when_project_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = g.knip_config(self.proj, Path(tmp))
            self.assertIsNotNone(cfg)
            data = json.loads(Path(cfg).read_text())
            self.assertIn("docs/**", data["ignore"])
            self.assertIn("**/.next/**", data["ignore"])

    def test_knip_keeps_project_config(self):
        (self.root / "knip.json").write_text("{}")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(g.knip_config(self.proj, Path(tmp)))


class VultureIgnoresWhereTheProjectLives(unittest.TestCase):
    """vulture 的 --exclude 拿模式匹配绝对路径：项目本身放在 .claude/ 这类被排除的目录下时，整个项目都被排除，
    死代码数误报为 0（2026-10-10 在一个位于 .claude/worktrees/ 下的真实项目复现）。改成只传排除后剩下的文件。"""

    def make_project(self, parent: Path) -> Path:
        root = parent / ".claude" / "worktrees" / "proj"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("def used():\n    return 1\n\n\ndef never_called():\n    return 2\n\n\nused()\n")
        (root / "docs").mkdir()
        (root / "docs" / "b.py").write_text("def doc_only():\n    return 3\n")
        return root

    def test_args_list_files_instead_of_scanning_dot(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = g.detect(self.make_project(Path(tmp)), [".claude", "docs"])
            args = g.vulture_args(proj)
            self.assertNotIn(".", args)
            self.assertNotIn("--exclude", args)
            self.assertIn("src/a.py", args)
            self.assertNotIn("docs/b.py", args)

    @unittest.skipUnless(shutil.which("uvx") or shutil.which("vulture"), "没有 uvx / vulture")
    def test_real_vulture_counts_dead_code_under_excluded_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = g.detect(self.make_project(Path(tmp)), [".claude", "docs"])  # 和那个真实项目的基线一样排除 .claude
            c = g.check_vulture(proj, 300)
            self.assertEqual(c.status, "ok", c.note)
            self.assertEqual(c.value, 1, c.details)


class AuditDoesNotTouchCode(unittest.TestCase):
    def test_audit_only_writes_into_out_dir(self):
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as out:
            p = Path(proj)
            (p / "a.py").write_text("def f():\n    return 1\n")
            before = {f: f.read_bytes() for f in p.rglob("*") if f.is_file()}
            skip = [k for k in g.METRICS if k != "large_files"]
            rc = g.main(["audit", "--root", proj, "--out", out, "--skip", *skip, "--save-baseline"])
            self.assertEqual(rc, 0)
            after = {f: f.read_bytes() for f in p.rglob("*") if f.is_file()}
            self.assertEqual(before, after)
            self.assertTrue((Path(out) / "baseline.json").exists())
            self.assertEqual(len(list(Path(out).glob("audit-*.md"))), 1)


class XcodeBuildDirsExcluded(unittest.TestCase):
    """Xcode/SwiftPM 的 .build、DerivedData 是自动生成的构建产物，
    扫进去会污染重复代码和超长文件指标（2026-10-23 实测：同一份生成代码在多个 .build 下被算成重复）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "App" / ".build" / "DerivedSources").mkdir(parents=True)
        (self.root / "App" / ".build" / "DerivedSources" / "generated.swift").write_text("x\n" * 600)
        (self.root / "App" / "Sources").mkdir(parents=True)
        (self.root / "App" / "Sources" / "a.swift").write_text("let a = 1\n")
        (self.root / "DerivedData" / "Index.noindex").mkdir(parents=True)
        (self.root / "DerivedData" / "Index.noindex" / "b.swift").write_text("let b = 2\n" * 700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_artifacts_not_in_file_list(self):
        files, _ = g.list_files(self.root, [])
        self.assertIn(Path("App/Sources/a.swift"), files)
        self.assertNotIn(Path("App/.build/DerivedSources/generated.swift"), files)
        self.assertNotIn(Path("DerivedData/Index.noindex/b.swift"), files)

    def test_large_files_ignores_build_artifacts(self):
        c, _, _ = g.check_large_files(g.detect(self.root, []))
        self.assertEqual(c.value, 0, c.details)


class StackSummaryTests(unittest.TestCase):
    """技术栈按各语言行数排序展示：以 Swift 为主的项目不应被标成 "Python"。"""

    def test_swift_dominant_project(self):
        s = g.stack_summary({".swift": 7999, ".py": 1069, ".js": 632})
        self.assertTrue(s.startswith("Swift 7,999 行"), s)
        self.assertIn("Python 1,069 行", s)
        self.assertIn("JavaScript 632 行", s)

    def test_merges_extensions_of_same_language(self):
        self.assertEqual(g.stack_summary({".js": 100, ".mjs": 50}), "JavaScript 150 行")

    def test_empty(self):
        self.assertEqual(g.stack_summary({}), "未识别")

    def test_audit_report_shows_swift_stack(self):
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as out:
            p = Path(proj)
            (p / "main.swift").write_text("let x = 1\n" * 800)
            (p / "tool.py").write_text("y = 2\n")
            skip = [k for k in g.METRICS if k != "large_files"]
            rc = g.main(["audit", "--root", proj, "--out", out, "--skip", *skip])
            self.assertEqual(rc, 0)
            report = next(Path(out).glob("audit-*.md")).read_text(encoding="utf-8")
            stack = next(ln for ln in report.splitlines() if ln.startswith("- 技术栈："))
            self.assertIn("Swift", stack)
            self.assertIn("Python", stack)
            self.assertLess(stack.index("Swift"), stack.index("Python"))


class SwiftLintCheck(unittest.TestCase):
    def test_non_swift_project_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("x = 1\n")
            c = g.check_swiftlint(g.detect(Path(tmp), []), 60)
            self.assertEqual(c.status, "skipped")

    @unittest.skipUnless(shutil.which("swiftlint"), "没有 swiftlint")
    def test_real_swiftlint_counts_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "probe.swift").write_text("let x = 1\n\n\nlet y = 2\n")
            c = g.check_swiftlint(g.detect(Path(tmp), []), 300)
            self.assertEqual(c.status, "ok", c.note)
            self.assertGreaterEqual(c.value, 1, c.details)


class PipAuditInputs(unittest.TestCase):
    """依赖漏洞检查的输入解析：requirements 优先，其次 uv.lock / poetry.lock 转换。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, path: str | None = None):
        old_path = os.environ.get("PATH", "")
        if path is not None:
            os.environ["PATH"] = path
        try:
            with tempfile.TemporaryDirectory() as tmp:
                return g.resolve_requirements(g.detect(self.root, []), Path(tmp), 120)
        finally:
            os.environ["PATH"] = old_path

    def empty_path(self) -> str:
        """PATH 里只放一个空目录，模拟 uv / poetry 都没装。"""
        bindir = self.root / "empty-bin"
        bindir.mkdir(exist_ok=True)
        return str(bindir)

    def test_requirements_preferred(self):
        (self.root / "requirements.txt").write_text("requests==2.31.0\n")
        (self.root / "uv.lock").write_text("")
        args, source, err = self.resolve()
        self.assertEqual(err, "")
        self.assertEqual(source, "requirements*.txt")
        self.assertEqual(args, ["-r", "requirements.txt"])

    def test_uv_lock_without_uv(self):
        (self.root / "uv.lock").write_text("version = 1")
        _, _, err = self.resolve(self.empty_path())
        self.assertIn("uv.lock", err)
        self.assertIn("uv", err)

    def test_poetry_lock_without_poetry(self):
        (self.root / "poetry.lock").write_text("# lock")
        _, _, err = self.resolve(self.empty_path())
        self.assertIn("poetry.lock", err)
        self.assertIn("poetry", err)

    def test_no_inputs(self):
        (self.root / "pyproject.toml").write_text("[project]\nname='x'\n")
        _, _, err = self.resolve()
        self.assertIn("未检查", err)

    def test_uv_lock_with_fake_uv_converts(self):
        """造一个假 uv 命令验证调用参数和产物路径（不需要真 uv）。"""
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "uv"
        fake.write_text("#!/bin/sh\n"
                        "while [ $# -gt 0 ]; do\n"
                        "  case \"$1\" in\n"
                        "    -o) out=\"$2\"; shift 2;;\n"
                        "    *) shift;;\n"
                        "  esac\n"
                        "done\n"
                        "printf 'requests==2.31.0\\n' > \"$out\"\n")
        fake.chmod(0o755)
        (self.root / "uv.lock").write_text("version = 1")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bindir)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args, source, err = g.resolve_requirements(
                    g.detect(self.root, []), Path(tmp), 120)
                self.assertEqual(err, "")
                self.assertIn("uv.lock", source)
                self.assertEqual(args[0], "-r")
                self.assertIn("requests==2.31.0", Path(args[1]).read_text())
        finally:
            os.environ["PATH"] = old_path


if __name__ == "__main__":
    unittest.main()

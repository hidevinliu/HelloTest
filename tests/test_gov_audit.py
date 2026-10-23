"""gov_audit.py 的测试。运行：python3 -m unittest discover -s tests -v（在 skill 根目录）。
样例输出取自 2026-10-09 在真实项目上实测的工具输出。"""
import json
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
    """vulture 的 --exclude 拿模式匹配绝对路径：项目本身放在 .claude/ 之类被排除的目录下时，整个项目都被排除，
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


class XcodeBuildDirsExcluded(unittest.TestCase):
    """Xcode / SwiftPM 的构建目录和 node_modules 一样是生成物，不参与统计（issue #1：
    .build 下每次构建重新生成的 resource_bundle_accessor.swift 被 jscpd 算成 79 行重复）。"""

    GENERATED = ("App/.build/out/DerivedSources/resource_bundle_accessor.swift",
                 "DerivedData/App/Build/gen.swift",
                 ".swiftpm/xcode/package.swift")

    def make_project(self, root: Path):
        (root / "Sources").mkdir()
        (root / "Sources" / "App.swift").write_text("let a = 1\n")
        for rel in self.GENERATED:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("let b = 2\n" * 600)

    def test_walk_fallback_skips_build_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            proj = g.detect(root)
            self.assertFalse(proj.is_git)
            self.assertEqual([f.as_posix() for f in proj.files], ["Sources/App.swift"])
            self.assertEqual(g.check_large_files(proj)[0].value, 0)

    @unittest.skipUnless(shutil.which("git"), "没有 git")
    def test_git_tracked_build_dirs_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            g.run(["git", "init", "-q"], root, 30)
            g.run(["git", "add", "-A"], root, 30)
            proj = g.detect(root)
            self.assertTrue(proj.is_git)
            self.assertEqual([f.as_posix() for f in proj.files], ["Sources/App.swift"])

    def test_tools_get_build_dirs_in_ignore_globs(self):
        with tempfile.TemporaryDirectory() as tmp:
            globs = g.exclude_globs(g.detect(Path(tmp)))
            for d in (".build", "DerivedData", ".swiftpm"):
                self.assertIn(f"**/{d}/**", globs)


class StackReflectsCodeShare(unittest.TestCase):
    """技术栈按各语言代码行数如实列出（issue #1：Swift 7,999 / Python 1,069 / JS 632 行的项目被报成「Python」）。"""

    def audit_report(self, files: dict[str, str]) -> str:
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as out:
            for rel, text in files.items():
                (Path(proj) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(proj) / rel).write_text(text)
            skip = [k for k in g.METRICS if k != "large_files"]
            self.assertEqual(g.main(["audit", "--root", proj, "--out", out, "--skip", *skip]), 0)
            return next(Path(out).glob("audit-*.md")).read_text(encoding="utf-8")

    def stack_line(self, report: str) -> str:
        return next(ln for ln in report.splitlines() if ln.startswith("- 技术栈："))

    def test_swift_first_project_lists_all_languages_by_lines(self):
        report = self.audit_report({
            "Package.swift": "// swift-tools-version:5.9\n",
            "Sources/App.swift": "let a = 1\n" * 80,
            "scripts/tool.py": "x = 1\n" * 10,
            "web/app.js": "let y = 1\n" * 6,
        })
        self.assertEqual(self.stack_line(report), "- 技术栈：Swift 81 行、Python 10 行、JavaScript 6 行")
        self.assertIn("Swift 没有专项检查", report)

    def test_config_only_language_still_listed(self):
        report = self.audit_report({"package.json": "{}", "tsconfig.json": "{}", "a.py": "x = 1\n"})
        self.assertEqual(self.stack_line(report), "- 技术栈：Python 1 行、JavaScript、TypeScript（只有配置文件，没有代码文件）")
        self.assertNotIn("没有专项检查", report)


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


if __name__ == "__main__":
    unittest.main()

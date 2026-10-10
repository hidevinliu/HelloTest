"""gov_audit.py 的测试。运行：python3 -m unittest discover -s tests -v（在 skill 根目录）。
样例输出取自 2026-10-09 在真实项目上实测的工具输出。"""
import json
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

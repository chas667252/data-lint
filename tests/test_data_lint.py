"""data-lint 的测试。只用标准库 unittest，不引入任何依赖。

运行：python -m unittest discover -s tests -v
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import data_lint as dl  # noqa: E402


def build(header, rows, **kwargs):
    return dl.analyze(header, rows, **kwargs)


def pick(report, category=None, severity=None):
    return [
        f for f in report["findings"]
        if (category is None or f["category"] == category)
        and (severity is None or f["severity"] == severity)
    ]


class TestCleanData(unittest.TestCase):
    def test_clean_file_has_no_findings(self):
        header = ["order_id", "amount", "status"]
        rows = [
            ["ORD-1", "10.00", "paid"],
            ["ORD-2", "20.00", "paid"],
            ["ORD-3", "30.00", "pending"],
        ]
        report = build(header, rows)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["summary"]["critical"], 0)


class TestStructure(unittest.TestCase):
    def test_empty_data_is_critical(self):
        report = build(["a", "b"], [])
        self.assertEqual(report["score"], 0)
        self.assertEqual(report["summary"]["critical"], 1)

    def test_duplicate_header_warns(self):
        report = build(["a", "a"], [["1", "2"], ["3", "4"]])
        self.assertTrue(pick(report, "structure", "warning"))

    def test_ragged_row_warns(self):
        report = build(["a", "b"], [["1", "2"], ["3"]])
        titles = [f["title"] for f in pick(report, "structure", "warning")]
        self.assertIn("存在列数不一致的行", titles)


class TestType(unittest.TestCase):
    def test_mixed_type_is_critical(self):
        rows = [["1"], ["2"], ["abc"], ["4"], ["5"], ["6"]]
        report = build(["amount"], rows)
        critical = [f for f in pick(report, "type", "critical") if f["title"] == "类型混杂"]
        self.assertTrue(critical)
        self.assertIn("abc", " ".join(critical[0]["examples"]))

    def test_leading_zero_warns(self):
        report = build(["code"], [["00123"], ["00456"]])
        titles = [f["title"] for f in report["findings"]]
        self.assertIn("数字列存在前导零", titles)

    def test_date_formats_inconsistent(self):
        report = build(["d"], [["2024-03-01"], ["2024/03/02"], ["2024-03-03"]])
        titles = [f["title"] for f in pick(report, "consistency", "warning")]
        self.assertIn("日期格式不一致", titles)


class TestCompleteness(unittest.TestCase):
    def test_null_ratio_over_threshold(self):
        rows = [["1"], [""], ["3"], ["4"], ["5"]]
        report = build(["a"], rows)
        found = [f for f in pick(report, "completeness", "warning") if f["title"] == "空值率偏高"]
        self.assertTrue(found)

    def test_all_null_column_is_critical(self):
        report = build(["a"], [[""], [""], [""]])
        titles = [f["title"] for f in pick(report, "completeness", "critical")]
        self.assertIn("整列为空", titles)


class TestUniqueness(unittest.TestCase):
    def test_duplicate_key_is_critical(self):
        rows = [["ORD-1", "10"], ["ORD-1", "20"], ["ORD-2", "30"]]
        report = build(["order_id", "amount"], rows, key_cols=["order_id"])
        self.assertTrue(pick(report, "uniqueness", "critical"))

    def test_duplicate_rows_warn(self):
        rows = [["1", "2"], ["1", "2"], ["3", "4"]]
        report = build(["a", "b"], rows)
        self.assertTrue(pick(report, "uniqueness", "warning"))

    def test_suggested_key_detected(self):
        report = build(["order_id", "v"], [["A", "1"], ["B", "2"], ["C", "3"]])
        self.assertIn("order_id", report["suggested_keys"])


class TestConsistency(unittest.TestCase):
    def test_case_variants_warn(self):
        rows = [["paid"], ["Paid"], ["PAID"], ["pending"]]
        report = build(["status"], rows)
        titles = [f["title"] for f in pick(report, "consistency", "warning")]
        self.assertIn("取值大小写或写法不一致", titles)

    def test_trailing_space_warn(self):
        report = build(["status"], [["paid"], ["paid "], ["pending"]])
        titles = [f["title"] for f in report["findings"]]
        self.assertIn("存在首尾空格", titles)


class TestOutput(unittest.TestCase):
    def test_json_is_serializable(self):
        report = build(["a"], [["1"], ["2"]])
        text = json.dumps(report, ensure_ascii=False)
        self.assertIn("findings", json.loads(text))

    def test_html_contains_sections(self):
        header = ["amount", "status"]
        rows = [["1", "paid"], ["abc", "Paid"], ["3", "paid"]]
        report = build(header, rows)
        html = dl.render_html(report)
        self.assertIn("<html", html)
        self.assertIn("问题清单", html)
        self.assertIn("严重", html)

    def test_text_report_renders(self):
        report = build(["a"], [["1"], ["1"]])
        text = dl.render_text(report)
        self.assertIn("健康度", text)


class TestIO(unittest.TestCase):
    def test_gbk_file_is_detected(self):
        content = "姓名,金额\n张三,10.00\n李四,20.00\n"
        path = ROOT / "_gbk_fixture.csv"
        try:
            path.write_bytes(content.encode("gbk"))
            header, rows, meta = dl.load_csv(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(meta["encoding"], "gbk")
        self.assertEqual(header, ["姓名", "金额"])
        self.assertEqual(len(rows), 2)

    def test_cli_end_to_end(self):
        target = ROOT / "examples" / "orders_dirty.csv"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "data_lint.py"), str(target), "--key", "order_id"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("健康度", proc.stdout)
        self.assertIn("主键冲突", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

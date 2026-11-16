"""示例场景：软件交付流水线（TDD 版）

需求解析 → 测试设计 → 编码实现 → 边界加固 → 交付评审
编码节点会真实执行单元测试，首轮故意失败以演示 Self-Loop 闭环。
"""
from __future__ import annotations

import json

SCENARIO = {
    "id": "software_delivery",
    "name": "软件交付流水线",
    "desc": "把一句需求交付成可运行、可验收的代码：测试先行，失败自愈，交叉复核",
    "task_default": "实现一个 CSV 数据清洗与统计 CLI 工具，输入订单 CSV，输出按城市聚合的 JSON 统计结果",
    "nodes": [
        {
            "id": "spec", "name": "需求解析", "role": "analyst", "deps": [],
            "outputs": ["spec.json"], "validator": "spec", "max_attempts": 2,
            "levels": ["l1", "l2"],
            "desc": "拆出可执行的验收标准",
        },
        {
            "id": "test", "name": "测试设计", "role": "tester", "deps": ["spec"],
            "outputs": ["test_main.py"], "validator": "test", "max_attempts": 2,
            "levels": ["l1", "l2", "l3"],
            "desc": "每条验收标准一个用例",
        },
        {
            "id": "risk", "name": "风险审查", "role": "reviewer", "deps": ["spec"],
            "outputs": ["risk.json"], "validator": "json", "max_attempts": 1,
            "required": ["risks", "mitigations"], "levels": ["l1"],
            "desc": "与测试设计并行识别边界、安全和交付风险",
        },
        {
            "id": "code", "name": "编码实现", "role": "coder", "deps": ["test", "risk"],
            "outputs": ["main.py"], "validator": "coding", "max_attempts": 3,
            "levels": ["l1", "l2", "l3"],
            "desc": "实现并跑通测试",
        },
        {
            "id": "hardening", "name": "边界加固", "role": "tester", "deps": ["code"],
            "outputs": ["test_edge.py"], "validator": "hardening", "max_attempts": 2,
            "levels": ["l1", "l3"],
            "desc": "补边界用例并回归",
        },
        {
            "id": "review", "name": "交付评审", "role": "reviewer",
            "deps": ["spec", "test", "risk", "code", "hardening"],
            "outputs": ["review.json"], "validator": "review", "max_attempts": 2,
            "levels": ["l1", "l2"],
            "desc": "独立复核并出交付结论",
        },
    ],
}

SYSTEM_PROMPTS = {
    "analyst": "你是资深需求分析师。把需求拆成可验证的验收标准，输出严格 JSON。",
    "tester": "你是测试工程师。每条验收标准对应至少一个 unittest 用例，覆盖正常流与边界流。",
    "coder": "你是 Python 工程师。只输出可直接运行的完整代码，接口与测试约定保持一致。",
    "reviewer": "你是独立评审人。逐条对照验收标准给出结论，输出严格 JSON。",
}

KB_QUERIES = {
    "spec": "数据清洗 验收标准 编写要求",
    "test": "验收标准 测试用例 边界",
    "code": "清洗规范 统计聚合口径 编码规范",
    "hardening": "边界条件 异常输入 编码规范",
    "review": "验收标准 评审 口径",
}

_SPEC_TMPL = {
    "project": "csv-insight",
    "task": "",
    "acceptance_criteria": [
        {"id": "C1", "text": "空 id、负数金额、非数字金额的行必须被剔除",
         "keywords": ["amount < 0", "float("]},
        {"id": "C2", "text": "按城市分组输出 count/sum/avg，数值保留两位小数",
         "keywords": ["by_city", "round("]},
        {"id": "C3", "text": "空输入不得崩溃，均值取 0.0",
         "keywords": ["if rows else", "count"]},
        {"id": "C4", "text": "结果以 UTF-8 JSON 输出，可直接被下游消费",
         "keywords": ["encoding", "json"]},
    ],
    "interfaces": {
        "clean(rows: list[dict]) -> list[dict]": "清洗行数据",
        "summarize(rows: list[dict]) -> dict": "聚合统计",
    },
}

_TEST_MAIN = '''"""验收测试：由测试工程师依据 spec.json 生成"""
import unittest

import main


class TestCleanRows(unittest.TestCase):
    def test_drop_blank_id(self):
        rows = [{"id": "", "city": "北京", "amount": "10"}]
        self.assertEqual(main.clean(rows), [])

    def test_drop_negative_amount(self):
        rows = [{"id": "1", "city": "北京", "amount": "-5"}]
        self.assertEqual(main.clean(rows), [])

    def test_drop_bad_number(self):
        rows = [{"id": "2", "city": "上海", "amount": "abc"}]
        self.assertEqual(main.clean(rows), [])

    def test_keep_valid_and_cast(self):
        rows = [{"id": "3", "city": " 杭州 ", "amount": "12.5"}]
        out = main.clean(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["city"], "杭州")
        self.assertIsInstance(out[0]["amount"], float)


class TestSummarize(unittest.TestCase):
    def test_empty_input(self):
        stats = main.summarize([])
        self.assertEqual(stats["count"], 0)
        self.assertEqual(stats["avg"], 0.0)

    def test_avg_precision(self):
        rows = [
            {"id": "1", "city": "北京", "amount": 50.0},
            {"id": "2", "city": "上海", "amount": 25.0},
            {"id": "3", "city": "北京", "amount": 25.0},
        ]
        stats = main.summarize(rows)
        self.assertEqual(stats["total"], 100.0)
        self.assertAlmostEqual(stats["avg"], 33.33, places=2)
        self.assertEqual(stats["by_city"]["北京"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
'''

_MAIN_V1 = '''"""CSV 数据清洗与统计工具（第一版）"""
import csv
import json


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def clean(rows):
    result = []
    for row in rows:
        if not (row.get("id") or "").strip():
            continue
        try:
            amount = float(row.get("amount") or 0)
        except ValueError:
            continue
        if amount < 0:
            continue
        result.append({
            "id": row["id"].strip(),
            "city": (row.get("city") or "未知").strip(),
            "amount": amount,
        })
    return result


def summarize(rows):
    total = sum(r["amount"] for r in rows)
    by_city = {}
    for row in rows:
        by_city.setdefault(row["city"], []).append(row["amount"])
    stats = {
        city: {
            "count": len(values),
            "sum": round(sum(values), 2),
            "avg": round(sum(values) / len(values), 2),
        }
        for city, values in by_city.items()
    }
    return {
        "count": len(rows),
        "total": round(total, 2),
        "avg": round(total // len(rows), 2),
        "by_city": stats,
    }


def main(path):
    rows = clean(load_rows(path))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys

    main(sys.argv[1])
'''

_MAIN_V2 = '''"""CSV 数据清洗与统计工具

清洗规则：空主键、负数金额、非数字金额一律剔除。
统计口径：均值 = 总量 / 有效行数，空集合返回 0.0。
"""
import csv
import json


def load_rows(path):
    """读取 CSV，返回字典列表。"""
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def clean(rows):
    """剔除脏数据并做类型归一。"""
    result = []
    for row in rows:
        if not (row.get("id") or "").strip():
            continue
        try:
            amount = float(row.get("amount") or 0)
        except ValueError:
            continue
        if amount < 0:
            continue
        result.append({
            "id": row["id"].strip(),
            "city": (row.get("city") or "未知").strip(),
            "amount": amount,
        })
    return result


def summarize(rows):
    """按城市聚合 count/sum/avg，空输入不抛异常。"""
    total = sum(r["amount"] for r in rows)
    by_city = {}
    for row in rows:
        by_city.setdefault(row["city"], []).append(row["amount"])
    stats = {
        city: {
            "count": len(values),
            "sum": round(sum(values), 2),
            "avg": round(sum(values) / len(values), 2),
        }
        for city, values in by_city.items()
    }
    return {
        "count": len(rows),
        "total": round(total, 2),
        "avg": round(total / len(rows), 2) if rows else 0.0,
        "by_city": stats,
    }


def main(path):
    rows = clean(load_rows(path))
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys

    main(sys.argv[1])
'''

_TEST_EDGE = '''"""边界加固用例：空白、零值、大数"""
import unittest

import main


class TestEdgeCases(unittest.TestCase):
    def test_whitespace_id_trimmed(self):
        rows = [{"id": " 7 ", "city": "深圳", "amount": "3"}]
        out = main.clean(rows)
        self.assertEqual(out[0]["id"], "7")

    def test_zero_amount_kept(self):
        rows = [{"id": "8", "city": "深圳", "amount": "0"}]
        self.assertEqual(len(main.clean(rows)), 1)

    def test_large_numbers(self):
        rows = main.clean([{"id": "9", "city": "广州", "amount": "1000000"}])
        self.assertEqual(main.summarize(rows)["total"], 1000000.0)

    def test_missing_city_default(self):
        rows = [{"id": "10", "city": "", "amount": "5"}]
        self.assertEqual(main.clean(rows)[0]["city"], "未知")


if __name__ == "__main__":
    unittest.main()
'''


def produce(node: dict, ctx: dict, attempt: int) -> dict:
    node_id = node["id"]
    if node_id == "spec":
        spec = json.loads(json.dumps(_SPEC_TMPL, ensure_ascii=False))
        spec["task"] = ctx.get("task", "")
        spec["version"] = attempt
        return {"spec.json": json.dumps(spec, ensure_ascii=False, indent=2)}

    if node_id == "test":
        return {"test_main.py": _TEST_MAIN}

    if node_id == "risk":
        return {"risk.json": json.dumps({
            "risks": ["非法数值输入", "空城市字段", "浮点精度", "大额数据边界"],
            "mitigations": ["输入过滤", "默认值", "两位小数", "边界回归测试"],
            "reviewer": "independent-risk-agent",
        }, ensure_ascii=False, indent=2)}

    if node_id == "code":
        # 能力不足的 Agent 首轮产出带边界缺陷；拿到失败反馈后（attempt>=2）
        # 或由强模型直接执行时才产出正确版本
        capability = float(ctx.get("agent_capability", 0.8))
        fixed = attempt >= 2 or capability >= 0.85
        return {"main.py": _MAIN_V2 if fixed else _MAIN_V1}

    if node_id == "hardening":
        return {"test_edge.py": _TEST_EDGE}

    if node_id == "review":
        validations = ctx.get("validations", {})
        criteria = (ctx.get("spec") or {}).get("acceptance_criteria", [])
        ok = bool(validations.get("code", {}).get("passed") and validations.get("hardening", {}).get("passed"))
        result = "pass" if ok else "fail"
        evidence = {
            "C1": ("test_main.py", "测试分别检查空 ID、负数和无法解析的金额。"),
            "C2": ("test_main.py", "聚合测试检查分组计数、总额和平均值精度。"),
            "C3": ("test_main.py", "test_empty_input 检查空数据的统计结果。"),
            "C4": ("main.py", "CLI 以 UTF-8 编码处理文件并序列化 JSON。"),
        }
        items = [
            {"id": c["id"], "result": result, "file": evidence[c["id"]][0],
             "reason": evidence[c["id"]][1]}
            for c in criteria
        ]
        data = {
            "verdict": result,
            "criteria": items,
        }
        return {"review.json": json.dumps(data, ensure_ascii=False, indent=2)}

    return {}

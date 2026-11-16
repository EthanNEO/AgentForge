"""盲测评测：固定任务 × 钉住规划模型，用平台独立核算的验收客观比较模型。

用法（服务需已启动）：
    python eval_blind.py --port 8000
    python eval_blind.py --providers deepseek,mimo --tasks signup,meeting

设计要点：
- 任务与样例固定，预期由平台独立核算（grounded 用例），模型看不到人工答案；
- provider 钉住规划模型且禁用跨模型回退，规划失败即记为 planning_failed；
- 执行 Worker 仍按平台默认调度（本工具比较的是规划模型，执行侧保持一致）；
- 纯 JSON 任务无浏览器用例，用独立内容核对判定业务通过；
- 简化计划（两次规划均无效）记为 no_auto_acceptance，不算业务通过；
- 业务失败但实现可能正确的运行列入 false_reject_review，需人工复核，不自动计为误拒。
"""
from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SIGNUP = ("做个活动报名名单整理工具。报名名单里有人重复填，也有人忘了留手机号。"
          "把重复的人合并，只保留最后一次填写的信息。没有手机号的人单独列出来。"
          "告诉我有多少人可以确认报名。做一份假名单演示整理前后区别。")
AA = ("做个出游 AA 算账工具\n我们几个朋友出去玩，有人垫了饭钱，有人付了打车费。帮我做个小工具，"
      "记下每笔是谁付的、花了多少钱、哪些人参与平摊，最后直接告诉我谁该转给谁多少钱。"
      "比如三个人吃饭花了 300 元，小李先付了，那另外两个人就各转给小李 100 元。"
      "再给我演示一下两个人分别垫过钱时怎么算。")
MEETING = ("做一个可操作的会议室预约冲突检查工具，支持录入两条预约。浏览器验收：A房间09:00到10:00与09:30到10:30重叠30分钟，"
           "首尾相接无冲突，不同房间同一时段无冲突。")
CSV = ("请读取附件 orders.csv 并生成一页订单汇总报告。排除负数金额和无法解析的金额，按城市展示有效订单数和金额合计，"
       "保留两位小数，并列出排除记录数。只使用附件的数据。")
JSON_TASK = ("将杭州 2 单 31.00 元、上海 1 单 8.50 元整理为一个城市数组。输出 JSON 格式。"
             "数组元素字段为 city、count、total，金额和订单数用数字。")

ORDERS_CSV = "city,amount\n上海,8.50\n杭州,12.00\n杭州,19.00\n北京,-3\n上海,abc\n"
JSON_EXPECTED = {"杭州": {"count": 2, "total": 31.0}, "上海": {"count": 1, "total": 8.5}}

TASKS = {
    "signup": {"label": "报名名单", "task": SIGNUP},
    "aa": {"label": "AA 算账", "task": AA},
    "meeting": {"label": "会议室冲突", "task": MEETING},
    "csv": {"label": "CSV 汇总", "task": CSV, "csv": True},
    "json": {"label": "纯 JSON", "task": JSON_TASK, "json_check": True},
}
PLATFORM_ASSERTIONS = {"expect_transfers", "expect_names", "expect_roster", "expect_conflict"}


class Eval:
    def __init__(self, base: str, timeout: int):
        self.base = base
        self.timeout = timeout

    def api(self, path: str, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))

    def upload_orders(self) -> str:
        result = self.api("/api/uploads", {"files": [{
            "name": "orders.csv", "type": "text/csv",
            "data": base64.b64encode(ORDERS_CSV.encode("utf-8")).decode("ascii"),
        }]})
        return result["upload_id"]

    def wait_run(self, run_id: str) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            detail = self.api(f"/api/runs/{run_id}")
            if detail["run"]["status"] in ("succeeded", "failed"):
                return detail
            time.sleep(3)
        raise TimeoutError(run_id)

    def fetch_artifact(self, item: dict) -> str:
        with urllib.request.urlopen(self.base + item["url"], timeout=30) as response:
            return response.read().decode("utf-8")


def grounded_cases(plan: dict) -> list:
    cases = []
    for node in plan.get("nodes", []):
        cfg = node.get("config", {})
        platform_node = bool(cfg.get("demo_roster") or cfg.get("meeting_scenarios"))
        for case in cfg.get("browser_cases", []):
            last = (case.get("actions") or [{}])[-1]
            if case.get("grounded") or platform_node or last.get("kind") in PLATFORM_ASSERTIONS:
                cases.append(case)
    return cases


def judge_business(detail: dict, plan: dict, spec: dict, eval_: Eval) -> tuple[str, str]:
    """Return (verdict, note): passed / failed / no_auto_acceptance."""
    if detail["run"]["status"] != "succeeded":
        return "failed", detail["run"].get("error") or "运行失败"
    if spec.get("json_check"):
        artifact = next((a for a in detail["artifacts"] if a["name"].endswith(".json")), None)
        if not artifact:
            return "failed", "缺少 JSON 产物"
        try:
            data = json.loads(eval_.fetch_artifact(artifact))
        except (ValueError, UnicodeError) as exc:
            return "failed", f"JSON 无法解析：{exc}"
        rows = data if isinstance(data, list) else data.get("cities") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return "failed", "JSON 不是城市数组"
        actual = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("city"), str):
                return "failed", "数组元素缺少 city 字段"
            if not isinstance(row.get("count"), (int, float)) or isinstance(row.get("count"), bool):
                return "failed", "count 不是数字"
            if not isinstance(row.get("total"), (int, float)) or isinstance(row.get("total"), bool):
                return "failed", "total 不是数字"
            actual[row["city"]] = {"count": row["count"], "total": float(row["total"])}
        if actual != JSON_EXPECTED:
            return "failed", f"内容不符：{actual}"
        return "passed", "独立内容核对通过"
    cases = grounded_cases(plan)
    if not cases:
        return "no_auto_acceptance", "无平台核算用例（简化计划或模型自拟），需人工核对"
    results = {}
    for stage in detail["nodes"]:
        browser = ((stage.get("latest") or {}).get("verdict") or {}).get("l1", {}).get("browser") or {}
        for row in browser.get("case_results", []):
            results[row["criterion"]] = row
        for criterion in browser.get("verified_criteria", []):
            results.setdefault(criterion, {"status": "passed"})
    failed = [c["criterion"] for c in cases
              if results.get(c["criterion"], {}).get("status") != "passed"]
    if failed:
        return "failed", "平台用例未通过：" + "、".join(map(str, failed))
    return "passed", f"{len(cases)} 条平台核算用例全部通过"


def run_arm(eval_: Eval, key: str, provider: str, upload_ids: list) -> dict:
    spec = TASKS[key]
    record = {"task": key, "label": spec["label"], "provider": provider,
              "status": None, "business": None, "note": "", "run_id": None,
              "attempts": None, "retries": None, "tokens": 0, "cost": 0.0,
              "duration_s": None, "planning_latency_s": None, "planning_model": None,
              "simplified": False, "fallbacks": 0, "cases": []}
    started = time.time()
    try:
        preview = eval_.api("/api/plan", {"task": spec["task"], "scenario": "custom",
                                          "provider": provider, "upload_ids": upload_ids})
    except urllib.error.HTTPError as exc:
        record["status"] = "planning_failed"
        record["business"] = "failed"
        record["note"] = f"规划失败（{exc.code}）：{exc.read().decode('utf-8', 'replace')[:300]}"
        record["duration_s"] = round(time.time() - started, 2)
        return record
    plan = preview["plan"]
    record["planning_model"] = plan.get("planning_model")
    record["simplified"] = plan.get("provenance") == "deterministic"
    started_run = eval_.api("/api/run", {"task": spec["task"], "scenario": "custom",
                                         "draft_id": preview["draft_id"], "upload_ids": upload_ids})
    record["run_id"] = started_run["run_id"]
    detail = eval_.wait_run(record["run_id"])
    stats = detail["run"].get("stats") or {}
    record["status"] = detail["run"]["status"]
    record["attempts"] = stats.get("attempts")
    record["retries"] = stats.get("retries")
    record["tokens"] = stats.get("tokens") or 0
    record["cost"] = stats.get("cost") or 0.0
    record["duration_s"] = stats.get("duration") or round(time.time() - started, 2)
    record["planning_latency_s"] = round(sum(
        row["latency"] for row in detail.get("model_usage", []) if row["phase"] == "planning"), 2)
    record["fallbacks"] = len(detail.get("gateway_fallbacks", []))
    for stage in detail["nodes"]:
        browser = ((stage.get("latest") or {}).get("verdict") or {}).get("l1", {}).get("browser") or {}
        record["cases"].extend(browser.get("case_results", []))
    record["business"], record["note"] = judge_business(detail, plan, spec, eval_)
    return record


def summarize(records: list) -> dict:
    summary = {}
    for provider in dict.fromkeys(r["provider"] for r in records):
        rows = [r for r in records if r["provider"] == provider]
        passed = sum(r["business"] == "passed" for r in rows)
        summary[provider] = {
            "runs": len(rows),
            "business_passed": passed,
            "business_failed": sum(r["business"] == "failed" and r["status"] != "planning_failed" for r in rows),
            "planning_failed": sum(r["status"] == "planning_failed" for r in rows),
            "no_auto_acceptance": sum(r["business"] == "no_auto_acceptance" for r in rows),
            "pass_rate": round(passed / len(rows), 3) if rows else 0,
            "total_tokens": sum(r["tokens"] for r in rows),
            "total_cost": round(sum(r["cost"] for r in rows), 4),
            "avg_duration_s": round(sum(r["duration_s"] or 0 for r in rows) / len(rows), 2) if rows else 0,
            "false_reject_review": [r["run_id"] for r in rows
                                    if r["business"] == "failed" and r["status"] == "succeeded"],
        }
    return summary


def render_markdown(records: list, summary: dict) -> str:
    lines = ["# AgentForge 盲测对照报告", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　固定 Harness，钉住规划模型，禁用跨模型回退。",
             "业务通过 = 平台独立核算用例全部通过（纯 JSON 为独立内容核对）。执行 Worker 按平台默认调度。", "",
             "## 按规划模型汇总", "",
             "| 规划模型 | 运行 | 业务通过 | 通过率 | 业务失败 | 规划失败 | 无自动验收 | Tokens | 成本(¥) | 平均耗时(s) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for provider, s in summary.items():
        lines.append(f"| {provider} | {s['runs']} | {s['business_passed']} | {s['pass_rate']:.0%} | "
                     f"{s['business_failed']} | {s['planning_failed']} | {s['no_auto_acceptance']} | "
                     f"{s['total_tokens']:,} | {s['total_cost']} | {s['avg_duration_s']} |")
    lines += ["", "## 逐次运行", "",
              "| 任务 | 规划模型 | Run | 状态 | 业务判定 | 尝试 | Tokens | 耗时(s) | 说明 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in records:
        lines.append(f"| {r['label']} | {r['provider']} | {r['run_id'] or '—'} | {r['status']} | "
                     f"{r['business']} | {r['attempts'] if r['attempts'] is not None else '—'} | "
                     f"{r['tokens']:,} | {r['duration_s']} | {r['note'][:120]} |")
    review = [p for p, s in summary.items() if s["false_reject_review"]]
    if review:
        lines += ["", "## 待人工复核（业务失败但运行成功，需确认是否误拒）", ""]
        for provider in review:
            lines.append(f"- {provider}: " + ", ".join(summary[provider]["false_reject_review"]))
    lines += ["", "注：误拒率需人工复核上表运行后另行统计；规划失败计入通过率分母，反映模型端到端可用性。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--providers", default="qwen,deepseek,mimo")
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    keys = [k.strip() for k in args.tasks.split(",") if k.strip()]
    for key in keys:
        if key not in TASKS:
            parser.error(f"未知任务：{key}")
    eval_ = Eval(f"http://127.0.0.1:{args.port}", args.timeout)
    upload_ids = [eval_.upload_orders()] if any(TASKS[k].get("csv") for k in keys) else []
    records = []
    for provider in providers:
        for key in keys:
            print(f"\n===== {TASKS[key]['label']} × {provider} =====", flush=True)
            try:
                record = run_arm(eval_, key, provider, upload_ids if TASKS[key].get("csv") else [])
            except Exception as exc:  # noqa: BLE001
                record = {"task": key, "label": TASKS[key]["label"], "provider": provider,
                          "status": "error", "business": "failed", "note": str(exc)[:300],
                          "run_id": None, "attempts": None, "retries": None, "tokens": 0,
                          "cost": 0.0, "duration_s": None, "planning_latency_s": None,
                          "planning_model": None, "simplified": False, "fallbacks": 0, "cases": []}
            records.append(record)
            print(f"状态={record['status']} 业务={record['business']} run={record['run_id']} "
                  f"tokens={record['tokens']} 耗时={record['duration_s']}s | {record['note'][:160]}", flush=True)
    summary = summarize(records)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_json = ROOT / "output" / f"eval-blind-{stamp}.json"
    out_md = ROOT / "output" / f"eval-blind-{stamp}.md"
    out_json.write_text(json.dumps({"records": records, "summary": summary}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    out_md.write_text(render_markdown(records, summary), encoding="utf-8")
    print("\n" + render_markdown(records, summary))
    print("结果已保存:", out_json, "和", out_md)


if __name__ == "__main__":
    main()

"""AA 自愈主线可复现演示：平台核算拒绝 → 失败归因 → 携带证据修复 → 通过。

用法（服务需已启动且为在线模式）：
    python demo_selfheal.py --port 8000
    python demo_selfheal.py --port 8000 --resume --shots

设计要点：
- 演示任务由 /api/demo/selfheal 生成：真实规划 + 平台独立核算用例；
- 首版实现注入历史真实错账页面（运行 20261116-012851-5870d071 的产物，
  初始化预载演示费用导致新录入叠加算出 140/200 错账），provenance=demo 明确标注；
- 平台核算拒绝、Self-Loop 归因、模型携带证据修复全部真实执行，不伪造验收；
- 脚本逐步打印可见界面状态与 URL，结束时输出证据报告（output/demo-selfheal-*）；
- --resume：若两次尝试后仍失败，自动走「从未完成步骤继续」携带证据续跑再验一遍；
- --shots：用真实浏览器在失败态与终态截图留证。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ASSERTIONS = {"expect_transfers", "expect_names", "expect_roster", "expect_conflict"}


class Demo:
    def __init__(self, base: str, timeout: int):
        self.base = base
        self.timeout = timeout

    def api(self, path: str, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))

    def wait_run(self, run_id: str, on_event=None) -> dict:
        deadline = time.time() + self.timeout
        seen_events = 0
        seen_status = {}
        while time.time() < deadline:
            detail = self.api(f"/api/runs/{run_id}")
            for event in detail["events"][seen_events:]:
                seen_events = len(detail["events"])
                kind = event["kind"]
                if kind in ("demo.fault_injected", "selfloop", "artifact.handoff",
                            "worker.completed", "optimizer.decision", "step.reused"):
                    print(f"    [{event['ts'][11:]}] {kind}: {event['payload'].get('message', '')[:160]}")
                if on_event:
                    on_event(event, detail)
            for node in detail["nodes"]:
                key = (node["id"], node["status"], len(node["attempts"]))
                if seen_status.get(node["id"]) != key:
                    seen_status[node["id"]] = key
                    print(f"    节点 {node['name']}（{node['role']}）→ {node['status']}"
                          f"（{len(node['attempts'])} 次尝试）")
            if detail["run"]["status"] in ("succeeded", "failed"):
                return detail
            time.sleep(3)
        raise TimeoutError(run_id)


def grounded_cases(plan: dict) -> list:
    cases = []
    for node in plan.get("nodes", []):
        cfg = node.get("config", {})
        for case in cfg.get("browser_cases", []):
            last = (case.get("actions") or [{}])[-1]
            if case.get("grounded") or last.get("kind") in PLATFORM_ASSERTIONS:
                cases.append(case)
    return cases


def attempt_evidence(detail: dict) -> dict:
    """Collect the failure/attribution/repair/pass evidence from a finished run."""
    evidence = {"injected": False, "rejected": False, "attribution": None,
                "repair_attempt": None, "passed": False, "grounded_total": 0,
                "grounded_passed": 0, "failure_details": []}
    for event in detail["events"]:
        if event["kind"] == "demo.fault_injected":
            evidence["injected"] = True
        if event["kind"] == "selfloop" and not evidence["attribution"]:
            evidence["attribution"] = event["payload"].get("attribution")
            evidence["patch_excerpt"] = str(event["payload"].get("patch", ""))[:400]
    for node in detail["nodes"]:
        for attempt in node["attempts"]:
            verdict = attempt.get("verdict") or {}
            if verdict.get("passed") is False and not evidence["rejected"]:
                evidence["rejected"] = True
                evidence["rejected_attempt"] = attempt["attempt"]
                l1 = verdict.get("l1") or {}
                browser = l1.get("browser") or {}
                evidence["failure_details"] = [
                    item.get("detail", "") for item in browser.get("case_results", [])
                    if item.get("status") == "failed"] or [l1.get("detail", "")]
            if evidence["rejected"] and attempt["attempt"] > evidence.get("rejected_attempt", 0):
                evidence["repair_attempt"] = attempt["attempt"]
        final = node.get("latest") or {}
        verdict = final.get("verdict") or {}
        plan_node = next((item for item in detail["plan"]["nodes"] if item["id"] == node["id"]), {})
        node_cases = [case for case in plan_node.get("config", {}).get("browser_cases", [])
                      if case.get("grounded")
                      or (case.get("actions") or [{}])[-1].get("kind") in PLATFORM_ASSERTIONS]
        evidence["grounded_total"] += len(node_cases)
        if verdict.get("passed"):
            evidence["passed"] = True
            browser = (verdict.get("l1") or {}).get("browser") or {}
            results = {item["criterion"]: item for item in browser.get("case_results", [])}
            verified = set(browser.get("verified_criteria", []))
            for case in node_cases:
                result = results.get(case["criterion"])
                if (result and result.get("status") == "passed") or case["criterion"] in verified:
                    evidence["grounded_passed"] += 1
    return evidence


def shoot(url: str, path: Path, wait_ms: int = 2500):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge", timeout=10000)
        except Exception:
            browser = playwright.chromium.launch(timeout=10000)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(path), full_page=True)
        browser.close()
    print(f"    截图：{path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AA 自愈主线演示")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--resume", action="store_true", help="失败时自动携带证据续跑")
    parser.add_argument("--shots", action="store_true", help="真实浏览器截图留证")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    demo = Demo(base, args.timeout)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"started_at": stamp, "base": base, "steps": []}
    shots_dir = ROOT / "output" / f"demo-selfheal-shots-{stamp}"

    health = demo.api("/api/health")
    if health.get("mode") != "live":
        print("演示需要在线模型执行真实修复：当前服务不是 live 模式。")
        return 2
    print("① 服务在线，模型可用：" + ", ".join(
        name for name, item in health["models"]["providers"].items() if item["configured"]))

    print("② 生成演示计划（真实规划 + 平台核算用例 + 首版注错声明）…")
    preview = demo.api("/api/demo/selfheal", {})
    cases = grounded_cases(preview["plan"])
    print(f"    计划节点 {len(preview['plan']['nodes'])} 个，平台核算用例 {len(cases)} 条")
    for case in cases:
        print("    用例：" + case["actions"][-1]["value"])
    report["steps"].append({"step": "plan", "draft_id": preview["draft_id"],
                            "grounded_cases": [c["actions"][-1]["value"] for c in cases]})
    if len(cases) < 2:
        print("平台核算用例不足，演示无法客观判定，终止。")
        return 1

    print("③ 确认执行，注入历史错账首版（provenance=demo）…")
    run = demo.api("/api/run", {"scenario": "custom", "draft_id": preview["draft_id"]})
    run_id = run["run_id"]
    monitor_url = f"{base}/?run={run_id}"
    workbench_url = f"{base}/workbench?run={run_id}"
    print(f"    监控台：{monitor_url}")
    print(f"    工作台：{workbench_url}")
    report["run_id"] = run_id
    report["urls"] = {"monitor": monitor_url, "workbench": workbench_url}

    failure_shot_done = False

    def on_event(event, detail):
        nonlocal failure_shot_done
        if args.shots and not failure_shot_done and event["kind"] == "selfloop":
            shots_dir.mkdir(parents=True, exist_ok=True)
            try:
                shoot(monitor_url, shots_dir / "01-monitor-rejected.png")
                shoot(workbench_url, shots_dir / "02-workbench-rejected.png")
                failure_shot_done = True
            except Exception as exc:
                print(f"    截图失败（不影响演示）：{exc}")

    detail = demo.wait_run(run_id, on_event)
    evidence = attempt_evidence(detail)
    print(f"④ 首轮运行结束：{detail['run']['status']}")
    report["first_run"] = {"run_id": run_id, "status": detail["run"]["status"], "evidence": evidence}

    if detail["run"]["status"] != "succeeded" and args.resume:
        print("⑤ 首轮未通过，走「从未完成步骤继续」携带证据续跑…")
        retry = demo.api(f"/api/runs/{run_id}/retry-plan", {"reuse": True})
        resumed = demo.api("/api/run", {"scenario": "custom", "draft_id": retry["draft_id"]})
        resume_id = resumed["run_id"]
        print(f"    续跑监控台：{base}/?run={resume_id}")
        print(f"    续跑工作台：{base}/workbench?run={resume_id}")
        detail = demo.wait_run(resume_id)
        evidence = attempt_evidence(detail)
        report["resume_run"] = {"run_id": resume_id, "status": detail["run"]["status"],
                                "evidence": evidence,
                                "urls": {"monitor": f"{base}/?run={resume_id}",
                                         "workbench": f"{base}/workbench?run={resume_id}"}}
        run_id = resume_id
        monitor_url = f"{base}/?run={run_id}"
        workbench_url = f"{base}/workbench?run={run_id}"
        print(f"    续跑结束：{detail['run']['status']}")

    if args.shots:
        shots_dir.mkdir(parents=True, exist_ok=True)
        try:
            shoot(monitor_url, shots_dir / "03-monitor-final.png")
            shoot(workbench_url, shots_dir / "04-workbench-final.png")
        except Exception as exc:
            print(f"    截图失败（不影响演示）：{exc}")

    ok = (evidence["injected"] and evidence["rejected"] and evidence["attribution"]
          and evidence["repair_attempt"] and detail["run"]["status"] == "succeeded"
          and evidence["grounded_total"] > 0
          and evidence["grounded_passed"] == evidence["grounded_total"])
    report["narrative_complete"] = ok
    report["finished_at"] = time.strftime("%Y%m%d-%H%M%S")
    out_json = ROOT / "output" / f"demo-selfheal-{stamp}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 自愈叙事核验 =====")
    print(f"  注错首版（demo）：{'✓' if evidence['injected'] else '✗'}")
    print(f"  平台核算拒绝：{'✓' if evidence['rejected'] else '✗'}")
    for item in evidence["failure_details"][:2]:
        print(f"    证据：{item[:180]}")
    print(f"  失败归因：{evidence['attribution'] or '✗'}")
    print(f"  携带证据修复（第 {evidence['repair_attempt']} 次尝试）：{'✓' if evidence['repair_attempt'] else '✗'}")
    print(f"  平台用例通过：{evidence['grounded_passed']}/{evidence['grounded_total']}")
    print(f"  最终状态：{detail['run']['status']}")
    print(f"  报告：{out_json}")
    print(f"  可见界面：{workbench_url}")
    if ok:
        print("\n演示主线完整跑通：失败 → 归因 → 修复 → 通过。")
        return 0
    print("\n演示主线未完整成立，请查看报告与运行记录（不伪造验收）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""本轮竞赛整改实机验收 · 阶段 1/2：P0-1 复杂任务多节点协作、P0-2 界面建 Agent 被选中。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parents[1] / "output"


def api(path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_run(run_id, timeout=900):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        detail = api(f"/api/runs/{run_id}")
        sig = json.dumps([(n["id"], n["status"], len(n["attempts"])) for n in detail["nodes"]])
        if sig != last:
            last = sig
            print("   ", sig, flush=True)
        if detail["run"]["status"] in ("succeeded", "failed"):
            return detail
        time.sleep(4)
    raise TimeoutError(run_id)


def phase1():
    print("== P0-1 复杂自由任务：多节点 DAG + 多角色 + 产物交接 ==", flush=True)
    task = ("构建一个团队活动经费报销工具（可操作网页）：先做需求分析，再实现报销录入与审批页面，"
            "然后编写测试报告，最后输出评审结论和风险清单。")
    plan = api("/api/plan", {"task": task, "scenario": "custom"})
    nodes = plan["plan"]["nodes"]
    roles = sorted({n["role"] for n in nodes})
    print(f"  计划：{len(nodes)} 节点，角色 {roles}，复杂度 {plan['plan'].get('complexity')}", flush=True)
    assert len(nodes) >= 3, "复杂任务必须 >=3 节点"
    assert len(roles) >= 2, "必须 >=2 种角色"
    assert any(n.get("deps") for n in nodes), "必须有依赖边"
    run = api("/api/run", {"scenario": "custom", "draft_id": plan["draft_id"]})
    run_id = run["run_id"]
    print(f"  运行 {run_id}：{BASE}/?run={run_id}", flush=True)
    detail = wait_run(run_id)
    resume_id = None
    if detail["run"]["status"] != "succeeded":
        print("  首轮未通过，走平台「从未完成步骤继续」携带失败证据续跑…", flush=True)
        retry = api(f"/api/runs/{run_id}/retry-plan", {"reuse": True})
        resumed = api("/api/run", {"scenario": "custom", "draft_id": retry["draft_id"]})
        resume_id = resumed["run_id"]
        print(f"  续跑 {resume_id}：{BASE}/?run={resume_id}", flush=True)
        detail = wait_run(resume_id)
    events = detail["events"]
    handoffs = [e for e in events if e["kind"] == "artifact.handoff"]
    executed_roles = sorted({a["role"] for n in detail["nodes"] for a in n["attempts"] if a.get("role")})
    agents_used = sorted({a.get("agent_name") for n in detail["nodes"] for a in n["attempts"] if a.get("agent_name")})
    result = {
        "task": task, "run_id": run_id, "resume_run_id": resume_id,
        "final_run_id": resume_id or run_id, "status": detail["run"]["status"],
        "node_count": len(nodes), "roles": roles, "executed_roles": executed_roles,
        "agents_used": agents_used, "handoff_count": len(handoffs),
        "handoffs": [{"files": e["payload"].get("files"), "message": e["payload"].get("message")} for e in handoffs],
        "complexity": plan["plan"].get("complexity"),
        "monitor_url": f"{BASE}/?run={resume_id or run_id}",
        "workbench_url": f"{BASE}/workbench?run={resume_id or run_id}",
    }
    print(f"  结果：{detail['run']['status']}，交接事件 {len(handoffs)}，执行角色 {executed_roles}", flush=True)
    assert detail["run"]["status"] == "succeeded", "复杂任务运行未成功"
    assert len(handoffs) >= 2, "产物交接事件不足"
    assert len(executed_roles) >= 2, "真实执行角色不足"
    return result


def phase2():
    print("== P0-2 界面创建 Agent → 被调度选中 → 统计更新 ==", flush=True)
    created = api("/api/agents", {"name": "编码新星·验收", "role": "coder",
                                   "model": "deepseek-flash", "skills": {"coder": 0.99},
                                   "cost_per_call": 0.001, "description": "竞赛验收：通过 API/面板创建",
                                   "tools": ["write_file", "run_tests", "kb_search", "run_script"]})
    agent = created["agent"]
    rank = api("/api/agents/rank?role=coder")
    top = rank["ranked"][0]
    print(f"  创建 {agent['id']}，排序第一：{top['agent_id']}（{top['reason']}）", flush=True)
    assert top["agent_id"] == agent["id"], "新建 Agent 应排第一"
    task = "制作一个可操作的倒计时页面"
    plan = api("/api/plan", {"task": task, "scenario": "custom"})
    print(f"  简单任务计划节点数：{len(plan['plan']['nodes'])}（应保持单节点）", flush=True)
    assert len(plan["plan"]["nodes"]) == 1, "简单任务不应被扩展"
    run = api("/api/run", {"scenario": "custom", "draft_id": plan["draft_id"]})
    run_id = run["run_id"]
    print(f"  运行 {run_id}：{BASE}/workbench?run={run_id}", flush=True)
    detail = wait_run(run_id)
    chosen = [(a.get("agent_id"), a.get("agent_name"), a["status"])
              for n in detail["nodes"] for a in n["attempts"]]
    print(f"  执行记录：{chosen}，状态 {detail['run']['status']}", flush=True)
    after = api(f"/api/agents/{agent['id']}")
    result = {
        "agent_id": agent["id"], "rank_top": top, "run_id": run_id,
        "status": detail["run"]["status"], "attempts": chosen,
        "agent_after": {k: after[k] for k in ("success", "failure", "total_tokens", "total_cost", "success_rate", "status")},
        "workbench_url": f"{BASE}/workbench?run={run_id}",
    }
    assert detail["run"]["status"] == "succeeded", "验收任务未成功"
    assert any(a[0] == agent["id"] for a in chosen), "新建 Agent 未被选中执行"
    assert after["success"] >= 1 and after["total_tokens"] > 0, "统计未更新"
    return result


def main():
    results = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        results["p01_complex_collaboration"] = phase1()
        results["p02_agent_crud_selection"] = phase2()
        results["ok"] = True
    except AssertionError as exc:
        results["ok"] = False
        results["failure"] = str(exc)
    out = OUT / f"acceptance-p01-p02-{stamp}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据：{out}", flush=True)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

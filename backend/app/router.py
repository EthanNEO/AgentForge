"""路由层：纯函数式接口定义，不依赖任何 Web 框架。

server.py 负责把它挂到 HTTP 上；测试也可以直接调用这些函数。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict
from threading import Lock
from urllib.parse import quote

from . import config, db, knowledge, selfloop
from .engine import Engine
from .events import events
from .github_mcp import GitHubMcpConfig
from .llm import llm
from .browser_check import check_browser_environment
from .llm import track_usage
from .planner import create_plan, plan_custom, ground_attachment_checks, ground_aa_demo_cases
from .scheduler import scheduler, seed_agents
from .uploads import create_upload, describe_file, materialize_uploads, resolve_uploads
from scenarios import all_scenarios, get as get_scenario

_JSON = dict


def bootstrap():
    db.get_conn()
    seed_agents()
    from scenarios.aiops import SEED_DOCS as AIOPS_DOCS

    knowledge.kb.seed_if_empty(AIOPS_DOCS)
    knowledge.kb.reload()


def meta():
    scenarios = all_scenarios()
    return {
        "mode": config.mode_label(),
        "weights": config.SCHEDULER_WEIGHTS,
        "scenarios": scenarios,
        "default_scenario": scenarios[0]["id"],
        "scenario": scenarios[0],
        "kb_size": len(knowledge.kb.docs),
        "version": "2.0.0",
        "models": llm.status(),
    }


def agents():
    return scheduler.summary()


AGENT_ROLES = ("analyst", "coder", "tester", "reviewer",
               "triage", "diagnostician", "operator", "verifier")
AGENT_TOOLS = ("write_file", "append_file", "read_file", "list_files", "run_tests", "kb_search", "run_script")
_AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")


def _agent_fields(body: dict, existing: dict | None = None) -> dict:
    """Validate a create/update payload; only provided fields change on update."""
    def pick(key, default):
        return body[key] if key in body else (existing.get(key) if existing else default)

    name = str(pick("name", "") or "").strip()
    role = str(pick("role", "") or "").strip()
    model = str(pick("model", "") or "").strip()
    description = str(pick("description", "") or "").strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("name 必填，且不超过 40 字")
    if role not in AGENT_ROLES:
        raise ValueError("role 仅支持 " + " / ".join(AGENT_ROLES))
    if not model or len(model) > 80:
        raise ValueError("model 必填，且不超过 80 字符")
    if len(description) > 200:
        raise ValueError("description 不超过 200 字")

    skills = pick("skills", None)
    if skills is None:
        skills = existing["skills"] if existing else {role: 0.9}
    if isinstance(skills, str):
        skills = db.json_load(skills, None)
    if not isinstance(skills, dict) or not skills:
        raise ValueError("skills 须为非空对象，如 {\"coder\": 0.9}")
    cleaned_skills = {}
    for key, value in skills.items():
        if key not in AGENT_ROLES:
            raise ValueError(f"skills 含未知角色：{key}")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"skills.{key} 须为 0 到 1 的数字") from None
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"skills.{key} 须在 0 到 1 之间")
        cleaned_skills[key] = round(number, 3)

    tools = pick("tools", None)
    if tools is None:
        tools = existing["tools"] if existing else ["write_file", "run_tests", "kb_search", "run_script"]
    if isinstance(tools, str):
        tools = db.json_load(tools, None)
    if not isinstance(tools, list) or any(t not in AGENT_TOOLS for t in tools):
        raise ValueError("tools 仅支持 " + " / ".join(AGENT_TOOLS))

    try:
        cost = float(pick("cost_per_call", 0.003))
    except (TypeError, ValueError):
        raise ValueError("cost_per_call 须为数字") from None
    if not 0.0 <= cost <= 1.0:
        raise ValueError("cost_per_call 须在 0 到 1 之间（每 1k tokens 估算成本）")

    status = str(pick("status", "ready") or "ready").strip()
    if status not in ("ready", "offline"):
        raise ValueError("status 仅支持 ready / offline（degraded 由调度器自动管理）")

    return {"name": name, "role": role, "model": model, "description": description,
            "skills": cleaned_skills, "tools": list(dict.fromkeys(tools)),
            "cost_per_call": round(cost, 6), "status": status}


def agents_create(body: dict):
    fields = _agent_fields(body)
    agent_id = str(body.get("id") or "").strip() or "agent-" + uuid.uuid4().hex[:8]
    if not _AGENT_ID.match(agent_id):
        raise ValueError("id 须为 2-41 位小写字母开头的字母/数字/下划线/连字符")
    if db.one("SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise ValueError(f"Agent 已存在：{agent_id}")
    db.execute(
        "INSERT INTO agents(id,name,role,description,model,skills,tools,cost_per_call,"
        "avg_latency,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, fields["name"], fields["role"], fields["description"], fields["model"],
         json.dumps(fields["skills"], ensure_ascii=False), json.dumps(fields["tools"]),
         fields["cost_per_call"], 1.0, fields["status"], db.now()),
    )
    return {"ok": True, "agent": next(r for r in scheduler.summary() if r["id"] == agent_id)}


def agent_detail(agent_id: str):
    row = next((r for r in scheduler.summary() if r["id"] == agent_id), None)
    if row is None:
        raise KeyError(agent_id)
    return row


def agents_update(agent_id: str, body: dict):
    existing = next((r for r in scheduler.summary() if r["id"] == agent_id), None)
    if existing is None:
        raise KeyError(agent_id)
    fields = _agent_fields(body, existing)
    db.execute(
        "UPDATE agents SET name=?, role=?, description=?, model=?, skills=?, tools=?, "
        "cost_per_call=?, status=? WHERE id=?",
        (fields["name"], fields["role"], fields["description"], fields["model"],
         json.dumps(fields["skills"], ensure_ascii=False), json.dumps(fields["tools"]),
         fields["cost_per_call"], fields["status"], agent_id),
    )
    return {"ok": True, "agent": agent_detail(agent_id)}


def agents_delete(agent_id: str):
    if not db.one("SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise KeyError(agent_id)
    busy = db.one("SELECT id FROM worker_sessions WHERE agent_id=? AND status='running'", (agent_id,))
    if busy:
        raise ValueError("Agent 正在执行任务，不能删除")
    db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    return {"ok": True, "id": agent_id}


def agents_rank(query: dict):
    role = (query.get("role") or [""])[0].strip()
    if role not in AGENT_ROLES:
        raise ValueError("role 仅支持 " + " / ".join(AGENT_ROLES))
    return {"role": role, "weights": config.SCHEDULER_WEIGHTS, "ranked": scheduler.rank(role)}


def health():
    github = GitHubMcpConfig.from_env(os.environ)
    return {
        "ok": True,
        "mode": config.mode_label(),
        "models": llm.status(),
        "mcp": {"github": github.status()},
    }


def kb_list():
    rows = db.query("SELECT id,title,source,tags,content,created_at FROM kb ORDER BY id")
    return {"total": len(rows), "items": rows}


def kb_add(body: dict):
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    if not title or not content:
        raise ValueError("title 与 content 必填")
    knowledge.kb.ingest(title, content, body.get("source") or "手动录入", body.get("tags") or "")
    knowledge.kb.reload()
    return {"ok": True, "total": len(knowledge.kb.docs)}


def lessons():
    return selfloop.known_lessons()


_confirming_drafts = set()
_confirmation_lock = Lock()


def preview_plan(body: dict):
    sid = body.get("scenario") or "software_delivery"
    resolved = resolve_uploads(body.get("upload_ids", []))
    attachments = [
        {key: value for key, value in item.items() if key != "source_path"}
        for item in resolved
    ]
    task = (body.get("task") or "").strip()
    provider = (body.get("provider") or "").strip().lower() or None
    if provider and provider not in ("qwen", "deepseek", "mimo"):
        raise ValueError("provider 仅支持 qwen / deepseek / mimo")
    if sid == "custom":
        if not task:
            raise ValueError("请描述你的任务")
        reserved_run_id = new_run_id()
        with track_usage("planning", reserved_run_id):
            scenario = plan_custom(task, attachments, provider=provider)
        ground_attachment_checks(task, scenario, resolved)
    else:
        scenario = get_scenario(sid).SCENARIO
        task = task or scenario["task_default"]
    plan = create_plan(task, scenario, config.mode_label())
    result = {
        "task": task,
        "scenario": {"id": scenario["id"], "name": scenario["name"]},
        "plan": plan.to_dict(),
        "upload_ids": body.get("upload_ids", []),
        "attachments": attachments,
    }
    if scenario.get("clarification"):
        result["clarification"] = scenario["clarification"]
    if sid == "custom":
        token = uuid.uuid4().hex
        db.execute("INSERT INTO plan_drafts(id,payload) VALUES(?,?)", (
            token, json.dumps((task, scenario, result["upload_ids"], reserved_run_id), ensure_ascii=False)
        ))
        result["draft_id"] = token
    return result


def restore_plan(draft_id: str):
    saved = db.one("SELECT payload FROM plan_drafts WHERE id=?", (draft_id,))
    if saved:
        task, scenario, upload_ids, _, *source = json.loads(saved["payload"])
        source_run = source[0] if source else None
        attachments = run_detail(source_run)["inputs"] if source_run else [
            {key: value for key, value in item.items() if key != "source_path"}
            for item in resolve_uploads(upload_ids)
        ]
        restored = {
            "task": task, "draft_id": draft_id, "upload_ids": upload_ids,
            "scenario": {"id": scenario["id"], "name": scenario["name"]},
            "plan": create_plan(task, scenario, config.mode_label()).to_dict(),
            "attachments": attachments, "source_run": source_run, "reuse_passed": bool(scenario.get("reuse_from")),
        }
        if scenario.get("clarification"):
            restored["clarification"] = scenario["clarification"]
        return restored
    existing = db.one("SELECT id FROM runs WHERE draft_id=?", (draft_id,))
    if existing:
        return {"run_id": existing["id"]}
    raise KeyError(draft_id)


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def start_run(body: dict):
    import threading

    sid = body.get("scenario") or "software_delivery"
    upload_ids = body.get("upload_ids", [])
    draft_id = None
    source_run = None
    if sid == "custom":
        draft_id = body.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            raise ValueError("请先生成并确认自由任务计划")
        existing = db.one("SELECT id FROM runs WHERE draft_id=?", (draft_id,))
        if existing:
            return {"run_id": existing["id"]}
        with _confirmation_lock:
            if draft_id in _confirming_drafts:
                raise ValueError("计划正在确认，请稍后重试")
            saved = db.one("SELECT payload FROM plan_drafts WHERE id=?", (draft_id,))
            if not saved:
                existing = db.one("SELECT id FROM runs WHERE draft_id=?", (draft_id,))
                if existing:
                    return {"run_id": existing["id"]}
                raise ValueError("请先生成并确认自由任务计划")
            task, scenario, upload_ids, run_id, *source = json.loads(saved["payload"])
            source_run = source[0] if source else None
            if scenario.get("clarification") and body.get("acknowledge_clarification") is not True:
                raise ValueError(scenario["clarification"])
            _confirming_drafts.add(draft_id)
    else:
        scenario = get_scenario(sid).SCENARIO
        task = (body.get("task") or scenario["task_default"]).strip()
        run_id = new_run_id()
    try:
        mode = config.mode_label()
        if sid == "custom" and any(node.get("browser_cases") for node in scenario["nodes"]):
            browser = check_browser_environment()
            if browser["status"] != "passed":
                raise ValueError("暂时无法开始浏览器验收：" + browser["detail"] + "。修复后可重新确认同一计划。")
        plan = create_plan(task, scenario, mode) if sid == "custom" else None
        inputs = config.RUNS_DIR / run_id / "inputs"
        if source_run:
            original = config.RUNS_DIR / source_run / "inputs"
            if original.is_dir() and not original.is_symlink():
                inputs.mkdir(parents=True, exist_ok=True)
                for path in original.iterdir():
                    if path.is_file() and not path.is_symlink():
                        shutil.copyfile(path, inputs / path.name)
        else:
            materialize_uploads(upload_ids, inputs)
        with db.transaction() as conn:
            if draft_id and not conn.execute("DELETE FROM plan_drafts WHERE id=?", (draft_id,)).rowcount:
                return {"run_id": run_id}
            conn.execute(
                "INSERT INTO runs(id,task,scenario,status,mode,created_at,plan,plan_version,draft_id,source_run) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, task, scenario["id"], "queued", mode, db.now(),
                 json.dumps(plan.to_dict(), ensure_ascii=False) if plan else None,
                 plan.version if plan else None, draft_id, source_run),
            )
    finally:
        if draft_id:
            with _confirmation_lock:
                _confirming_drafts.discard(draft_id)

    def fail_start(message: str):
        db.execute(
            "UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?",
            (message, db.now(), run_id),
        )

    def worker():
        try:
            Engine(run_id, task, scenario, mode).run()
        except Exception as exc:
            fail_start(f"后台执行失败：{exc}")

    try:
        threading.Thread(target=worker, daemon=True, name=f"run-{run_id}").start()
    except RuntimeError as exc:
        fail_start(f"无法启动后台执行：{exc}")
    return {"run_id": run_id}


def retry_plan(run_id: str, *, reuse=False):
    run = db.one("SELECT task, scenario, status, plan FROM runs WHERE id=?", (run_id,))
    if not run:
        raise KeyError(run_id)
    plan = _loads(run["plan"])
    if run["status"] != "failed" or run["scenario"] != "custom" or not plan.get("nodes"):
        raise ValueError("此运行没有可重试的自由任务计划")
    nodes = []
    for saved in plan["nodes"]:
        node = dict(saved)
        config_fields = node.pop("config", {})
        nodes.append({**config_fields, **node})
    scenario = {"id": "custom", "name": "自由任务", "nodes": nodes}
    if plan.get("clarification"):
        scenario["clarification"] = plan["clarification"]
    if reuse:
        scenario["reuse_from"] = run_id
    token = uuid.uuid4().hex
    db.execute("INSERT INTO plan_drafts(id,payload) VALUES(?,?)", (
        token, json.dumps((run["task"], scenario, [], new_run_id(), run_id), ensure_ascii=False)
    ))
    return restore_plan(token)


AA_DEMO_TASK = (
    "做个出游 AA 算账工具\n我们几个朋友出去玩，有人垫了饭钱，有人付了打车费。帮我做个小工具，"
    "记下每笔是谁付的、花了多少钱、哪些人参与平摊，最后直接告诉我谁该转给谁多少钱。"
    "比如三个人吃饭花了 300 元，小李先付了，那另外两个人就各转给小李 100 元。"
    "再给我演示一下两个人分别垫过钱时怎么算。"
)
DEMO_SOURCE_RUN = "20261116-012851-5870d071"
DEMO_NOTE = (
    "首版实现注入历史真实错账页面（初始化预载演示费用，导致新录入叠加算出 140/200 错账）；"
    "平台核算拒绝、失败归因与模型携带证据修复全部真实执行。"
)


def demo_selfheal(body: dict):
    """AA 自愈演示：真实规划 + 平台核算用例 + 首版注入历史错账实现。

    注入只替换第一次尝试的产物（provenance=demo），验收、归因、修复均走真实链路。
    """
    if config.mode_label() != "live":
        raise ValueError("自愈演示需要在线模型执行真实修复，请先配置模型密钥")
    task = AA_DEMO_TASK
    reserved_run_id = new_run_id()
    with track_usage("planning", reserved_run_id):
        scenario = plan_custom(task, [])
    ground_attachment_checks(task, scenario, [])
    node = next((n for n in scenario["nodes"] if "index.html" in n.get("outputs", [])), None)
    if node is None:
        raise ValueError("演示规划未产出页面节点，请重试")
    ground_aa_demo_cases(task, node)
    if not any(case.get("grounded") for case in node.get("browser_cases", [])):
        raise ValueError("演示需要平台核算用例，本次规划未生成，请重试")
    injection = {
        "node": node["id"], "attempt": 1, "file": "index.html",
        "fixture": "aa_faulty_index.html", "source_run": DEMO_SOURCE_RUN, "note": DEMO_NOTE,
    }
    scenario["demo_inject"] = injection
    node["demo_inject"] = injection
    plan = create_plan(task, scenario, config.mode_label())
    result = {
        "task": task,
        "scenario": {"id": scenario["id"], "name": scenario["name"]},
        "plan": plan.to_dict(),
        "upload_ids": [],
        "attachments": [],
        "demo": {"kind": "aa_selfheal", "label": "AA 自愈演示",
                 "source_run": DEMO_SOURCE_RUN, "note": DEMO_NOTE},
    }
    token = uuid.uuid4().hex
    db.execute(
        "INSERT INTO plan_drafts(id,payload) VALUES(?,?)",
        (token, json.dumps((task, scenario, [], reserved_run_id), ensure_ascii=False)),
    )
    result["draft_id"] = token
    return result


def runs():
    rows = db.query(
        "SELECT id,task,scenario,status,mode,created_at,finished_at,stats FROM runs "
        "WHERE NOT EXISTS (SELECT 1 FROM hidden_run_history WHERE run_id = runs.id) "
        "ORDER BY created_at DESC, id DESC LIMIT 30"
    )
    for r in rows:
        r["stats"] = _loads(r.get("stats"))
    return rows


def _loads(value):
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def run_detail(run_id: str):
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise KeyError(run_id)
    run.pop("draft_id", None)
    run["stats"] = _loads(run.get("stats"))
    scenario = get_scenario(run.get("scenario") or "software_delivery").SCENARIO
    steps = db.query("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,))
    trace = db.query("SELECT * FROM traces WHERE run_id=? ORDER BY id", (run_id,))
    lesson_rows = db.query("SELECT * FROM lessons WHERE run_id=? ORDER BY id", (run_id,))
    worker_rows = db.query("SELECT * FROM worker_sessions WHERE run_id=? ORDER BY started_at,id", (run_id,))
    event_rows = [asdict(event) for event in events.list(run_id)]

    for s in steps:
        s["verdict"] = _loads(s.get("verdict"))
    for t in trace:
        t["payload"] = _loads(t.get("payload"))

    nodes = []
    planned_nodes = _loads(run.get("plan")).get("nodes", []) if run["scenario"] == "custom" else scenario["nodes"]
    for node in planned_nodes:
        mine = [s for s in steps if s["node_id"] == node["id"]]
        nodes.append({
            "id": node["id"],
            "name": node["name"],
            "role": node["role"],
            "deps": node["deps"],
            "outputs": node.get("outputs", []),
            "desc": node.get("desc", ""),
            "status": mine[-1]["status"] if mine else "pending",
            "attempts": mine,
            "latest": mine[-1] if mine else None,
        })

    plan = _loads(run.get("plan"))
    usage = db.query(
        "SELECT phase,node_id,provider,model,reported_tokens,responses,metered_responses,outcome,latency "
        "FROM model_usage WHERE run_id=? ORDER BY id", (run_id,)
    )
    fallbacks = db.query(
        "SELECT phase,node_id,from_provider,from_model,category,wait_seconds,to_provider,to_model,detail,"
        "cooldown_seconds,created_at "
        "FROM gateway_fallbacks WHERE run_id=? ORDER BY id", (run_id,)
    )
    verifications = [event for event in event_rows if event["kind"] == "verification.completed"]
    feedback_cycles = [event for event in event_rows if event["kind"] in {
        "optimizer.decision", "selfloop", "watchdog.timeout", "experience.applied"
    }]
    artifact_dir = config.RUNS_DIR / run_id / "artifacts"
    previewable = {".html", ".txt", ".md", ".json", ".py", ".csv", ".log", ".yaml", ".yml"}
    artifacts = []
    if artifact_dir.is_dir() and not artifact_dir.is_symlink():
        for path in sorted(artifact_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.is_symlink():
                continue
            artifacts.append({
                "name": path.name,
                "size": path.stat().st_size,
                "url": f"/api/runs/{run_id}/artifacts/{quote(path.name, safe='')}",
                "previewable": path.suffix.lower() in previewable,
            })
    input_dir = config.RUNS_DIR / run_id / "inputs"
    inputs = []
    if input_dir.is_dir() and not input_dir.is_symlink():
        for path in sorted(input_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.is_symlink():
                continue
            item = describe_file(path)
            item.update({
                "url": f"/api/runs/{run_id}/inputs/{quote(path.name, safe='')}",
                "previewable": item["kind"] in {"text", "image"},
            })
            inputs.append(item)
    return {
        "run": run, "plan": plan, "model_usage": usage, "gateway_fallbacks": fallbacks,
        "nodes": nodes, "workers": worker_rows,
        "events": event_rows, "verifications": verifications,
        "feedback_cycles": feedback_cycles, "traces": trace, "lessons": lesson_rows,
        "artifacts": artifacts, "inputs": inputs,
        "provenance": {"mode": run.get("mode"), "offline": run.get("mode") == "mock"},
    }


def events_for_run(run_id: str, query: dict | None = None):
    if not db.one("SELECT id FROM runs WHERE id=?", (run_id,)):
        raise KeyError(run_id)
    query = query or {}
    raw_after = query.get("after", ["0"])
    raw_limit = query.get("limit", ["500"])
    after = int(raw_after[0] if isinstance(raw_after, list) else raw_after)
    limit = int(raw_limit[0] if isinstance(raw_limit, list) else raw_limit)
    items = [asdict(event) for event in events.list(run_id, after_id=after, limit=limit)]
    return {"items": items, "next": items[-1]["id"] if items else after}


def artifact_path(run_id: str, name: str):
    artifact_dir = config.RUNS_DIR / run_id / "artifacts"
    path = artifact_dir / name
    if artifact_dir.is_symlink() or path.is_symlink():
        raise KeyError(name)
    try:
        root = artifact_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KeyError(name) from None
    if not resolved.is_file() or resolved.parent != root:
        raise KeyError(name)
    return resolved


def input_path(run_id: str, name: str):
    input_dir = config.RUNS_DIR / run_id / "inputs"
    path = input_dir / name
    if input_dir.is_symlink() or path.is_symlink():
        raise KeyError(name)
    try:
        root = input_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KeyError(name) from None
    if not resolved.is_file() or resolved.parent != root:
        raise KeyError(name)
    return resolved


ROUTES = [
    ("GET", r"^/api/meta$", lambda p, b: meta()),
    ("GET", r"^/api/agents$", lambda p, b: agents()),
    ("POST", r"^/api/agents$", lambda p, b: agents_create(b)),
    ("GET", r"^/api/agents/rank$", lambda p, b: agents_rank(b)),
    ("GET", r"^/api/agents/(?P<agent_id>[\w\-]+)$", lambda p, b: agent_detail(p["agent_id"])),
    ("PUT", r"^/api/agents/(?P<agent_id>[\w\-]+)$", lambda p, b: agents_update(p["agent_id"], b)),
    ("DELETE", r"^/api/agents/(?P<agent_id>[\w\-]+)$", lambda p, b: agents_delete(p["agent_id"])),
    ("POST", r"^/api/demo/selfheal$", lambda p, b: demo_selfheal(b)),
    ("GET", r"^/api/kb$", lambda p, b: kb_list()),
    ("POST", r"^/api/kb$", lambda p, b: kb_add(b)),
    ("GET", r"^/api/lessons$", lambda p, b: lessons()),
    ("GET", r"^/api/plan/(?P<draft_id>[a-f0-9]{32})$", lambda p, b: restore_plan(p["draft_id"])),
    ("POST", r"^/api/plan$", lambda p, b: preview_plan(b)),
    ("POST", r"^/api/uploads$", lambda p, b: create_upload(b)),
    ("POST", r"^/api/runs/(?P<run_id>[\w\-]+)/retry-plan$", lambda p, b: retry_plan(p["run_id"], reuse=b.get("reuse") is True)),
    ("POST", r"^/api/run$", lambda p, b: start_run(b)),
    ("GET", r"^/api/runs$", lambda p, b: runs()),
    ("GET", r"^/api/runs/(?P<run_id>[\w\-]+)$", lambda p, b: run_detail(p["run_id"])),
    ("GET", r"^/api/runs/(?P<run_id>[\w\-]+)/events$", lambda p, b: events_for_run(p["run_id"], b)),
    ("GET", r"^/api/health$", lambda p, b: health()),
]

"""场景二：AIOps 故障处置

告警分诊 → 根因定位 → 处置执行 → 恢复验证 → 复盘沉淀
处置脚本与健康检查都在沙箱中真实执行，恢复与否由脚本退出码决定。
"""
from __future__ import annotations

import json

SCENARIO = {
    "id": "aiops",
    "name": "AIOps 故障处置",
    "desc": "从告警到复盘：定位根因、执行处置、验证恢复，治标不算通过",
    "task_default": "订单服务出现 503 告警，P95 时延超标，请完成故障处置并验证恢复",
    "nodes": [
        {
            "id": "triage", "name": "告警分诊", "role": "triage", "deps": [],
            "outputs": ["triage.json"], "validator": "json",
            "required": ["severity", "affected", "symptoms"],
            "max_attempts": 2, "levels": ["l1"],
            "desc": "定级、圈定影响面、提取症状",
        },
        {
            "id": "diagnose", "name": "根因定位", "role": "diagnostician", "deps": ["triage"],
            "outputs": ["diagnosis.json"], "validator": "json",
            "required": ["root_cause", "evidence", "plan"],
            "max_attempts": 2, "levels": ["l1"],
            "desc": "给证据链和处置计划",
        },
        {
            "id": "remediate", "name": "处置执行", "role": "operator", "deps": ["diagnose"],
            "outputs": ["fix.py"], "validator": "remediate",
            "script": "fix.py", "check": "health_check.py",
            "max_attempts": 3, "levels": ["l1"],
            "desc": "执行修复并做健康检查",
        },
        {
            "id": "verify", "name": "恢复验证", "role": "verifier", "deps": ["remediate"],
            "outputs": [], "validator": "health", "check": "health_check.py",
            "max_attempts": 1, "levels": ["l1"],
            "desc": "独立复核服务状态",
        },
        {
            "id": "postmortem", "name": "复盘沉淀", "role": "reviewer", "deps": ["verify"],
            "outputs": ["postmortem.json"], "validator": "json",
            "required": ["verdict", "root_cause", "actions"],
            "max_attempts": 1, "levels": ["l1"],
            "desc": "出复盘报告与改进项",
        },
    ],
}

SYSTEM_PROMPTS = {
    "triage": "你是值班 SRE。对告警做定级、圈定影响面，输出严格 JSON。",
    "diagnostician": "你是故障诊断专家。给出根因、证据链与分层处置计划，输出严格 JSON。",
    "operator": "你是运维执行工程师。只输出可执行的 Python 处置脚本，读写 service.json。",
    "verifier": "你是独立验证人。执行健康检查并给出结论。",
    "reviewer": "你是复盘负责人。输出复盘报告 JSON，包含结论、根因与改进项。",
}

KB_QUERIES = {
    "triage": "故障分级 告警 影响面",
    "diagnose": "根因分析 连接池 超时 排查",
    "remediate": "变更处置 连接池 副本 超时",
    "verify": "健康检查 恢复 基线",
    "postmortem": "复盘 改进项",
}

SEED_DOCS = [
    {
        "title": "故障分级标准 v1.1",
        "source": "SRE 手册",
        "tags": "分诊,定级",
        "content": (
            "故障分级：P0 全站不可用；P1 核心链路大面积失败；P2 单服务错误率或时延超标但可降级；"
            "P3 边缘功能异常。P2 需 30 分钟内给出恢复方案。定级需同时说明影响服务、影响用户比例、"
            "是否有可用降级路径。"
        ),
    },
    {
        "title": "连接池与超时排查手册",
        "source": "中间件规范",
        "tags": "根因,连接池,超时",
        "content": (
            "503 常见根因排序：连接池上限过低导致获取连接超时 > 下游超时阈值设置不合理导致重试风暴 > "
            "单副本无冗余放大故障。处置基线：最大连接数不低于 100，超时阈值不低于 2000ms，"
            "副本数不少于 2。仅调大超时不解决连接耗尽，属于治标。"
        ),
    },
    {
        "title": "变更与处置规范",
        "source": "运维规范",
        "tags": "处置,变更",
        "content": (
            "所有处置必须脚本化、可回滚、可在沙箱预演。禁止手工改配置不留痕。"
            "处置后必须执行健康检查并留存输出，健康检查不通过不得关闭故障单。"
        ),
    },
]

_SERVICE_JSON = {
    "service": "order-service",
    "max_connections": 5,
    "timeout_ms": 800,
    "replicas": 1,
    "version": "v3.2.1",
}

_SERVICE_PY = '''"""模拟服务：读取 service.json，给出健康状态与指标。"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(BASE, "service.json"), encoding="utf-8") as f:
        return json.load(f)


def health():
    cfg = load_config()
    reasons = []
    if cfg.get("max_connections", 0) < 100:
        reasons.append("连接池上限 %s < 100，高并发下连接耗尽返回 503" % cfg.get("max_connections"))
    if cfg.get("timeout_ms", 0) < 2000:
        reasons.append("超时阈值 %sms < 2000ms，重试风暴放大故障" % cfg.get("timeout_ms"))
    if cfg.get("replicas", 0) < 2:
        reasons.append("副本数 %s < 2，单点故障无冗余" % cfg.get("replicas"))
    p95 = 1200 if cfg.get("max_connections", 0) < 100 else 180
    if p95 > 500:
        reasons.append("P95 时延 %sms 超过 500ms 基线" % p95)
    return {"healthy": not reasons, "p95": p95, "reasons": reasons}
'''

_HEALTH_CHECK = '''"""健康检查基线：由平台预置，处置方不可修改。"""
import sys

import service

result = service.health()
if result["healthy"]:
    print("HEALTHY")
    print("P95=%sms" % result["p95"])
    sys.exit(0)

print("UNHEALTHY")
for reason in result["reasons"]:
    print("- " + reason)
sys.exit(1)
'''

_FIX_V1 = '''"""处置脚本 v1：仅调高超时阈值"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(BASE, "service.json")

with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

cfg["timeout_ms"] = 3000

with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print("已调整 timeout_ms=3000")
'''

_FIX_V2 = '''"""处置脚本 v2：按基线修复连接池、超时与副本"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(BASE, "service.json")

with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

cfg["max_connections"] = 200
cfg["timeout_ms"] = 3000
cfg["replicas"] = 2

with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print("已修复：max_connections=200, timeout_ms=3000, replicas=2")
'''

_TRIAGE = {
    "severity": "P2",
    "affected": ["order-service"],
    "affected_ratio": "约 35% 订单接口返回 503",
    "symptoms": [
        "接口 503 错误率上升至 35%",
        "P95 时延 1200ms，超过 500ms 基线",
        "错误集中在高并发时段，低峰期正常",
    ],
    "degradable": True,
    "note": "存在降级路径，可先限流保核心链路",
}

_DIAGNOSIS = {
    "root_cause": "order-service 连接池上限配置为 5，远低于 100 的基线，高并发下连接耗尽直接返回 503；"
                  "同时超时阈值 800ms 低于 2000ms 基线，触发重试风暴；单副本部署无冗余",
    "evidence": [
        "service.json: max_connections=5（基线 ≥100）",
        "service.json: timeout_ms=800（基线 ≥2000）",
        "service.json: replicas=1（基线 ≥2）",
        "P95=1200ms，与连接等待时间吻合",
    ],
    "hypothesis_chain": "连接池耗尽 → 获取连接超时 → 上层重试 → 压力放大 → 时延进一步恶化",
    "plan": [
        "第一步：连接池上限提升到 200",
        "第二步：超时阈值调整到 3000ms",
        "第三步：副本扩到 2，消除单点",
        "第四步：执行健康检查，确认三条基线全部满足",
    ],
}


def prepare(work, ctx=None):
    """预置被观测服务与验收基线。"""
    from app import tools

    tools.write_artifact(work, "service.json", json.dumps(_SERVICE_JSON, ensure_ascii=False, indent=2))
    tools.write_artifact(work, "service.py", _SERVICE_PY)
    tools.write_artifact(work, "health_check.py", _HEALTH_CHECK)


def produce(node: dict, ctx: dict, attempt: int) -> dict:
    node_id = node["id"]

    if node_id == "triage":
        return {"triage.json": json.dumps(_TRIAGE, ensure_ascii=False, indent=2)}

    if node_id == "diagnose":
        return {"diagnosis.json": json.dumps(_DIAGNOSIS, ensure_ascii=False, indent=2)}

    if node_id == "remediate":
        # 能力不足的 Agent 只做治标处置；拿到失败反馈或由强模型执行时按基线全治
        capability = float(ctx.get("agent_capability", 0.8))
        fixed = attempt >= 2 or capability >= 0.85
        return {"fix.py": _FIX_V2 if fixed else _FIX_V1}

    if node_id == "verify":
        return {}

    if node_id == "postmortem":
        diagnosis = (ctx.get("docs") or {}).get("diagnose") or _DIAGNOSIS
        remediate = (ctx.get("validations") or {}).get("remediate") or {}
        data = {
            "verdict": "closed" if remediate.get("passed") else "open",
            "root_cause": diagnosis.get("root_cause", ""),
            "timeline": [
                "T+0 告警触发，503 错误率 35%",
                "T+3min 完成分诊，定级 P2",
                "T+8min 定位连接池耗尽为根因",
                "T+15min 完成处置并通过健康检查",
            ],
            "actions": [
                "将连接池上限纳入配置基线巡检",
                "超时阈值统一由配置中心下发",
                "单副本服务纳入高可用专项治理",
            ],
            "prevention": "在发布流水线中加入配置基线校验，低于基线直接阻断发布",
        }
        return {"postmortem.json": json.dumps(data, ensure_ascii=False, indent=2)}

    return {}

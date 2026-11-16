"""Self-Loop：失败归因 + 差异化修复策略 + 经验沉淀。

区别于「报错就无脑重试」：先把失败归类，再决定修法，
并把修复经验写进经验库，同类问题下次直接命中。
"""
from __future__ import annotations

from . import db

LOGIC_DEFECT = "LOGIC_DEFECT"
OUTPUT_INVALID = "OUTPUT_INVALID"
SPEC_GAP = "SPEC_GAP"
QUALITY_GAP = "QUALITY_GAP"
TOOL_TIMEOUT = "TOOL_TIMEOUT"
ROOT_CAUSE_MISS = "ROOT_CAUSE_MISS"
NONE = "NONE"

ATTRIBUTION_META = {
    LOGIC_DEFECT: {
        "label": "逻辑缺陷",
        "strategy": "失败用例驱动修复",
        "switch_agent": True,
        "patch": "上一版实现未通过断言。请逐条对照失败用例，定位边界条件与精度处理问题，"
                 "只修改致错逻辑，不要重写整体结构，并保证空输入、非法输入不抛异常。",
    },
    OUTPUT_INVALID: {
        "label": "产出不可用",
        "strategy": "重新生成并强制格式约束",
        "switch_agent": True,
        "patch": "上一版产出无法解析或无法导入。必须输出完整、可执行、无语法错误的代码，"
                 "文件名与接口保持不变，禁止输出解释性文字包裹代码。",
    },
    SPEC_GAP: {
        "label": "需求覆盖不足",
        "strategy": "补充知识检索后重做",
        "switch_agent": False,
        "patch": "产出未覆盖全部验收条目。请结合检索到的口径文档补齐缺失场景，"
                 "逐条自检每个验收标准是否都有对应实现。",
    },
    QUALITY_GAP: {
        "label": "质量不达标",
        "strategy": "按评审 rubric 重写",
        "switch_agent": True,
        "patch": "测试虽通过但质量评分不足。请补充文档字符串、显式异常处理，"
                 "并统一数值精度与命名规范。",
    },
    TOOL_TIMEOUT: {
        "label": "执行超时",
        "strategy": "缩小执行范围后重试",
        "switch_agent": False,
        "patch": "上一轮执行超时。请精简实现，避免大规模循环与重复 IO，确保在时限内完成。",
    },
    ROOT_CAUSE_MISS: {
        "label": "根因未命中",
        "strategy": "回溯证据链重新定位",
        "switch_agent": True,
        "patch": "处置执行后健康检查仍然失败，说明当前处置只处理了表象，没有命中根因。"
                 "请回到诊断证据，逐项核对连接池上限、超时阈值、副本数是否都满足基线，"
                 "禁止只调整单一参数当作修复。",
    },
}


def attribute(verdict: dict) -> dict:
    """依据验证结果做失败归因。"""
    if verdict.get("passed"):
        return {"code": NONE, **ATTRIBUTION_META.get(NONE, {"label": "通过", "strategy": "-", "patch": ""}),
                "switch_agent": False}

    detail = verdict.get("detail", {}) or {}
    l1 = detail.get("l1", {}) or {}
    reason_text = " ".join(str(x) for x in (verdict.get("reasons") or [])).lower()

    if l1.get("returncode") == 124:
        code = TOOL_TIMEOUT
    elif any("健康检查" in str(r) or "根因" in str(r) for r in (verdict.get("reasons") or [])):
        code = ROOT_CAUSE_MISS
    elif not l1.get("ok") and l1.get("returncode") not in (0, None) and not l1.get("failures"):
        code = OUTPUT_INVALID
    elif "syntaxerror" in reason_text or "importerror" in reason_text or "modulenotfound" in reason_text:
        code = OUTPUT_INVALID
    elif l1.get("failures"):
        code = LOGIC_DEFECT
    elif detail.get("l3", {}).get("coverage", 1.0) < 0.75:
        code = SPEC_GAP
    elif detail.get("l2", {}).get("score", 100) < 60:
        code = QUALITY_GAP
    else:
        code = LOGIC_DEFECT

    meta = ATTRIBUTION_META[code]
    return {"code": code, **meta}


def feedback_patch(attribution: dict, verdict: dict, limit: int = 3500) -> str:
    """Combine the repair strategy with bounded, concrete verifier evidence."""
    detail = verdict.get("detail", {}) or {}
    l1 = detail.get("l1", {}) or {}
    l3 = detail.get("l3", {}) or {}
    sections = [str(attribution.get("patch", "")).strip()]
    reasons = [str(item) for item in verdict.get("reasons", []) if item]
    if reasons:
        sections.append("验证结论：" + "；".join(reasons[:5]))
    failures = [str(item) for item in l1.get("failures", []) if item]
    browser_results = (l1.get("browser") or {}).get("case_results") or []
    browser_failures = [item["detail"] for item in browser_results
                        if item.get("status") == "failed" and item.get("detail")]
    if browser_failures:
        failures = browser_failures
    if failures:
        sections.append("失败用例：\n- " + "\n- ".join(failures[:10]))
    stderr = str(l1.get("stderr") or "").strip()
    if stderr:
        sections.append("真实测试错误（末尾）：\n" + stderr[-2400:])
    gaps = [str(item) for item in l3.get("gaps", []) if item]
    if gaps:
        sections.append("未覆盖验收项：" + "；".join(gaps[:10]))
    return "\n\n".join(section for section in sections if section)[:limit]


def record_lesson(run_id: str, node_id: str, attribution: dict) -> dict:
    """同一类失败在同一节点反复出现时，累加命中次数，形成可复用经验。"""
    signature = f"{node_id}:{attribution['code']}"
    existing = db.one("SELECT * FROM lessons WHERE signature=? ORDER BY id DESC LIMIT 1", (signature,))
    if existing:
        db.execute("UPDATE lessons SET hits=hits+1 WHERE id=?", (existing["id"],))
        return {**existing, "hits": existing["hits"] + 1}
    db.execute(
        "INSERT INTO lessons(run_id,signature,strategy,patch,hits,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, signature, attribution.get("strategy", ""), attribution.get("patch", ""), 1, db.now()),
    )
    return db.one("SELECT * FROM lessons WHERE signature=? ORDER BY id DESC LIMIT 1", (signature,))


def known_lessons() -> list[dict]:
    return db.query("SELECT * FROM lessons ORDER BY hits DESC, id DESC LIMIT 50")

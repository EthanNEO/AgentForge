"""Validated planning boundary for scenario-backed and future model plans."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .llm import GatewayError, llm
from .models import ExecutionPlan, PlanNode


class PlanError(ValueError):
    pass


def create_plan(task: str, scenario: dict, mode: str) -> ExecutionPlan:
    nodes = []
    for raw in scenario.get("nodes", []):
        known = {"id", "name", "role", "deps", "outputs", "validator", "max_attempts", "desc"}
        nodes.append(PlanNode(
            id=str(raw["id"]), name=str(raw["name"]), role=str(raw["role"]),
            deps=tuple(raw.get("deps", [])), outputs=tuple(raw.get("outputs", [])),
            validator=str(raw.get("validator", "generic")),
            max_attempts=int(raw.get("max_attempts", 1)), desc=str(raw.get("desc", "")),
            config={k: v for k, v in raw.items() if k not in known},
        ))
    plan = ExecutionPlan(
        scenario_id=str(scenario.get("id", "unknown")), task=task, mode=mode, nodes=nodes,
        provenance=scenario.get("provenance", "model" if scenario["id"] == "custom" else "replay"),
        planning_model=scenario.get("planning_model", {}),
        clarification=str(scenario.get("clarification", "")),
        complexity=dict(scenario.get("complexity", {})),
    )
    validate_plan(plan)
    return plan


def validate_plan(plan: ExecutionPlan) -> None:
    if not plan.nodes:
        raise PlanError("执行计划不能为空")
    ids = [node.id for node in plan.nodes]
    if len(set(ids)) != len(ids):
        raise PlanError("执行计划包含重复节点 ID")
    known = set(ids)
    for node in plan.nodes:
        if not node.id or node.max_attempts < 1:
            raise PlanError(f"节点配置无效：{node.id or '<empty>'}")
        missing = set(node.deps) - known
        if missing:
            raise PlanError(f"节点 {node.id} 引用了未知依赖：{sorted(missing)}")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {node.id: node for node in plan.nodes}

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise PlanError(f"执行计划存在环：{node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dep in by_id[node_id].deps:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)

def plan_custom(task: str, attachments: list, provider: str | None = None) -> dict:
    """Turn model suggestions into a bounded, executable graph.

    provider 钉住规划模型（评测用）：两次尝试都用该模型，不做跨模型回退。
    """
    feedback = ""
    backup = None
    if provider is None and "qwen" in llm.providers and llm.available("qwen"):
        backup = next((name for name in dict.fromkeys((llm.default_provider, "deepseek", "mimo"))
                       if name != "qwen" and llm.available(name)), None)
    for attempt in range(2):
        try:
            pinned = provider if provider else (backup if attempt else None)
            return _plan_custom_once(task, attachments, feedback, pinned)
        except GatewayError as exc:
            raise ValueError(f"自由任务需要可用的在线模型：{exc}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, json.JSONDecodeError):
                feedback = f"返回不是完整 JSON（第 {exc.lineno} 行）：{exc.msg}"
            elif isinstance(exc, KeyError):
                feedback = f"缺少字段 {exc}"
            else:
                feedback = str(exc) or "节点或文件不符合格式要求"
    return _fallback_plan(task)


def _fallback_plan(task: str) -> dict:
    files, needs_page = _delivery_files(task)
    if not 1 <= len(files) <= 4:
        raise PlanError("请将交付文件分成多个任务")
    acceptance = ["逐项核对原始任务要求和用户给出的示例"]
    if needs_page:
        acceptance.append("在浏览器打开页面并核对实际结果。若任务要求交互，操作真实输入和演示。")
    return {"id": "custom", "name": "简化计划", "provenance": "deterministic", "nodes": [{
        "id": "build", "name": "制作交付物", "role": "coder", "deps": [],
        "desc": "根据原始需求制作交付物。自动规划未通过校验，业务要求需人工核对。",
        "outputs": files, "acceptance": acceptance, "validator": "deliverable",
        "levels": ["l1"], "max_attempts": 2,
    }]}


AA_REMAINDER_RULE = "每笔费用按整数分平摊，剩余分按参与名单顺序各补 1 分。"

SIGNUP_IDENTITY_RULE = (
    "按姓名识别同一人：同名记录合并并保留最后一次填写的信息；"
    "手机号不同不影响合并，不同姓名即使手机号相同也不合并。"
)

# 平台独立准备的假名单：覆盖重复填写换号、最后一次留空、从未留号、同号不同名。
SIGNUP_ROSTER = [
    {"name": "张三", "phone": "13800000001"},
    {"name": "李四", "phone": "13700000002"},
    {"name": "张三", "phone": "13900000003"},
    {"name": "王五", "phone": ""},
    {"name": "李四", "phone": ""},
    {"name": "赵六", "phone": "13600000004"},
    {"name": "孙七", "phone": "13600000004"},
]

SIGNUP_CONTROL_CONTRACT = (
    "控件约定：name-input、phone-input、add-record-button 录入单条记录；"
    "record-list 显示当前原始记录；页面初始记录列表为空；"
    "demo-button 载入平台假名单并替换当前记录；process-button 整理当前记录列表。"
    "整理后：result-output 显示「可确认报名 N 人」；missing-phone-list 用、分隔列出缺手机号姓名；"
    "merged-list 用「姓名 手机号」格式、；分隔列出确认人员；before-after 显示「整理前 X 条，整理后 Y 人」。"
)


def _is_signup_task(task: str) -> bool:
    return "报名" in task and "手机号" in task and bool(re.search(r"名单|重复|合并", task))


SIGNUP_IDENTITY_CUES = (
    r"(?:按|以|用|根据)手机号(?:来)?(?:识别|去重|合并|判断|区分)",
    r"手机号(?:相同|一样)[^。；;]{0,12}(?:同一人|合并|去重|算一个人)",
    r"同名[^。；;]{0,8}不同人",
    r"(?:按|以|用|根据)(?:身份证|学号|工号|编号|证件号)号?(?:来)?(?:识别|去重|合并|判断|区分)",
)

SIGNUP_CLARIFICATION = (
    "任务似乎要求按姓名以外的字段（手机号/证件号等）识别同一人。"
    "平台独立核算目前仅支持「按姓名识别同一人、保留最后一次填写」。"
    "请确认：勾选「确认按姓名识别同一人」后继续，验收将按姓名规则核算；"
    "如确需其他识别字段，请修改任务描述，该情况将没有平台自动验收，需人工核对。"
)


def _signup_identity_question(task: str) -> str | None:
    """Ask before executing when the task hints at a non-name identity rule."""
    if "报名" not in task or not re.search(r"名单|重复|合并|去重", task):
        return None
    if any(re.search(cue, task) for cue in SIGNUP_IDENTITY_CUES):
        return SIGNUP_CLARIFICATION
    return None


def _signup_expected(records: list) -> dict:
    """Independently merge by name, keeping the last entry; never reuse an old phone."""
    merged: dict[str, str] = {}
    for row in records:
        merged[str(row["name"]).strip()] = str(row["phone"]).strip()
    confirmed = {name: phone for name, phone in merged.items() if phone}
    missing = [name for name, phone in merged.items() if not phone]
    return {"raw_count": len(records), "merged_count": len(merged),
            "confirmed": confirmed, "missing": missing}


def _ground_signup_checks(task: str, node: dict) -> bool:
    """Replace model-written signup acceptance with platform-computed expectations."""
    if not _is_signup_task(task) or "index.html" not in node.get("outputs", []):
        return False
    expected = _signup_expected(SIGNUP_ROSTER)
    confirmed_text = "；".join(f"{name} {phone}" for name, phone in expected["confirmed"].items())
    missing_text = "、".join(expected["missing"])
    count_text = f"可确认报名 {len(expected['confirmed'])} 人"
    compare_text = f"整理前 {expected['raw_count']} 条，整理后 {expected['merged_count']} 人"

    def demo_case(criterion: int, kind: str, target: str, value: str) -> dict:
        return {"criterion": criterion, "grounded": True, "actions": [
            {"kind": "click", "target": "demo-button"},
            {"kind": "click", "target": "process-button"},
            {"kind": kind, "target": target, "value": value},
        ]}

    def manual_case(criterion: int, name: str, phones: tuple[str, str],
                    kind: str, target: str, value: str, count: str) -> dict:
        actions = []
        for phone in phones:
            actions.extend([
                {"kind": "fill", "target": "name-input", "value": name},
                {"kind": "fill", "target": "phone-input", "value": phone},
                {"kind": "click", "target": "add-record-button"},
            ])
        actions.append({"kind": "click", "target": "process-button"})
        actions.append({"kind": kind, "target": target, "value": value})
        actions.append({"kind": "expect_text", "target": "result-output", "value": count})
        return {"criterion": criterion, "grounded": True, "actions": actions}

    acceptance = [
        "页面提供姓名、手机号输入框和添加、演示、整理按钮。" + SIGNUP_IDENTITY_RULE,
        f"点击演示按钮载入平台假名单（{expected['raw_count']} 条记录），点击整理后对比区显示：{compare_text}。",
        f"整理后结果区显示：{count_text}。缺手机号者不计入确认人数。",
        f"缺手机号名单列出：{missing_text}。李四最后一次填写未留手机号，不沿用之前的号码。",
        f"整理后确认名单保留每人最后一次填写的手机号：{confirmed_text}。",
        "手动录入测试甲（13511110000）和测试甲（13522220000）两条后整理，"
        "确认名单仅保留：测试甲 13522220000，可确认报名 1 人。",
        "手动录入测试乙（13533330000）和测试乙（手机号留空）两条后整理，"
        "测试乙进入缺手机号名单，可确认报名 0 人。",
    ]
    cases = [
        demo_case(2, "expect_text", "before-after", compare_text),
        demo_case(3, "expect_text", "result-output", count_text),
        demo_case(4, "expect_names", "missing-phone-list", missing_text),
        demo_case(5, "expect_roster", "merged-list", confirmed_text),
        manual_case(6, "测试甲", ("13511110000", "13522220000"),
                    "expect_roster", "merged-list", "测试甲 13522220000", "可确认报名 1 人"),
        manual_case(7, "测试乙", ("13533330000", ""),
                    "expect_names", "missing-phone-list", "测试乙", "可确认报名 0 人"),
    ]
    node["acceptance"] = acceptance
    node["browser_cases"] = cases
    node["demo_roster"] = [dict(row) for row in SIGNUP_ROSTER]
    node["desc"] = f"{node.get('desc', '')} {SIGNUP_IDENTITY_RULE}".strip()[:1600]
    return True


MEETING_RULE = (
    "判定规则：同一房间且时间重叠大于 0 分钟才算冲突；首尾相接不算冲突；不同房间不算冲突。"
)

MEETING_CONTROL_CONTRACT = (
    "控件约定：room1、start1、end1 录入第一条预约的房间与起止时间（HH:MM），"
    "room2、start2、end2 录入第二条预约；check-btn 执行检查；result 显示检查结论。"
    + MEETING_RULE +
    "结果文本必须包含「冲突」或「无冲突」，冲突时须写明「重叠 N 分钟」。"
)

# 平台固定场景：部分重叠、首尾相接、不同房间、完全包含，预期全部独立计算。
MEETING_SCENARIOS = [
    {"name": "同房间部分重叠", "room1": "A", "start1": "09:00", "end1": "10:00",
     "room2": "A", "start2": "09:30", "end2": "10:30"},
    {"name": "首尾相接", "room1": "A", "start1": "09:00", "end1": "10:00",
     "room2": "A", "start2": "10:00", "end2": "11:00"},
    {"name": "不同房间同时段", "room1": "A", "start1": "09:00", "end1": "10:00",
     "room2": "B", "start2": "09:30", "end2": "10:30"},
    {"name": "同房间完全包含", "room1": "A", "start1": "09:00", "end1": "11:00",
     "room2": "A", "start2": "09:30", "end2": "10:00"},
]


def _is_meeting_task(task: str) -> bool:
    return "会议室" in task and "冲突" in task


def _meeting_expected(scenario: dict) -> str:
    def minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    if scenario["room1"].strip() != scenario["room2"].strip():
        return "无冲突"
    overlap = max(0, min(minutes(scenario["end1"]), minutes(scenario["end2"]))
                  - max(minutes(scenario["start1"]), minutes(scenario["start2"])))
    return f"重叠 {overlap} 分钟" if overlap > 0 else "无冲突"


def _meeting_user_example(task: str) -> dict | None:
    match = re.search(
        r"([A-Za-z0-9\u4e00-\u9fff]{1,12})房间\s*([0-2]\d:[0-5]\d)\s*(?:到|至|[-–—])\s*([0-2]\d:[0-5]\d)"
        r"\s*与\s*([0-2]\d:[0-5]\d)\s*(?:到|至|[-–—])\s*([0-2]\d:[0-5]\d)", task)
    if not match:
        return None
    room, start1, end1, start2, end2 = match.groups()
    return {"name": "用户示例", "room1": room, "start1": start1, "end1": end1,
            "room2": room, "start2": start2, "end2": end2}


def _ground_meeting_checks(task: str, node: dict) -> bool:
    """Own meeting-room acceptance with independently computed overlap expectations."""
    if not _is_meeting_task(task) or "index.html" not in node.get("outputs", []):
        return False
    scenarios = [dict(row) for row in MEETING_SCENARIOS]
    example = _meeting_user_example(task)
    if example and all(
        any(example[key] != row[key] for key in ("room1", "start1", "end1", "room2", "start2", "end2"))
        for row in scenarios
    ):
        scenarios.append(example)
    acceptance = ["页面提供两条预约的房间、开始时间、结束时间输入框和检查按钮。" + MEETING_RULE]
    cases = []
    for index, scenario in enumerate(scenarios, start=2):
        expected = _meeting_expected(scenario)
        label = "冲突，" + expected if expected != "无冲突" else "无冲突"
        acceptance.append(
            f"录入预约一 {scenario['room1']} {scenario['start1']}-{scenario['end1']}、"
            f"预约二 {scenario['room2']} {scenario['start2']}-{scenario['end2']}，"
            f"点击检查，结果应显示：{label}。（{scenario['name']}，平台独立核算）")
        actions = [
            {"kind": "fill", "target": target, "value": scenario[key]}
            for target, key in (("room1", "room1"), ("start1", "start1"), ("end1", "end1"),
                                ("room2", "room2"), ("start2", "start2"), ("end2", "end2"))
        ]
        actions.append({"kind": "click", "target": "check-btn"})
        actions.append({"kind": "expect_conflict", "target": "result", "value": expected})
        cases.append({"criterion": index, "grounded": True, "actions": actions})
    node["acceptance"] = acceptance
    node["browser_cases"] = cases
    node["meeting_scenarios"] = scenarios
    node["desc"] = f"{node.get('desc', '')} {MEETING_RULE}".strip()[:1600]
    return True


def _mentions_rounding(task: str) -> bool:
    return bool(re.search(r"舍入|取整|四舍五入|余分|尾差|余数|最后一分|一分钱|补[差分]|精确到分", task))


def _split_bill_expected(task: str, actions: list) -> str | None:
    """Verify the balances for recognizable AA browser inputs in integer cents."""
    if "AA" not in task.upper() and "平摊" not in task:
        return
    fields = {}
    net = defaultdict(int)
    for action in actions[:-1]:
        if action["kind"] == "fill":
            fields[action["target"]] = action["value"]
        if action["kind"] != "click" or action["target"] != "add-expense-button":
            continue
        payer = fields.get("payer-input", "").strip()
        amount = fields.get("amount-input", "")
        people = [part.strip() for part in re.split("[,，、]", fields.get("participants-input", ""))]
        fields.clear()
        if not payer or not all(people) or len(set(people)) != len(people):
            return
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", amount):
            return
        cents = int(Decimal(amount) * 100)
        share, remainder = divmod(cents, len(people))
        if remainder and _mentions_rounding(task):
            return
        net[payer] += cents
        for index, person in enumerate(people):
            net[person] -= share + (index < remainder)
    if not net or actions[-1]["target"] != "result-output":
        return
    debtors = [(name, -balance) for name, balance in net.items() if balance < 0]
    creditors = [(name, balance) for name, balance in net.items() if balance > 0]
    transfers = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        payer, debt = debtors[debtor_index]
        payee, credit = creditors[creditor_index]
        cents = min(debt, credit)
        amount = format(Decimal(cents) / 100, ".2f").rstrip("0").rstrip(".")
        transfers.append(f"{payer}转给{payee} {amount} 元")
        if debt == cents:
            debtor_index += 1
        else:
            debtors[debtor_index] = (payer, debt - cents)
        if credit == cents:
            creditor_index += 1
        else:
            creditors[creditor_index] = (payee, credit - cents)
    return "；".join(transfers) or "无需转账"


def _add_multi_expense_check(task: str, cases: list, acceptance: list) -> bool:
    if ("AA" not in task.upper() or not re.search(r"分别垫|多人垫|两笔|多笔", task)
            or len(acceptance) >= 8):
        return False
    if any(
        case["actions"][-1]["kind"] == "expect_transfers"
        and sum(action["kind"] == "click" and action["target"] == "add-expense-button"
                for action in case["actions"]) >= 2
        for case in cases
    ):
        return False
    actions = []
    expenses = [("小李", "300", "小李,小王,小张"), ("小王", "120", "小李,小王")]
    for payer, amount, people in expenses:
        actions.extend({"kind": "fill", "target": target, "value": value}
                       for target, value in zip(("payer-input", "amount-input", "participants-input"),
                                                (payer, amount, people)))
        actions.append({"kind": "click", "target": "add-expense-button"})
    calculation_buttons = {
        action["target"] for case in cases if case["actions"][-1]["kind"] == "expect_transfers"
        for action in case["actions"] if action["kind"] == "click"
        and action["target"] == "calculate-button"
    }
    if calculation_buttons:
        actions.append({"kind": "click", "target": "calculate-button"})
    result = {"kind": "expect_transfers", "target": "result-output"}
    actions.append(result)
    expected = _split_bill_expected(task, actions)
    if expected is None:
        return False
    result["value"] = expected
    acceptance.append("录入小李垫付 300 元（三人平摊），再录入小王垫付 120 元（仅小李、小王平摊）。"
                      f"结算应显示：{expected}")
    cases.append({"criterion": len(acceptance), "grounded": True, "actions": actions})
    return True


def _requested_output_files(task: str) -> set[str]:
    """Recognize explicit deliverable filenames without treating inputs as outputs."""
    matches = re.finditer(
        r'(?:输出|交付|保存为|导出为)\s*(?:名为\s*)?[`“"]?'
        r"([A-Za-z0-9_-]{1,60}\.(?:json|csv|txt|md|py))(?![A-Za-z0-9_.-])",
        task, re.I,
    )
    return {match[1].lower() for match in matches}


def _requested_output_formats(task: str) -> set[str]:
    formats = re.finditer(
        r"(?:输出(?:为)?|交付|保存为|导出(?:为)?|返回)\s*(JSON|CSV|Markdown|TXT)"
        r"(?=\s*(?:格式|文件|[，。；、,.!！]|$))",
        task, re.I,
    )
    suffixes = {"json": "json", "csv": "csv", "markdown": "md", "txt": "txt"}
    return {suffixes[match[1].lower()] for match in formats}


def _delivery_files(task: str) -> tuple[list[str], bool]:
    requested = _requested_output_files(task)
    formats = _requested_output_formats(task)
    interactive = bool(re.search(r"可操作|交互式|网页|页面", task))
    needs_page = interactive or not (requested or formats)
    example_files = sorted(requested) + [
        f"result.{suffix}" for suffix in sorted(formats)
        if not any(name.endswith("." + suffix) for name in requested)
    ]
    if needs_page:
        example_files.append("index.html")
    return example_files, needs_page


COMPLEXITY_CUES = (
    ("测试", r"测试|用例"),
    ("评审", r"评审|复核|审查|审核"),
    ("风险", r"风险"),
    ("需求分析", r"需求分析|分析需求|先分析|需求拆解"),
    ("设计文档", r"设计文档|技术方案|架构设计"),
)


def _complexity_cues(task: str) -> list[str]:
    return [label for label, pattern in COMPLEXITY_CUES if re.search(pattern, task)]


def _is_platform_simple_task(task: str) -> bool:
    """平台已接管核算的单一工具任务：保持单节点，避免拆散固定控件契约。"""
    if _is_signup_task(task) or _is_meeting_task(task):
        return True
    if "AA" in task.upper() or "平摊" in task or "分摊" in task:
        return True
    if all(word in task for word in ("按城市", "负数", "无法解析", "两位小数")):
        return True
    return False


def _is_complex_task(task: str) -> bool:
    """复杂任务 = 多角色线索并存，或明确分阶段且带角色线索；平台简单任务除外。"""
    if _is_platform_simple_task(task):
        return False
    cues = _complexity_cues(task)
    if len(cues) >= 2:
        return True
    staged = bool(re.search(r"先[^。；;]{2,60}(?:再|然后|接着|最后)", task))
    return staged and len(cues) >= 1


def _expand_complex_plan(task: str, nodes: list) -> list:
    """模型对复杂任务只给单节点时，平台补齐 analyst→coder→tester→reviewer 协作链。

    原节点（含平台核算用例）保持为 coder 实现节点，验收与浏览器用例不动；
    新增节点各自产出独立文档，形成真实的产物交接。
    """
    build = nodes[0]
    taken_ids = {build["id"]}
    taken_files = {name.lower() for name in build["outputs"]}

    def fresh_id(base: str) -> str:
        candidate, index = base, 2
        while candidate in taken_ids:
            candidate, index = f"{base}{index}", index + 1
        taken_ids.add(candidate)
        return candidate

    def fresh_file(base: str) -> str:
        candidate, index = base, 2
        while candidate.lower() in taken_files:
            stem, suffix = base.rsplit(".", 1)
            candidate, index = f"{stem}{index}.{suffix}", index + 1
        taken_files.add(candidate.lower())
        return candidate

    analyze_id = fresh_id("analyze")
    test_id = fresh_id("test")
    review_id = fresh_id("review")
    analysis_file = fresh_file("analysis.md")
    test_file = fresh_file("test_report.md")
    review_file = fresh_file("review.md")
    build["deps"] = [analyze_id] + [dep for dep in build["deps"] if dep != analyze_id]
    build["desc"] = (build.get("desc", "") + " 依据上游需求分析文档实现交付。"
                     "页面加载时内置验收用例所需的示例数据，用例断言与控件标识必须和页面初始状态一致。").strip()[:1600]
    analyze = {
        "id": analyze_id, "name": "需求分析", "role": "analyst", "deps": [],
        "desc": "拆解任务目标、关键规则与验收要点，形成实现依据。",
        "outputs": [analysis_file],
        "acceptance": ["分析文档覆盖任务目标、关键业务规则与验收要点，并给出实现建议"],
        "browser_cases": [], "validator": "deliverable", "levels": ["l1"], "max_attempts": 2,
    }
    test = {
        "id": test_id, "name": "测试验证", "role": "tester", "deps": [build["id"]],
        "desc": "逐条对照已确认的验收要求核对交付物，记录真实核对结果与证据。",
        "outputs": [test_file],
        "acceptance": ["测试报告逐条列出验收要求与核对结论，未通过项给出具体证据"],
        "browser_cases": [], "validator": "deliverable", "levels": ["l1"], "max_attempts": 2,
    }
    review = {
        "id": review_id, "name": "交付评审", "role": "reviewer", "deps": [build["id"], test_id],
        "desc": "独立复核交付物与测试报告，给出交付结论与剩余风险。",
        "outputs": [review_file],
        "acceptance": ["评审结论明确（通过/不通过），逐条给出依据并列出剩余风险"],
        "browser_cases": [], "validator": "deliverable", "levels": ["l1"], "max_attempts": 2,
    }
    return [analyze, build, test, review]


def _plan_custom_once(task: str, attachments: list, feedback: str, provider: str | None = None) -> dict:
    requested = _requested_output_files(task)
    formats = _requested_output_formats(task)
    example_files, needs_page = _delivery_files(task)
    complex_task = _is_complex_task(task)
    cues = _complexity_cues(task)
    if complex_task:
        example = json.dumps([
            {"id": "analyze", "name": "需求分析", "desc": "拆解目标、规则与验收要点",
             "role": "analyst", "deps": [], "outputs": ["analysis.md"],
             "acceptance": ["分析覆盖任务目标、关键规则与验收要点"]},
            {"id": "build", "name": "实现交付", "desc": "按分析实现可操作页面",
             "role": "coder", "deps": ["analyze"], "outputs": example_files,
             "acceptance": ["根据任务给出的输入和预期核对交付结果"]},
            {"id": "test", "name": "测试验证", "desc": "逐条核对验收要求并记录证据",
             "role": "tester", "deps": ["build"], "outputs": ["test_report.md"],
             "acceptance": ["测试报告逐条给出核对结论与证据"]},
            {"id": "review", "name": "交付评审", "desc": "独立复核并给出结论与风险",
             "role": "reviewer", "deps": ["build", "test"], "outputs": ["review.md"],
             "acceptance": ["评审结论明确并逐条给出依据"]},
        ], ensure_ascii=False)
        complexity_guidance = (
            "复杂度判定：本任务命中复杂线索（" + "、".join(cues) + "），必须拆成多节点 DAG 协作："
            "analyst 先产出分析文档，coder 依据分析实现交付，tester 依据验收要求产出测试报告，reviewer 独立复核产出评审结论。"
            "节点间用 deps 声明依赖，各节点产出互不重复的文件；浏览器用例只放在产出 index.html 的节点。"
            "浏览器用例必须与页面初始状态一致：用例断言的示例数据必须在页面加载时内置呈现，"
            "用例点击的 data-testid 控件必须真实存在；实现节点要把示例数据内置逻辑写进 desc。"
            "不要把全部工作塞进一个节点。"
        )
    else:
        example = json.dumps([{
            "id": "build", "name": "制作交付物", "desc": "按任务要求制作",
            "role": "coder", "deps": [], "outputs": example_files,
            "acceptance": ["根据任务给出的输入和预期核对交付结果"],
        }], ensure_ascii=False)
        complexity_guidance = (
            "复杂度判定：本任务目标单一，保持最短计划，通常一个节点即可；不要强加分析/测试/评审流程。"
        )
    response = llm.complete(
        "为用户任务按复杂度选择最短充分计划：简单任务用最少节点完成，复杂任务必须拆分多角色协作。只输出 JSON 数组，1 到 5 个节点，"
        "每节点含 id,name,desc,role,deps,outputs,acceptance。id 用英文小写，deps 为依赖 ID 数组。"
        "role 仅用 analyst/coder/tester/reviewer。outputs 是简单文件名数组，只允许英文字母、数字、下划线和连字符，后缀限 html、md、txt、json、py、csv、js、css。示例数据可内置于页面。"
        + complexity_guidance +
        "desc 只描述步骤。acceptance 是 1 到 8 条可核对的验收要求，每条给出操作和预期。原样保留用户明确给出的样例，不增加无关硬性要求。用户明确指定交付文件名或格式时必须按要求产出。纯文件交付无需网页，可操作工具仍须有 index.html；其余任务必须有一个节点产出 index.html，"
        "它是可独立打开的最终交付，内联 CSS 和 JS，不依赖网络。工具类任务必须可输入和操作；"
        "报告类直接呈现结果，并为用户提供的原始数据、约束和可核对结论分别生成浏览器断言。按城市汇总 CSV 且排除负数和无法解析金额时，为城市汇总和排除记录数分别建用例，target 分别用 city-summary 和 excluded-count，value 填待核算。静态报告用例可只包含 expect_text，target 指向报告中实际可见的对应字段。index.html 节点可带 browser_cases，每组含 criterion（验收序号，从1开始）和 actions。"
        "操作含 kind 和 target（裸 data-testid 值，如 amount-input，不能加 # 或选择器）；kind 可为 click、fill、expect_text，后两种还须有 value。"
        "AA 算账不要承诺最少转账笔数。AA 算账用 payer-input、amount-input、participants-input、add-expense-button、result-output 标识对应控件，参与人用逗号分隔，结果用 姓名转给姓名 金额 元 格式并用分号分隔。AA 用例最后一步的 value 可写待核算，平台会为可核算的示例填入正确结果。用户未指定舍入规则时，每笔费用按整数分平摊，剩余分按参与名单顺序各补 1 分。"
        "最后一步须用 expect_text 核对可见完整结果。优先覆盖用户明确提供的样例。按用户要求选择其余文件和步骤，不强加统一流程。格式示例（文件名与本任务交付要求对应）："
        + example
        + ("\n报名名单整理任务按姓名识别同一人：同名记录合并保留最后一次填写的信息，手机号不同不影响合并，"
           "不同姓名即使手机号相同也不合并。平台会提供固定假名单并独立核算预期、接管验收用例，"
           "不要套用 AA 转账格式。计划只需一个产出 index.html 的节点，desc 说明实现可录入、可演示、可整理的单页工具。"
           if _is_signup_task(task) else "")
        + ("\n会议室冲突检查任务用 room1、start1、end1、room2、start2、end2、check-btn、result 标识对应控件，"
           "时间用 HH:MM。同一房间且重叠大于 0 分钟才算冲突，首尾相接和不同房间不算冲突。"
           "平台会独立核算重叠时长并接管验收用例，不要手算预期。计划只需一个产出 index.html 的节点。"
           if _is_meeting_task(task) else ""),
        task + "\n本任务交付文件示例：" + json.dumps(example_files, ensure_ascii=False)
        + ("\n此任务为纯文件交付，无需 index.html 和浏览器用例。" if not needs_page else "")
        + "\n附件信息：" + json.dumps(attachments, ensure_ascii=False)
        + (f"\n上一次计划被拒：{feedback}。请按格式修正，保留用户要求。" if feedback else ""),
        timeout=45, max_output_tokens=5000,
        **({"provider": provider} if provider else {}),
    )
    raw = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.text.strip()))
    if isinstance(raw, dict):
        raw = raw.get("nodes")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise ValueError("计划节点、验收条目或文件格式无效")
    nodes, outputs = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("计划节点、验收条目或文件格式无效")
        nid, name, desc, role = (row[k] for k in ("id", "name", "desc", "role"))
        deps, files = row["deps"], row["outputs"]
        acceptance = row["acceptance"]
        if not (isinstance(acceptance, list) and 1 <= len(acceptance) <= 8
                and all(isinstance(check, str) and 0 < len(check.strip()) <= 500
                        for check in acceptance)):
            raise ValueError("acceptance 必须是 1 到 8 条非空文本，每条不超过 500 字")
        if not (isinstance(nid, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", nid)
                and isinstance(name, str) and 0 < len(name) <= 80
                and isinstance(desc, str) and 0 < len(desc) <= 1600
                and role in ("analyst", "coder", "tester", "reviewer")
                and isinstance(deps, list) and all(isinstance(dep, str) for dep in deps)
                and isinstance(files, list) and 1 <= len(files) <= 4):
            raise ValueError("计划节点、验收条目或文件格式无效")
        for file in files:
            if not isinstance(file, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}\.(html|md|txt|json|py|csv|js|css)", file) or file.lower() in outputs:
                raise ValueError("outputs 必须使用互不重复的英文文件名，后缀仅限 html/md/txt/json/py/csv/js/css")
            outputs.add(file.lower())
        cases = row.get("browser_cases", [])
        settlement_calculated = False
        if not isinstance(cases, list) or len(cases) > len(acceptance) or (cases and "index.html" not in files):
            raise ValueError("浏览器用例数量或归属无效")
        covered = set()
        for case in cases:
            criterion, actions = case["criterion"], case["actions"]
            if (type(criterion) is not int or not 1 <= criterion <= len(acceptance)
                    or criterion in covered or not isinstance(actions, list)
                    or not 1 <= len(actions) <= 12):
                raise ValueError("浏览器用例与验收条目不匹配")
            covered.add(criterion)
            for action in actions:
                kind, target = action["kind"], action["target"]
                if (kind not in ("click", "fill", "expect_text")
                        or not isinstance(target, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", target)):
                    raise ValueError(f"浏览器操作无效：kind={str(kind)[:24]!r}，target={str(target)[:64]!r}")
                if kind != "click" and (not isinstance(action.get("value"), str)
                                        or len(action["value"]) > 500
                                        or (kind == "expect_text" and not action["value"].strip())):
                    raise ValueError("浏览器操作值无效")
            if actions[-1]["kind"] != "expect_text":
                raise ValueError("浏览器用例需要结果断言")
            expected = _split_bill_expected(task, actions)
            if expected is not None:
                settlement_calculated = True
                case["grounded"] = True
                actions[-1]["value"] = expected
                actions[-1]["kind"] = "expect_transfers"
                entries = []
                for action in actions:
                    if action["kind"] == "fill":
                        label = {"payer-input": "付款人", "amount-input": "金额",
                                 "participants-input": "参与人"}.get(action["target"])
                        if label:
                            entries.append(f"{label}={action['value']}")
                    elif action["kind"] == "click" and action["target"] == "add-expense-button":
                        entries.append("添加支出")
                acceptance[criterion - 1] = "，".join(entries) + f"。结算应显示：{expected}"
        if settlement_calculated:
            _add_multi_expense_check(task, cases, acceptance)
        if settlement_calculated and not _mentions_rounding(task):
            desc = f"{desc} {AA_REMAINDER_RULE}"
        nodes.append(dict(id=nid, name=name, desc=desc, role=role, deps=deps,
                          outputs=files, browser_cases=cases, acceptance=[check.strip() for check in acceptance],
                          validator="deliverable", levels=["l1"], max_attempts=2))
    expanded = False
    if complex_task and len(nodes) == 1:
        nodes = _expand_complex_plan(task, nodes)
        expanded = True
    for node in nodes:
        _ground_signup_checks(task, node)
        _ground_meeting_checks(task, node)
    if requested - outputs:
        raise ValueError("缺少用户指定的交付文件：" + "、".join(sorted(requested - outputs)))
    missing_formats = formats - {name.rsplit(".", 1)[-1] for name in outputs}
    if missing_formats:
        raise ValueError("缺少用户指定的交付格式：" + "、".join(sorted(missing_formats)))
    if needs_page and not any("index.html" in node["outputs"] for node in nodes):
        raise ValueError("缺少 index.html 交付页面")
    scenario = {
        "id": "custom", "name": "自由任务", "nodes": nodes,
        "planning_model": {
            key: value for key in ("provider", "model")
            if (value := getattr(response, key, None))
        },
        "complexity": {
            "complex": complex_task,
            "cues": cues,
            "expanded": expanded,
            "reason": (
                ("命中复杂线索（" + "、".join(cues) + "），" if cues else "")
                + ("模型单节点计划由平台补齐 analyst→coder→tester→reviewer 协作链"
                   if expanded else "按多节点 DAG 协作执行")
                if complex_task else "目标单一，保持最短单节点计划"
            ),
        },
    }
    question = _signup_identity_question(task)
    if question:
        scenario["clarification"] = question
    create_plan(task, scenario, "live")
    return scenario

def ground_attachment_checks(task: str, scenario: dict, attachments: list) -> None:
    """Resolve supported CSV assertions locally. Unresolved placeholders stay manual."""
    files = [item for item in attachments if item["name"].lower().endswith(".csv")]
    expected = {}
    if len(files) == 1 and all(word in task for word in ("按城市", "负数", "无法解析", "两位小数")):
        path = files[0]["source_path"]
        try:
            totals = defaultdict(Decimal)
            counts = defaultdict(int)
            excluded = 0
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = csv.DictReader(source)
                if {"city", "amount"} <= set(rows.fieldnames or []):
                    for row in rows:
                        try:
                            amount = Decimal(row["amount"] or "")
                            valid = amount.is_finite() and amount >= 0
                        except InvalidOperation:
                            valid = False
                        if not valid:
                            excluded += 1
                            continue
                        city = row["city"] or ""
                        counts[city] += 1
                        totals[city] += amount
                    expected = {
                        "city-summary": "；".join(
                            f"{city} {counts[city]} 单 {totals[city]:.2f} 元" for city in sorted(counts)
                        ) or "无有效订单",
                        "excluded-count": f"排除 {excluded} 条",
                    }
        except (OSError, UnicodeError, csv.Error, InvalidOperation):
            pass

    for node in scenario["nodes"]:
        retained = []
        for case in node.get("browser_cases", []):
            actions = case["actions"]
            for action in actions:
                if (action["kind"] == "expect_text" and action["value"].strip() == "待核算"
                        and action["target"] in expected):
                    action["value"] = expected[action["target"]]
                    case["grounded"] = True
                    if action is actions[-1]:
                        node["acceptance"][case["criterion"] - 1] = "按附件核对：" + action["value"]
            if not any(action["kind"] == "expect_text" and action["value"].strip() == "待核算"
                       for action in actions):
                retained.append(case)
        node["browser_cases"] = retained


def ground_aa_demo_cases(task: str, node: dict) -> None:
    """为 AA 自愈演示保证平台核算用例：单笔 300 与多笔 300+120，预期全部独立计算。

    仅在规划未给出对应转账用例时补齐；已有用例不改动。控件契约与执行提示词一致。
    """
    acceptance = node.setdefault("acceptance", [])
    cases = node.setdefault("browser_cases", [])

    def add_count(case):
        return sum(action["kind"] == "click" and action["target"] == "add-expense-button"
                   for action in case["actions"])

    def settlement_case(case):
        return (case["actions"] and case["actions"][-1]["kind"] == "expect_transfers"
                and case["actions"][-1]["target"] == "result-output")

    has_single = any(settlement_case(case) and add_count(case) == 1 for case in cases)
    has_multi = any(settlement_case(case) and add_count(case) >= 2 for case in cases)

    def expense_actions(payer, amount, people):
        return [
            {"kind": "fill", "target": "payer-input", "value": payer},
            {"kind": "fill", "target": "amount-input", "value": amount},
            {"kind": "fill", "target": "participants-input", "value": people},
            {"kind": "click", "target": "add-expense-button", "value": ""},
        ]

    if not has_single:
        acceptance.append("核算单笔平摊：小李付 300 元、三人平摊，小王与小张各转给小李 100 元")
        actions = expense_actions("小李", "300", "小李,小王,小张") + [
            {"kind": "expect_transfers", "target": "result-output", "value": "待核算"},
        ]
        expected = _split_bill_expected(task, actions)
        actions[-1]["value"] = expected
        cases.append({"criterion": len(acceptance), "actions": actions, "grounded": True})
        acceptance[-1] = "按平台核算核对单笔转账：" + expected
    if not has_multi:
        acceptance.append("核算多笔平摊：小李 300（三人）+ 小王 120（小李小王），小王转小李 40、小张转小李 100")
        actions = (expense_actions("小李", "300", "小李,小王,小张")
                   + expense_actions("小王", "120", "小李,小王") + [
            {"kind": "expect_transfers", "target": "result-output", "value": "待核算"},
        ])
        expected = _split_bill_expected(task, actions)
        actions[-1]["value"] = expected
        cases.append({"criterion": len(acceptance), "actions": actions, "grounded": True})
        acceptance[-1] = "按平台核算核对多笔转账：" + expected

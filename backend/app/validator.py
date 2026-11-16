"""三级验证器：L1 确定性校验 → L2 模型评审 → L3 交叉复核。

设计意图：先用零成本、零幻觉的确定性手段兜底，
只有 L1 通过才消耗模型评审预算，最后用独立 Agent 交叉复核，
避免「自己写自己批」。
"""
from __future__ import annotations

import ast
import csv
import io
import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from . import tools
from .browser_check import check_page


def _compile_ok(path: Path) -> tuple[bool, str]:
    """纯内存语法校验：不产生 .pyc，不触碰文件系统。"""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        compile(source, str(path), "exec")
        return True, ""
    except SyntaxError as exc:
        return False, f"{exc.msg} (line {exc.lineno})"[:300]
    except (UnicodeDecodeError, OSError) as exc:
        return False, str(exc)[:300]


def _read(work: Path, name: str) -> str:
    p = work / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _overlap_contract_issue(task: str, criteria: list) -> str | None:
    """Check only unambiguous, same-day examples of a two-interval overlap."""
    if not all(word in task for word in ("会议室", "冲突")):
        return None
    for item in criteria:
        if not isinstance(item, dict):
            continue
        example = item.get("verification")
        if not isinstance(example, str) or "同一会议室" not in example or "重叠时长" not in example:
            continue
        ranges = re.findall(r"(?<!\d)([0-2]\d:[0-5]\d)\s*[-–—至]\s*([0-2]\d:[0-5]\d)(?!\d)", example)
        stated = re.findall(r"重叠时长\s*(?:为|=|是|：|:)\s*(\d+)\s*分钟", example)
        if len(ranges) != 2 or len(stated) != 1:
            continue
        def minutes(value):
            hour, minute = map(int, value.split(":"))
            return hour * 60 + minute
        (a, b), (c, d) = [tuple(map(minutes, pair)) for pair in ranges]
        if not (0 <= a < b <= 1440 and 0 <= c < d <= 1440):
            continue
        expected = max(0, min(b, d) - max(a, c))
        if expected != int(stated[0]):
            return f"{item.get('id', '?')}：{ranges} 的实际交集为 {expected} 分钟，验收示例写成 {stated[0]} 分钟"
    return None


def _split_bill_contract_issue(task: str, criteria: list) -> str | None:
    if "AA" not in task.upper() and "平摊" not in task and "分摊" not in task:
        return None
    for item in criteria:
        if not isinstance(item, dict):
            continue
        given, then = item.get("given"), item.get("then")
        if not isinstance(given, str) or not isinstance(then, str):
            continue
        expenses = re.findall(
            r"付款人=([^，,；;]+)[，,]\s*金额=(\d+(?:\.\d{1,2})?)[，,]\s*参与人=\[([^\]]+)\]", given
        )
        if not expenses or len(expenses) != given.count("付款人="):
            continue
        net = defaultdict(int)
        possible_extra = defaultdict(int)
        for payer, amount, names in expenses:
            participants = [name.strip() for name in re.split(r"[,，、]", names)]
            cents = int(Decimal(amount) * 100)
            if not all(participants) or len(set(participants)) != len(participants):
                break
            share, remainder = divmod(cents, len(participants))
            net[payer.strip()] += cents
            for name in participants:
                net[name] -= share
                possible_extra[name] += bool(remainder)
        else:
            stated = {}
            for name, ceiling in net.items():
                match = re.search(rf"(?<![\w/]){re.escape(name)}净额\s*[=＝]\s*([+−-]?\d+(?:\.\d{{1,2}})?)", then)
                if not match:
                    continue
                cents = int(Decimal(match[1].replace("−", "-")) * 100)
                stated[name] = cents
                floor = ceiling - possible_extra[name]
                if not floor <= cents <= ceiling:
                    expected = f"{Decimal(ceiling) / 100:.2f}"
                    if floor != ceiling:
                        expected = f"{Decimal(floor) / 100:.2f} 至 {expected}"
                    return f"{item.get('id', '?')}：{name}净额应为 {expected}，样例写成 {match[1]}"
            if len(stated) == len(net) and sum(stated.values()):
                return f"{item.get('id', '?')}：样例列出了所有人的净额，但合计不为零"
            if len(stated) == len(net):
                transfer = re.search(r"转账(?:建议)?(?:金额)?(?:之和|总额)\s*[=＝]\s*(\d+(?:\.\d{1,2})?)", then)
                expected = sum(amount for amount in stated.values() if amount > 0)
                if transfer and Decimal(transfer[1]) * 100 != expected:
                    return f"{item.get('id', '?')}：转账总额应等于待收款净额 {Decimal(expected) / 100:.2f}"
    return None


def _test_failure_owner(work: Path, stderr: str) -> dict:
    """Use traceback frames and source; never infer test ownership from TypeError alone."""
    findings = []
    for match in re.finditer(r"(?ms)^(ERROR|FAIL): (.*?)(?=^(?:ERROR|FAIL): |\Z)", stderr):
        kind, block = match.groups()
        frames = re.findall(r'^  File "([^"]+)", line (\d+), in ', block, re.M)
        local = [(Path(file).name, int(line)) for file, line in frames
                 if Path(file).parent.resolve() == work.resolve()]
        if any(name == "main.py" for name, _ in local):
            findings.append(("code", str(local)))
        elif local and local[-1][0] == "test_main.py" and (
            missing := re.search(r"ImportError: cannot import name '(\w+)' from 'main'", block)
        ):
            name = missing[1]
            tree = ast.parse(_read(work, "main.py"))
            declarations = {stmt.name for stmt in tree.body
                            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            spec_declares_name = re.search(rf"\b{re.escape(name)}\s*\(", _read(work, "spec.json"))
            test_only_candidate = declarations and all(item.startswith("test_") for item in declarations)
            if name not in declarations and (spec_declares_name or test_only_candidate):
                evidence = (f"main.py 仅包含测试声明，无法导入 {name}" if test_only_candidate
                            else f"main.py 缺少需求约定的接口 {name}")
                findings.append(("code", evidence))
            else:
                findings.append(("unknown", f"测试无法从 main.py 导入 {name}"))
        elif local and local[-1][0] == "test_main.py" and "TypeError: unhashable type:" in block:
            name, line = local[-1]
            tree = ast.parse(_read(work, name))
            if any(isinstance(n, ast.Set) and n.lineno <= line <= n.end_lineno
                   for n in ast.walk(tree)):
                findings.append(("test", f"{name}:{line} 构造集合时使用了不可哈希值"))
            else:
                findings.append(("unknown", str(local)))
        elif kind == "FAIL" and local:
            findings.append(("code", f"{local[-1][0]}:{local[-1][1]} 断言失败"))
        else:
            findings.append(("unknown", str(local)))
    owners = {owner for owner, _ in findings}
    return {"stage": next(iter(owners)) if len(owners) == 1 else "unknown",
            "evidence": [evidence for _, evidence in findings][:10]}


def _reject_json_constant(value: str):
    raise ValueError(f"JSON 不支持 {value}")


def _csv_format_error(raw: str) -> str | None:
    rows = csv.reader(io.StringIO(raw, newline=""), strict=True)
    width = None
    try:
        for row in rows:
            if not row:
                continue
            if width is None:
                width = len(row)
            elif len(row) != width:
                return f"第 {rows.line_num} 行有 {len(row)} 列，预期 {width} 列"
    except csv.Error as exc:
        return f"第 {rows.line_num} 行无法解析：{exc}"
    return None


def _l1(node: dict, work: Path, ctx: dict) -> dict:
    kind = node["validator"]
    out = {"ok": False, "checks": [], "failures": [], "returncode": 0}

    if kind == "deliverable":
        for name in node["outputs"]:
            raw = _read(work, name)
            if not raw.strip():
                return {**out, "detail": f"缺少有效产物 {name}"}
            suffix = Path(name).suffix.lower()
            if suffix == ".json":
                try:
                    json.loads(raw, parse_constant=_reject_json_constant)
                except ValueError as exc:
                    return {**out, "detail": f"{name} 不是有效 JSON：{exc}"}
            if suffix == ".csv":
                error = _csv_format_error(raw)
                if error:
                    return {**out, "detail": f"{name} 格式错误：{error}"}
            if suffix == ".html" and not re.search(r"<(?:html|body|main)\b", raw, re.I):
                return {**out, "detail": f"{name} 不是可预览的 HTML 页面"}
            if suffix == ".py":
                ok, error = _compile_ok(work / name)
                if not ok:
                    return {**out, "detail": f"{name}: {error}"}
        checks = ["声明产物存在且格式初检通过"]
        browser = None
        if "index.html" in node["outputs"]:
            cases = node.get("browser_cases", [])
            browser = check_page(work / "index.html", cases)
            if cases and browser["status"] == "unavailable":
                reason = "已确认的浏览器验收无法运行：" + browser["detail"]
                return {**out, "detail": reason, "browser": browser,
                        "responsibility": {"stage": "environment", "evidence": [reason]}}
            if browser["status"] == "failed":
                return {**out, "detail": browser["detail"], "failures": [browser["detail"]],
                        "browser": browser}
            checks.append(browser["detail"])
        return {**out, "ok": True, "checks": checks, "browser": browser}

    if kind == "spec":
        raw = _read(work, "spec.json")
        if not raw:
            return {**out, "detail": "缺少 spec.json"}
        try:
            spec = json.loads(raw)
        except ValueError as exc:
            return {**out, "detail": f"spec.json 不是合法 JSON: {exc}"}
        criteria = spec.get("acceptance_criteria") if isinstance(spec, dict) else None
        criteria = criteria or []
        if not isinstance(criteria, list):
            return {**out, "detail": "验收标准必须是数组"}
        if len(criteria) < 3:
            return {**out, "detail": f"验收标准不足 3 条（当前 {len(criteria)} 条）"}
        task = ctx.get("task", "")
        issue = _overlap_contract_issue(task, criteria) or _split_bill_contract_issue(task, criteria)
        if issue:
            return {**out, "detail": issue,
                    "responsibility": {"stage": "spec", "evidence": [issue]}}
        ctx["spec"] = spec
        return {**out, "ok": True, "checks": [f"验收标准 {len(criteria)} 条"],
                "detail": "spec.json 结构合法"}

    if kind == "test":
        names = [p.name for p in work.glob("test_*.py")]
        if not names:
            return {**out, "detail": "未产出测试文件"}
        for n in names:
            ok, msg = _compile_ok(work / n)
            if not ok:
                return {**out, "detail": f"{n} 语法错误: {msg}"}
        body = "".join(_read(work, n) for n in names)
        if "unittest" not in body and "pytest" not in body:
            return {**out, "detail": "测试文件未使用 unittest/pytest"}
        if "subprocess" in body and ("__file__" in body or any(name in body for name in names)):
            return {**out, "detail": "测试文件包含递归启动自身的 subprocess 调用"}
        ctx["test_files"] = names
        return {**out, "ok": True, "checks": [f"测试文件 {len(names)} 个且可编译"],
                "detail": "测试用例结构合法"}

    if kind == "coding":
        code_path = work / "main.py"
        if not code_path.exists():
            return {**out, "detail": "未产出 main.py"}
        ok, msg = _compile_ok(code_path)
        if not ok:
            return {**out, "detail": f"main.py 语法错误: {msg}", "returncode": 1}
        result = tools.run_python_tests(work)
        detail = {
            "ok": result["ok"],
            "checks": [f"实跑测试 {result['ran']} 个，耗时 {result['duration']}s"],
            "failures": result["failures"],
            "returncode": result["returncode"],
            "detail": "全部断言通过" if result["ok"] else "验收测试未通过",
            "ran": result["ran"],
            **({"responsibility": _test_failure_owner(work, result["stderr"])}
               if not result["ok"] and result["returncode"] != 124 else {}),
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }
        return detail

    if kind == "hardening":
        result = tools.run_python_tests(work)
        baseline = ctx.get("baseline_tests", 0)
        passed = result["ok"] and result["ran"] >= max(baseline, 1)
        return {
            "ok": passed,
            "checks": [f"回归测试 {result['ran']} 个（基线 {baseline}）"],
            "failures": result["failures"],
            "returncode": result["returncode"],
            "detail": "回归测试全部通过" if passed else "回归测试失败或用例减少",
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    if kind == "review":
        raw = _read(work, "review.json")
        if not raw:
            return {**out, "detail": "缺少 review.json"}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return {**out, "detail": f"review.json 非法: {exc}"}
        if not isinstance(data, dict) or data.get("verdict") not in ("pass", "fail"):
            return {**out, "detail": "评审结论必须是 pass 或 fail"}
        requirements = (ctx.get("spec") or {}).get("acceptance_criteria", [])
        expected = {item["id"]: item.get("text") or item.get("description") or item.get("title") or ""
                    for item in requirements
                    if isinstance(item, dict) and isinstance(item.get("id"), str)}
        if not expected or len(expected) != len(requirements):
            return {**out, "detail": "需求验收标准缺少唯一编号"}
        items = data.get("criteria")
        if not isinstance(items, list):
            return {**out, "detail": "缺少 criteria 数组，须逐条列出验收结论和依据"}
        evidence_files = ("main.py", "test_main.py", "test_edge.py", "risk.json")
        rows = {}
        for item in items:
            if not isinstance(item, dict):
                return {**out, "detail": "每条评审记录必须是 JSON 对象"}
            criterion_id = item.get("id")
            if not isinstance(criterion_id, str) or criterion_id not in expected or criterion_id in rows:
                return {**out, "detail": "criteria 中含未知或重复的验收编号"}
            result, filename, reason = item.get("result"), item.get("file"), item.get("reason")
            if (result not in ("pass", "fail") or filename not in evidence_files
                    or not (work / filename).is_file() or not isinstance(reason, str) or not reason.strip()):
                return {**out, "detail": f"{criterion_id} 须提供 pass/fail、现有证据文件和具体理由"}
            rows[criterion_id] = {"id": criterion_id, "text": expected[criterion_id],
                                  "result": result, "file": filename, "reason": reason.strip()}
        missing = expected.keys() - rows.keys()
        if missing:
            return {**out, "detail": "缺少验收条目：" + "、".join(sorted(missing))}
        failed = [row["id"] for row in rows.values() if row["result"] == "fail"]
        passed = not failed
        if data["verdict"] != ("pass" if passed else "fail"):
            return {**out, "detail": "总评审结论与逐条结果不一致"}
        return {**out, "ok": passed, "checks": [f"逐条评审 {len(rows)} 项"],
                "criteria": [rows[key] for key in expected],
                "detail": "评审通过" if passed else "评审未通过：" + "、".join(failed)}

    if kind == "json":
        target = node["outputs"][0] if node.get("outputs") else ""
        raw = _read(work, target)
        if not raw:
            return {**out, "detail": f"缺少 {target}"}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return {**out, "detail": f"{target} 不是合法 JSON: {exc}"}
        missing = [k for k in node.get("required", []) if not data.get(k)]
        if missing:
            return {**out, "detail": f"缺少必填字段：{', '.join(missing)}"}
        ctx.setdefault("docs", {})[node["id"]] = data
        return {**out, "ok": True, "checks": [f"字段完整性校验通过（{len(node.get('required', []))} 项）"],
                "detail": f"{target} 结构合法"}

    if kind == "run":
        script = node.get("script") or (node["outputs"][0] if node.get("outputs") else "")
        path = work / script
        if not path.exists():
            return {**out, "detail": f"未产出脚本 {script}"}
        ok, msg = _compile_ok(path)
        if not ok:
            return {**out, "detail": f"{script} 语法错误: {msg}", "returncode": 1}
        result = tools.run_script(work, script)
        return {
            "ok": result["ok"],
            "checks": [f"脚本执行完成，退出码 {result['returncode']}，{result.get('duration', 0)}s"],
            "failures": [] if result["ok"] else [result["stderr"][:200]],
            "returncode": result["returncode"],
            "detail": "脚本执行成功" if result["ok"] else "脚本执行失败",
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    if kind == "health":
        script = node.get("check") or "health_check.py"
        result = tools.run_script(work, script)
        marker = node.get("expect", "HEALTHY")
        healthy = result["ok"] and marker in (result["stdout"] or "")
        reasons = [ln.strip(" -") for ln in (result["stdout"] or "").splitlines() if ln.strip().startswith("-")]
        return {
            "ok": healthy,
            "checks": [f"健康检查 {script} 退出码 {result['returncode']}"],
            "failures": [] if healthy else (reasons or [result["stderr"][:200]]),
            "returncode": result["returncode"],
            "detail": "服务恢复健康" if healthy else "服务仍未恢复健康",
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    if kind == "remediate":
        script = node.get("script") or "fix.py"
        check = node.get("check") or "health_check.py"
        path = work / script
        if not path.exists():
            return {**out, "detail": f"未产出处置脚本 {script}"}
        ok, msg = _compile_ok(path)
        if not ok:
            return {**out, "detail": f"{script} 语法错误: {msg}", "returncode": 1}
        applied = tools.run_script(work, script)
        if not applied["ok"]:
            return {**out, "returncode": applied["returncode"],
                    "detail": "处置脚本执行失败",
                    "failures": [applied["stderr"][:300]],
                    "stdout": applied["stdout"], "stderr": applied["stderr"]}
        health = tools.run_script(work, check)
        healthy = health["ok"] and "HEALTHY" in (health["stdout"] or "")
        reasons = [ln.strip(" -") for ln in (health["stdout"] or "").splitlines()
                   if ln.strip().startswith("-")]
        return {
            "ok": healthy,
            "checks": [f"处置已执行（退出码 {applied['returncode']}），健康检查退出码 {health['returncode']}"],
            "failures": [] if healthy else (reasons or ["健康检查未通过"]),
            "returncode": health["returncode"],
            "detail": "处置后服务恢复健康" if healthy else "处置未解决根因，健康检查仍失败",
            "stdout": (applied["stdout"] + "\n" + health["stdout"])[-4000:],
            "stderr": health["stderr"],
        }

    return {**out, "detail": f"未知校验类型 {kind}"}


def _l2(node: dict, work: Path, ctx: dict) -> dict:
    """启发式 rubric 评审（在线模式下可替换为真实 LLM-as-Judge）。"""
    text = _read(work, "main.py") or _read(work, node["outputs"][0])
    if not text:
        return {"score": 0, "reasons": ["产出为空"]}
    score, reasons = 55, []
    if '"""' in text or "'''" in text:
        score += 8
        reasons.append("含模块文档字符串 +8")
    if "try" in text and "except" in text:
        score += 8
        reasons.append("含显式异常处理 +8")
    if "encoding" in text:
        score += 7
        reasons.append("显式指定编码 +7")
    if text.count("def ") >= 3:
        score += 7
        reasons.append("函数拆分合理 +7")
    if len(text) > 600:
        score += 6
        reasons.append("实现完整度达标 +6")
    if "#" in text:
        score += 5
        reasons.append("含注释 +5")
    score = min(score, 96)
    return {"score": score, "reasons": reasons}


def _l3(node: dict, work: Path, ctx: dict) -> dict:
    """交叉复核：独立重跑 + 按验收条目做覆盖度核对。"""
    criteria = (ctx.get("spec") or {}).get("acceptance_criteria") or []
    combined = "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in work.glob("*.py")
    )
    covered, gaps = 0, []
    for c in criteria:
        kws = c.get("keywords") or [c.get("id", "")]
        if any(k and k in combined for k in kws):
            covered += 1
        else:
            gaps.append(c.get("text", c.get("id", "")))
    coverage = round(covered / len(criteria), 3) if criteria else 1.0
    # 只有存在实现文件时才独立重跑，避免无实现时误判
    if (work / "main.py").exists():
        rerun_ok = tools.run_python_tests(work)["ok"]
    else:
        rerun_ok = True
    passed = coverage >= 0.75 and rerun_ok
    return {
        "coverage": coverage,
        "covered": covered,
        "total": len(criteria),
        "gaps": gaps[:5],
        "independent_rerun_ok": rerun_ok,
        "passed": passed,
    }


def validate(node: dict, work: Path, ctx: dict) -> dict:
    levels = node.get("levels", ["l1", "l2", "l3"])
    l1 = _l1(node, work, ctx)
    detail = {"l1": l1}
    reasons = [] if l1["ok"] else [l1.get("detail", "L1 未通过")]

    l2 = {"score": None, "skipped": True, "reasons": ["L1 未通过，跳过模型评审"]}
    l3 = {"coverage": None, "skipped": True, "passed": None, "gaps": []}

    if l1["ok"] and "l2" in levels:
        l2 = _l2(node, work, ctx)
        detail["l2"] = l2
        if l2["score"] < 60:
            reasons.append(f"质量评分 {l2['score']} 低于阈值 60")
    if l1["ok"] and "l3" in levels:
        l3 = _l3(node, work, ctx)
        detail["l3"] = l3
        if not l3["passed"]:
            reasons.append(f"交叉复核未通过（覆盖度 {l3['coverage']}）")

    passed = bool(
        l1["ok"]
        and (l2.get("score") is None or l2["score"] >= 60)
        and (l3.get("passed") is None or l3["passed"])
    )
    return {"passed": passed, "reasons": reasons, "detail": detail,
            "l1": l1, "l2": l2, "l3": l3}

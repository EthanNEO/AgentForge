"""Run generated pages with the same restrictions as the delivery preview."""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import re

PREVIEW_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; form-action 'none'; base-uri 'none'"
)


def _transfer_rows(text: str) -> Counter | None:
    rows = Counter()
    for line in re.split(r"[；;\n]+", text.strip()):
        match = re.fullmatch(r"(.+?)转给(.+?)\s+([0-9]+(?:\.[0-9]{1,2})?)\s*元", line.strip())
        if not match:
            return None
        payer, payee, amount = match.groups()
        rows[payer.strip(), payee.strip(), Decimal(amount)] += 1
    return rows


def _name_set(text: str) -> set[str]:
    return {part for part in re.split(r"[、，,；;\s]+", text.strip()) if part}


def _roster_rows(text: str) -> Counter | None:
    rows = Counter()
    for line in re.split(r"[；;\n]+", text.strip()):
        line = line.strip().replace("：", " ").replace(":", " ")
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        rows[(parts[0], parts[1])] += 1
    return rows


def _conflict_facts(text: str) -> tuple[str, int] | None:
    """Extract (conflict?, overlap minutes) from a meeting-room verdict, wording-tolerant."""
    text = text.strip()
    if "无冲突" in text:
        return ("none", 0)
    match = re.search(r"重叠\s*(\d+)\s*分钟", text)
    if match:
        return ("conflict", int(match.group(1)))
    return None


def check_browser_environment() -> dict:
    return check_page(None)


def check_page(path: Path | None, cases=()) -> dict:
    try:
        from playwright.sync_api import Error, expect, sync_playwright
    except ImportError:
        return {"status": "unavailable", "detail": "未安装浏览器验收组件"}
    url = "http://agentforge-preview.invalid/index.html"
    errors = []
    verified = []
    case_results = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="msedge", timeout=8000)
            except Error:
                try:
                    browser = playwright.chromium.launch(timeout=8000)
                except Error:
                    return {"status": "unavailable", "detail": "没有可启动的 Edge 或 Chromium"}
            try:
                page = browser.new_page(service_workers="block", accept_downloads=False)
                if path is None:
                    return {"status": "passed", "detail": "浏览器验收环境可用"}
                page.on("pageerror", lambda error: errors.append(str(error)[:300]))

                def intercept(route):
                    if route.request.url == url and route.request.is_navigation_request():
                        route.fulfill(path=path, headers={
                            "Content-Type": "text/html; charset=utf-8",
                            "Content-Security-Policy": PREVIEW_CSP,
                        })
                    else:
                        route.abort()

                page.route("**/*", intercept)
                page.goto(url, wait_until="load", timeout=5000)
                if not page.locator("body").inner_text(timeout=2500).strip():
                    errors.append("页面没有可读内容")
                for case in (() if errors else cases):
                    error_start = len(errors)
                    try:
                        page.goto(url, wait_until="load", timeout=5000)
                        for action in case["actions"]:
                            target = page.get_by_test_id(action["target"])
                            if action["kind"] == "fill":
                                target.fill(action["value"], timeout=1500)
                            elif action["kind"] == "click":
                                target.click(timeout=1500)
                            else:
                                expect(target).to_be_visible(timeout=1500)
                                expected = action["value"]
                                if action["kind"] == "expect_transfers" and _transfer_rows(expected):
                                    actual = target.inner_text(timeout=1500)
                                    if _transfer_rows(actual) != _transfer_rows(expected):
                                        raise AssertionError(f"预期转账 {expected}，实际 {actual}")
                                elif action["kind"] == "expect_names":
                                    actual = target.inner_text(timeout=1500)
                                    if _name_set(actual) != _name_set(expected):
                                        raise AssertionError(f"预期名单 {expected}，实际 {actual}")
                                elif action["kind"] == "expect_roster":
                                    actual = target.inner_text(timeout=1500)
                                    if _roster_rows(expected) is None or _roster_rows(actual) != _roster_rows(expected):
                                        raise AssertionError(f"预期名单记录 {expected}，实际 {actual}")
                                elif action["kind"] == "expect_conflict":
                                    actual = target.inner_text(timeout=1500)
                                    if _conflict_facts(actual) is None or _conflict_facts(actual) != _conflict_facts(expected):
                                        raise AssertionError(f"预期冲突结论 {expected}，实际 {actual}")
                                else:
                                    expect(target).to_have_text(expected, timeout=1500)
                    except (Error, AssertionError) as exc:
                        errors.append(str(exc).strip()[:400])
                    case_errors = errors[error_start:]
                    criterion = case["criterion"]
                    if case_errors:
                        detail = f"验收第 {criterion} 条未通过：" + "；".join(case_errors[:2])
                        errors[error_start:] = [detail]
                        case_results.append({"criterion": criterion, "status": "failed", "detail": detail})
                    else:
                        verified.append(criterion)
                        case_results.append({"criterion": criterion, "status": "passed"})
            finally:
                browser.close()
    except Error as exc:
        if path is None:
            return {"status": "unavailable", "detail": "无法初始化浏览器验收页面"}
        errors.append(f"页面加载检查未完成：{str(exc).splitlines()[0][:240]}")
    return {"status": "failed" if errors else "passed",
            "verified_criteria": verified,
            "case_results": case_results,
            "detail": "；".join(errors[:4]) if errors else "隔离浏览器已加载页面，未发现启动脚本异常"}

"""Submit one live software_delivery run and report terminal evidence."""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"), strict=False)


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "delivery"
    task = ("构建一个订单统计工具：读取 CSV 订单数据并生成统计结果，"
            "编写并运行 unittest 测试，输出测试与风险报告，并做独立交付评审")
    run = post("/api/run", {"scenario": "software_delivery", "task": task})
    run_id = run["run_id"]
    print(f"RUN {label}: {run_id}", flush=True)

    deadline = time.time() + 420
    detail = {}
    while time.time() < deadline:
        time.sleep(5)
        detail = get(f"/api/runs/{run_id}")
        if detail["run"]["status"] in ("succeeded", "failed"):
            break
    run_obj = detail.get("run", {})
    status = run_obj.get("status")
    print("STATUS:", status, flush=True)
    if run_obj.get("error"):
        print("ERROR:", run_obj["error"], flush=True)
    stats = run_obj.get("stats") or {}
    print("STATS:", json.dumps(stats, ensure_ascii=False), flush=True)

    for n in detail.get("nodes", []):
        print(f"NODE {n['id']:12} {n['status']:10} attempts={len(n.get('attempts', []))}", flush=True)
        for a in n.get("attempts", []):
            v = a.get("verdict") or {}
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    v = {"raw": v[:120]}
            reasons = v.get("reasons") or []
            print(f"   try{a.get('attempt')} {a.get('status'):10} "
                  f"detail={str(v.get('detail') or '')[:120]} reasons={str(reasons)[:160]}", flush=True)

    for art in detail.get("artifacts", []):
        print("ARTIFACT:", art.get("name"), art.get("size"), flush=True)
    for item in detail.get("inputs", []):
        print("INPUT:", item.get("name"), flush=True)
    print("VERIFY_EVENTS:", len(detail.get("verifications", [])), flush=True)
    print("FEEDBACK_CYCLES:", len(detail.get("feedback_cycles", [])), flush=True)
    print("RUN_ID:", run_id, flush=True)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())

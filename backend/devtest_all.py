"""双场景冒烟测试：跑通软件交付与 AIOps 故障处置。"""
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from app import config, db, knowledge
from app.engine import Engine
from app.scheduler import seed_agents
from scenarios import all_scenarios, get as get_scenario


def run_one(sid: str, verbose=True):
    mod = get_scenario(sid)
    scenario = mod.SCENARIO
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + sid[:5]
    db.execute(
        "INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, scenario["task_default"], scenario["id"], "queued", config.mode_label(), db.now()),
    )
    Engine(run_id, scenario["task_default"], scenario, config.mode_label()).run()
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if verbose:
        print(f"\n=== {scenario['name']} → {run['status']} ===")
        for t in db.query("SELECT * FROM traces WHERE run_id=? ORDER BY id", (run_id,)):
            print(f"[{t['kind']:9}] {t['message']}")
        print("stats:", run["stats"])
    return run["status"]


def main():
    db.get_conn()
    seed_agents()
    from scenarios.aiops import SEED_DOCS

    knowledge.kb.seed_if_empty(SEED_DOCS)
    knowledge.kb.reload()
    print("scenarios:", [s["id"] for s in all_scenarios()])
    results = {}
    for sid in [s["id"] for s in all_scenarios()]:
        results[sid] = run_one(sid)
    print("\n结果:", results)
    return 0 if all(v == "succeeded" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

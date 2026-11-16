"""本地冒烟测试：不启动 Web 服务，直接跑一遍完整流水线。"""
import json
import sys
import time

from app import config, db, knowledge
from app.engine import Engine
from app.scheduler import seed_agents
from scenarios.software_delivery import SCENARIO


def main():
    db.get_conn()
    seed_agents()
    knowledge.kb.seed_if_empty()
    knowledge.kb.reload()

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-dev"
    task = SCENARIO["task_default"]
    db.execute(
        "INSERT INTO runs(id,task,scenario,status,mode,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, task, SCENARIO["id"], "queued", config.mode_label(), db.now()),
    )
    Engine(run_id, task, SCENARIO, config.mode_label()).run()

    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    print("RUN:", run["status"], run["error"])
    print("STATS:", run["stats"])
    print("\n--- 事件流 ---")
    for t in db.query("SELECT * FROM traces WHERE run_id=? ORDER BY id", (run_id,)):
        print(f"[{t['kind']:9}] {t['message']}")
    print("\n--- 步骤 ---")
    for s in db.query("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)):
        print(f"{s['node_id']:10} #{s['attempt']} {s['agent_name']:12} {s['status']:9} {s['tokens']}tk")
    print("\n--- 经验库 ---")
    for l in db.query("SELECT * FROM lessons WHERE run_id=?", (run_id,)):
        print(l["signature"], "|", l["strategy"], "|", l["patch"][:40])
    arts = sorted((config.RUNS_DIR / run_id / "artifacts").glob("*"))
    print("\n产出文件:", [p.name for p in arts])
    return 0 if run["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

"""重置演示数据：清空运行记录与调度统计，恢复初始状态。"""
import shutil
from pathlib import Path

from app import config, db, knowledge
from app.router import bootstrap
from app.scheduler import scheduler, seed_agents


def main():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    if config.RUNS_DIR.exists():
        shutil.rmtree(config.RUNS_DIR)
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    bootstrap()
    scheduler.reset_session()
    seed_agents()
    print("已重置：", config.DB_PATH)
    print("Agent 数量：", len(scheduler.summary()))
    print("知识库条目：", len(knowledge.kb.docs))


if __name__ == "__main__":
    main()

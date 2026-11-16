# AgentForge · 多智能体编排与验证平台

> 第五届「未来杯」赛题一「Agent 编排创新应用」参赛项目。
> AgentForge 不是又一个聊天外壳，也不是对模型 API 的简单封装：它是位于**用户任务**与**底层 Agent/模型**之间的任务级 Harness，负责把任务规划成 DAG、动态选择执行者、在隔离会话中执行、用**确定性证据**独立验证、对失败归因并自愈，全程留痕可审计。

---

## 一、为什么符合赛题（红线核验）

赛题红线：**单 Agent 简单调用或 API 封装不符合要求；必须体现多 Agent 协作 + 至少一个自我循环/迭代优化机制。** AgentForge 三项均有可复现的实机证据：

| 红线要求 | AgentForge 的实现 | 证据 |
| --- | --- | --- |
| **多 Agent 协作** | 复杂任务由模型规划为 analyst→coder→tester→reviewer 多节点 DAG，不同角色 Worker 在独立会话中执行，上游产物沿依赖边交接 | Run `20261116-112627-f43a61e1`：4 节点 4 角色、3 条产物交接（analysis.md / index.html / test_report.md） |
| **自我循环 / 迭代优化** | 失败 → 六类归因 → 携带失败证据修复 → 重试；跨 Run 经验复用 | 自愈 Run `20261116-113735-2c8e4efa`：注错→平台核算拒绝→`LOGIC_DEFECT` 归因→第 2 次尝试修复→2/2 通过 |
| **独立验证（差异化护城河）** | 平台**独立核算**客观拦下错误实现，不是「LLM 评 LLM」；真实浏览器断言验收页面行为 | 跨模型盲测：同一 Harness 下 qwen 20% / deepseek 80% / mimo 80%（`output/eval-blind-20261116-094116.md`） |

**一句话：错误不是被模型自己「感觉」发现的，而是被平台独立核算客观拦下，再携带证据修复。**

---

## 二、五大模块映射（赛题评分点逐条对照）

| 赛题模块 | 能力 | 代码位置 | 界面 | 实机证据 |
| --- | --- | --- | --- | --- |
| **① Agent 定义与配置** | 角色/模型/能力/成本/工具配置化；CRUD + 调度排序；成功率/成本/连续失败/降级熔断统计 | `backend/app/router.py`（`/api/agents` CRUD + `/api/agents/rank`）、`scheduler.py`（`seed_agents`/`rank`/`pick`/`report`） | 监控台「Agent 配置」页签：创建/编辑/停用/删除、调度选择理由因子分解 | 创建 `agent-75de8752`→排序第一→被 Run `20261116-113346-b183c6a6` 选中执行→统计更新 |
| **② 工作流编排** | 模型按复杂度生成 DAG（1–5 节点）+ 平台补齐；依赖/环校验；平台核算任务强制单节点 | `backend/app/planner.py`（`COMPLEXITY_CUES`/`_is_complex_task`/`_expand_complex_plan`/grounding 钩子）、`scenarios/software_delivery.py`（6 节点预置） | 工作台计划预览 + 复杂度判定横幅；监控台按 deps 分层 DAG | 复杂任务 4 节点 4 角色 DAG（见上）；DAG 渲染断言 8/8 |
| **③ 任务调度与执行** | 四维评分（能力 .40 / 成功率 .25 / 负载 .20 / 成本 .15）+ 降级 .25 / 熔断 .55；独立 Worker 会话/目录/工具策略；同批就绪节点并行 ≤3 | `backend/app/scheduler.py`、`engine.py`、`policy.py`、`function_tools.py`、`watchdog.py` | 监控台 Worker Session、调度候选排序与选择理由、事件流 | 盲测 5 任务调度执行（`output/eval-blind-20261116-114104.md`） |
| **④ 结果验证与监控** | 三级验证（L1 确定性 / L2 启发式 / L3 覆盖度）+ **平台独立核算** + **真实浏览器验收**；Typed Events 全程留痕 | `backend/app/validator.py`、`browser_check.py`、`events.py` | 监控台验证证据、事件流、成本；工作台逐条验收 | 盲测 deepseek 5/5；平台核算用例逐条可查 |
| **⑤ 反馈循环与优化** | 六类失败归因 + 结构化重试决策 + Agent 改派 + 跨 Run 经验复用（BM25） | `backend/app/selfloop.py`（`attribute`）、`optimizer.py`、`knowledge.py`、`context.py` | 工作台自愈叙事卡片（拒绝→归因→修复→通过四步） | 自愈演示 2/2（`output/demo-selfheal-20261116-113735.json`） |

---

## 三、快速开始

### 环境要求
- **Python 3.11+**（开发环境 3.13）。**核心零第三方依赖**：后端仅用标准库 HTTP Server + SQLite。
- 可选扩展：真实浏览器验收/截图需安装浏览器自动化依赖（`backend/requirements-browser.txt`）；GitHub 只读 MCP 需安装 MCP 依赖（`backend/requirements-mcp.txt`）。两者均非核心运行所必需。

### 一键启动（Windows）
```bat
run.bat
```
`run.bat` 优先使用 `.venv\Scripts\python.exe`，调用 `backend/app/launcher.py`：幂等（已运行则复用）、端口占用检测、后台 detached、PID 文件、自动打开工作台。

或手动：
```bash
cd backend
python -m app.launcher --port 8000 --no-browser
```

### 两个入口
- **任务工作台**（面向用户）：<http://127.0.0.1:8000/workbench> — 输入任务、预览计划、确认执行、查看进度与结果、自愈演示。
- **编排监控台**（面向开发者/演示）：<http://127.0.0.1:8000/> — DAG、Worker Session、调度评分、验证证据、失败反馈、事件、成本、Agent 配置。

### 离线 / 在线模式
- **离线（mock）**：不配置任何密钥时自动进入 mock 模式，评委**无需密钥即可跑通完整 Harness**（规划/调度/验证/自愈链路）。
- **在线（live）**：配置 Windows 用户级环境变量后真实调用模型。支持 **qwen / deepseek / mimo 三个 Provider**：
  - `MDATAPLUS_API_KEY`（qwen）、`AF_DEEPSEEK_API_KEY`（deepseek）、`AF_MIMO_API_KEY`（mimo）
  - `AF_DEFAULT_PROVIDER` 选择默认 Provider；网关含递增冷却、降级与跨模型 fallback 留痕（`backend/app/llm.py`）。
  - 密钥仅存于环境变量，仓库不含 `.env`，代码无硬编码密钥。

---

## 四、验证创新：平台独立核算

传统做法让模型自评，容易「自我感觉良好」。AgentForge 把验收权交给**平台**：

- 报名名单 / 会议室冲突 / AA 算账 / CSV 汇总等任务，预期结果由**平台按确定性规则独立计算**（如 AA 转账按整数分平摊），不采信模型手算。
- 页面类任务用**真实浏览器**执行断言（`browser_check.py`：转账/名单/ roster /冲突检测），验证的是页面实际行为而非源码字符串。
- 盲测固定 Harness、钉住规划模型、禁用跨模型回退，**业务通过 = 平台核算用例全部通过**。

最新盲测（`output/eval-blind-20261116-114104.md`，钉住 deepseek）：

| 任务 | Run | 业务判定 | 平台用例 |
| --- | --- | --- | --- |
| 报名名单 | `20261116-113854-82005ee5` | passed | 6/6 |
| 会议室冲突 | `20261116-113921-ee48d1c8` | passed | 4/4 |
| AA 算账 | `20261116-113959-5c2fa3e2` | passed | 2/2 |
| CSV 汇总 | `20261116-114030-22b67a11` | passed | 2/2 |
| 纯 JSON | `20261116-114057-de38eeca` | passed | 独立内容核对 |

**5 任务 × 100% 通过**，109,055 tokens / ¥0.109 / 平均 20.98s。

---

## 五、自愈演示主线（3 分钟，推荐演示）

完整分镜与话术见 [`docs/演示脚本-自愈主线.md`](docs/演示脚本-自愈主线.md)。一键复现：

```bash
cd backend
python demo_selfheal.py --port 8000 --resume --shots
```

- 真实规划 AA 任务 + 平台独立核算用例；**唯一「注入」的是首版产物**（历史真实错账页面 `backend/app/demo_assets/aa_faulty_index.html`，`provenance=demo` 全程标注）。
- 核算拒绝、失败归因、模型修复**全部真实执行**；`--resume` 失败自动携带证据续跑，`--shots` 对失败态/终态双入口截图。
- 结束生成证据报告 `output/demo-selfheal-<时间戳>.json` 并核验完整叙事（不全过则退出码 1，**不伪造验收**）。

实机证据：Run `20261116-113735-2c8e4efa`，注入→拒绝（用例 2 实际 140/200、用例 6 实际 80/200 错账）→`LOGIC_DEFECT`→携带证据修复（attempt 2）→**2/2 平台核算通过**，`narrative_complete=true`，约 69s。

---

## 六、质量与测试

```bash
cd backend
python -m unittest discover -s tests   # 179 项通过
```

- **179 项后端测试**（14 个测试文件），覆盖 API 契约、Harness 核心、运行时、模型网关、平台核算验收（报名/会议室）、前端契约、function tools、自愈竞赛特性等。
- **真实浏览器断言**：主题一致性 16/16、多节点 DAG 8/8、自愈叙事卡片 10/10、Agent 面板 UI 全流程 6/6、分栏拖拽 13/13。
- 80+ 真实在线 Run；计划/事件/验证同事务写入，避免半份记录。

---

## 七、项目结构

```text
AgentForge/
├─ run.bat                     # 一键启动（Windows）
├─ backend/
│  ├─ app/
│  │  ├─ server.py / router.py # 标准库 HTTP Server + 路由（含 /api/agents、/api/demo/selfheal）
│  │  ├─ planner.py            # 复杂度规划 + DAG 校验 + 平台核算 grounding
│  │  ├─ engine.py             # 执行引擎：Worker 会话 / 产物交接 / 自愈注入 / 候选兜底
│  │  ├─ scheduler.py          # 四维评分调度 + 降级熔断 + 统计
│  │  ├─ validator.py          # 三级验证 + 平台独立核算
│  │  ├─ browser_check.py      # 真实浏览器验收
│  │  ├─ selfloop.py / optimizer.py  # 失败归因 + 重试决策
│  │  ├─ knowledge.py / context.py   # BM25 经验库 + 上下文装配
│  │  ├─ llm.py                # 三 Provider 模型网关（冷却/降级/fallback）
│  │  ├─ function_tools.py     # 工具（write_file/append_file/read_file/run_tests…）
│  │  ├─ launcher.py           # 幂等后台启动器
│  │  └─ demo_assets/          # 自愈演示注错素材（溯源历史真实产物）
│  ├─ scenarios/               # 预置场景（software_delivery 6 节点 / aiops）
│  ├─ tests/                   # 179 项测试
│  ├─ demo_selfheal.py         # 自愈演示复现工具
│  ├─ eval_blind.py            # 盲测工具
│  └─ acceptance_p01_p02.py    # 竞赛缺口验收工具
├─ frontend/
│  ├─ workbench.html           # 任务工作台
│  ├─ index.html               # 编排监控台（DAG / Agent 配置 / 可拖拽分栏）
│  └─ theme.css                # 统一暖色主题
├─ docs/                       # 白皮书 / 技术方案 / 架构调用图 / 演示脚本 / design-qa
└─ output/                     # 盲测 / 自愈 / 验收证据报告与截图
```

---

## 八、证据索引

| 类别 | 文件 / Run |
| --- | --- |
| 多 Agent 协作 | Run `20261116-112627-f43a61e1`；`output/acceptance-p01-p02-20261116-112230.json` |
| Agent 配置 | Run `20261116-113346-b183c6a6`；`agent-75de8752` |
| 自愈主线 | Run `20261116-113735-2c8e4efa`；`output/demo-selfheal-20261116-113735.json` |
| 平台盲测（最新 5/5） | `output/eval-blind-20261116-114104.md` |
| 验证创新（跨模型对照） | `output/eval-blind-20261116-094116.md` |
| 设计 QA（真实浏览器截图） | [`docs/design-qa.md`](docs/design-qa.md) |

> 说明：`output/acceptance-2026-11-16.json` 中的 AA 失败条目（`20261116-012851-5870d071`）是**自愈演示的注错素材来源**，其续跑 `20261116-013647-919b50d1` 已通过；该失败是演示刻意保留的真实历史样本，非平台缺陷。
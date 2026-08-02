# Cerberus EA 源码对分析控制台的接口与数据流地图

> 梳理范围：聚焦 `D:\XAU\mt5\Cerberus_v117_AvaR1.mq5`（主交易 EA，GUARDIAN + ORACLE）。
> 梳理角度：接口面 + 数据流地图，说明 EA 源码如何与 `D:\XAU\TradingAgents`（只读分析控制台）发生关系。
> 标注约定：**[事实]** 由代码/文档直接支持；**[推断]** 合理延伸但待验证；**[建议]** 候选接口，尚未落地。

---

## 0. 结论（先结论后证据）

- **[事实]** Cerberus 是**执行层**（MQL5，运行于 MT5 终端，拥有交易权限）；分析控制台是**只读分析层**（无下单/持仓/账户能力，见工作区记忆）。
- **[事实]** 当前两个系统**无直接代码耦合**：控制台独立并行地从 MT5 终端取快照、从 `faireconomy.media` 取 ForexFactory 日历，grep 控制台代码库确认不存在对 `ng_status.json` / `ff_cache.json` / Cerberus 全局变量的任何引用。
- **[推断]** EA 对"系统"的帮助目前是**结构性与间接的**：它提供了与控制台闸门同构的、已实盘验证（demo）的风控模型与同源日历语义；而非直接的代码贡献。
- **[建议]** 存在 **2 个明确可落地接口**能把 EA 纳入控制台数据链（日历单源、闸门状态前向对齐），均为低风险的"只读消费"，不引入交易权限。

---

## 1. 两个系统的职责边界

| 维度 | Cerberus EA（执行层） | 分析控制台（分析层） |
|---|---|---|
| 位置 | `D:\XAU\mt5\Cerberus_v117_AvaR1.mq5`（源码）/ MT5 终端 `MQL5/Files`（运行时产物） | `D:\XAU\TradingAgents`（Python） |
| 运行环境 | MT5 终端内 | 独立 Python 进程（HTTP 127.0.0.1:8767） |
| 交易权限 | **有**（下单抗、篮子管理、风控平仓） | **无**（快照只读，见工作区记忆 `account_trade_reads/writes=FORBIDDEN`） |
| 核心产出 | 持仓/订单 + `ng_status.json` + `ff_cache.json` + 日志 | `calendar.json` + MT5 快照 + Qwen 中文简报 |
| 状态 | Ava demo，**live use forbidden**（CERBERUS_AVA_R1.md） | 只读分析 |

---

## 2. Cerberus 接口面（它产出/消费什么）

### 2.1 出向接口（OUT）

| 接口 | 位置 | 内容 | 证据 |
|---|---|---|---|
| `ng_status.json` | MT5 终端 `MQL5/Files/` | 完整运行态 JSON（见 2.3 schema） | `Cerberus_v117_AvaR1.mq5:1967` `WriteStatusFile()` |
| `ff_cache.json` | MT5 终端 `MQL5/Files/` | ForexFactory 周历 JSON 缓存 | `:1079/:1088`，来源 `FEED_URL="https://nfs.faireconomy.media/ff_calendar_thisweek.json"`（`:45`） |
| `Cerberus_AvaR1_log.csv` | `MQL5/Files/` | 动作流水日志 | `:602` `FileOpen(LogFileName…)` |
| `R1_*` 全局变量 | MT5 终端（跨重启持久） | `GV_GUARD/GV_MANUAL/GV_SCHED/GV_ORACLE`、`R1_Off_<sym>`、`R1_DayDate/DayStartBal`、`GV_OV_*`（运行时覆盖） | `:716-742`、`:1482-1527`、`:2007-2021` |
| 持仓/订单 | MT5 终端内部 | 由 `Trade.mqh` 管理，不入文件 | `#include <Trade\Trade.mqh>`（`:34`） |

### 2.2 入向接口（IN）

| 接口 | 机制 | 指令集 | 证据 |
|---|---|---|---|
| `ng_command.txt` | `MQL5/Files/`，**读取即删除** | `AT_ON`/`AT_OFF`/`PAUSE`/`RESUME`/`CLOSEALL`/`RESETDAY`/`TEST=N`/`BUY <sym> <lots>`/`SELL <sym> <lots>`/`ORACLE_ON`/`ORACLE_OFF`/`SYMON <sym>`/`SYMOFF <sym>`/`BSTOP <usd>` | `:1535-1538`、`:1572-1629` |
| WebRequest | HTTPS GET | 取 ForexFactory 周历 JSON | `:971` `WebRequest("GET", FEED_URL…)` |
| 全局变量覆盖 | `R1_ov*` | 热改 TP/Grid/Lot/Factor/MaxLev/Bstop/EmaGate | `:2007-2021` |

### 2.3 `ng_status.json` 字段 schema（出向核心，控制台可消费）

来源 `:1952-1965` 的 `StringFormat` 模板，关键字段：

- `ea` / `version` / `gmt`（UTC 时间）
- `status`：`RUNNING` | `PAUSED_MANUAL` | `PAUSED_NEWS` | `PAUSED_SCHEDULE` | `PAUSED_VOLATILITY`
- `hour`：`risk`（风险等级名）、`blocked`（bool）、`change_min`、`sched_blocked`
- `market`：`symbol` / `open` / `close_in_min`
- `config`：`symbol` / `tp` / `grid` / `lot` / `factor` / `maxlev`
- `basket_stop`：`usd` / `hits_today`
- `regime_blocked`（H1 趋势否决，bool）
- `autotrading`（bool）
- `feed`：日历拉取状态字符串（如 `ERROR WebRequest 4014`）
- `events_loaded`（已加载事件数）
- 账户：`balance` / `equity` / `free_margin` / `margin_level` / `positions_pl`
- 绩效：`closed_trades` / `wins` / `losses` / `win_rate_pct` / `realized_pl` / `closed_today` / `avg_win` / `avg_loss` / `peak_equity` / `dd_money` / `dd_pct`
- `heads.oracle` / `heads.baskets[]` / `heads.cycles` / `heads.realized`
- `positions[]`（ticket/type/symbol/lots/open/pips/pl/magic）
- `recent_trades[]` / `next_event` / `last_action`

---

## 3. 分析控制台接口面（它消费什么）

| 接口 | 来源 | 落点 | 证据 |
|---|---|---|---|
| MT5 快照 | `scripts/mt5_xau_snapshot_with_ticks_once.py` → 动态加载外部 `D:\XAU\scripts\mt5_xau_market_context_once.py` → MT5 终端 | `data_cache/mt5_context/latest.jsonl`（`market_context` + `tick_health`） | `scripts/mt5_xau_snapshot_with_ticks_once.py:11,97`；`local_console/snapshot.py:42` |
| 日历 | WebRequest `https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml`（ForexFactory 周历 XML，取 USD 高/中影响，ET→UTC） | `calendar.json` | `local_console/calendar.py:45,98` |
| 闸门 | `guard.py` 两态 + 标注（2026-08-01 军师模式）：BLOCKED（数据不可用）/ ANALYSE（常态）；事件窗口、未核验、tick/共振/震荡市/EA 风控一律转 `warnings` 标注，不阻断分析 | 内存状态机 | `docs/xau-system-constitution.md` |
| 简报 | `brief.py` → Qwen（quick=qwen3.7-max / deep=qwen3.8-max-preview） | 报告 JSON | 工作区记忆 |

**关键差异（事实）**：控制台拉的是 `…/ff_calendar_thisweek.**xml**`；Cerberus 拉的是 `…/ff_calendar_thisweek.**json**`——同一发布方（`faireconomy.media`）、**不同端点与格式**。两者都取 USD 事件，但缓存文件互不通用。

---

## 4. 数据流地图

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cerberus EA  (执行层 · MQL5 · 有交易权限 · Ava demo, live forbidden)  │
│                                                                        │
│  WebRequest GET                                                        │
│     │  nfs.faireconomy.media/ff_calendar_thisweek.json                │
│     ▼                                                                  │
│  ff_cache.json (MQL5/Files)  ──┐                                        │
│                                 │ 〔当前未接入控制台〕                  │
│  WriteStatusFile()              │                                       │
│     ▼                           │                                       │
│  ng_status.json (MQL5/Files) ──┤ 〔接口 B 候选：闸门前向对齐〕         │
│                                 │                                       │
│  ProcessCommandFile() ◄── ng_command.txt (MQL5/Files, 读即删)          │
│  GlobalVariables R1_* (终端, 跨重启)                                   │
│  MT5 持仓/订单 (终端内部, Trade.mqh)                                    │
└──────────────────────────────────────────────────────────────────────┘
            │                                  │
            │   （两个系统当前无直接耦合，各自独立取数）
            ▼                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  分析控制台  (只读分析层 · Python · 无交易权限)                          │
│                                                                        │
│  scripts/mt5_xau_snapshot_with_ticks_once.py                           │
│     → 外部 mt5_xau_market_context_once.py → MT5 终端                    │
│     → data_cache/mt5_context/latest.jsonl (market_context+tick_health) │
│                                                                        │
│  calendar.py  WebRequest GET                                          │
│     → cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml              │
│     → calendar.json (USD 高/中影响, ET→UTC)                            │
│                                                                        │
│  guard.py: BLOCKED / ANALYSE（其余风险一律转 warnings 标注）            │
│  brief.py → Qwen 中文简报                                              │
└──────────────────────────────────────────────────────────────────────┘

图例：实线 = 已存在数据流；虚线/〔〕 = 候选可落地接口（尚未实现）。
```

---

## 5. EA 对系统的"帮助"——现状与可落地接口

### 5.1 现状（已存在的间接帮助）

1. **[推断] 同构风控模型参照**。Cerberus GUARDIAN 的核心阈值——新闻窗口 `MinutesBefore/After = 30`（`:72-73`）、波动率熔断 `VolSpikeATRmult = 5`（`:85`）、`HourBlockRisk = 3`（`:88`，封锁 VERY HIGH 时段 08:00–09:30 / 12:00–15:30 UTC）、H1 趋势否决（`regime_blocked`）——与分析控制台 `guard.py` 的**风险标注体系**（事件窗口/波动率/震荡市/高危时段 → warnings）是**同构的风控思想**。即控制台的标注阈值有了一个"在 demo 实盘跑过"的参照系，而非纯手工设定。注意：EA 处于 demo 且 live use forbidden，**这不构成该阈值的预测能力证据**，仅说明其工程合理性。
2. **[事实] 日历同源语义**。两边均从 `faireconomy.media` 取 USD 事件，控制台 `calendar.json` 与 Cerberus 事件窗口对"高影响事件"的判定天然一致，不会出现控制台给方向而 EA 因新闻窗口停机的语义冲突。

### 5.2 可落地接口（当前未接，属建议/候选）

| 接口 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A. 日历单源** | 控制台 `calendar.py` 改为消费 Cerberus 的 `ff_cache.json`（或反向），消除双拉取、单一事实源 | 低；但需格式对齐（Cerberus 缓存 `.json` thisweek，控制台解析 `.xml` thisweek），且需确认 `MQL5/Files/` 路径对 Python 进程可达 | **EXPLORATORY_WATCH** |
| **B. 闸门前向对齐** | 控制台 `guard.py` 读取 `ng_status.json` 的 `status`（`PAUSED_NEWS`/`PAUSED_VOLATILITY`…）、`regime_blocked`、`hour.blocked`、`feed`，使分析闸门的**风险标注**与 EA 实盘暂停态一致，避免"EA 已停但简报无提示"错位 | 低（只读消费）；失败安全：文件缺失/陈旧则忽略（同 guard.py 只标注不阻断原则） | **已落地（2026-07-31）**，2026-08-01 军师模式后全部转为 warnings 标注，见下方"接口 B 实施记录" |
| **C. 上下文富化** | 用 `ng_status.json` 的 `positions[]`/`realized_pl`/`dd` 作简报"当前暴露"上下文 | 中；控制台无交易权限，只能作信息展示，**不得**暗示交易建议或把 EA 绩效误读为预测能力 | **WATCH**（需明确边界；接口 B 实施时已决定**不接**持仓/盈亏字段） |

### 接口 B 实施记录（2026-07-31）

- **路径已核实**：`C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\ng_status.json`（MT5 终端数据目录，非源码目录）。
- **写入频率已核实**：EA 运行时 `OnTimer` 节流**每 30 秒**最多写一次（`Cerberus_v117_AvaR1.mq5:858-862`）；陈旧阈值取 **120 秒**（4 倍余量，env `XAU_CONSOLE_EA_STATUS_MAX_AGE` 可调）。
- **改动文件**：
  - `local_console/ea_status.py`（新增）：只读解析 + 失败安全（缺失/坏 JSON/缺 gmt/格式异常/陈旧 → `available=False` 静默忽略）。
  - `local_console/guard.py`：新增 `ea_downgrade_reason()`；`evaluate_gate` 增加可选参数 `ea_status`（缺省 None = 行为与接入前完全一致）。映射（2026-07-31）：`PAUSED_NEWS`→`WAIT`（禁模型，优先于"事件未核验"的 WATCH）；`PAUSED_VOLATILITY`/`regime_blocked`/`hour.blocked`→`WATCH`；`PAUSED_MANUAL`/`PAUSED_SCHEDULE` 属操作选择非市场证据，**不降级**。**（2026-08-01 军师模式）**：WAIT/WATCH 门态废除，上述风险机制字段全部转为 `warnings` 标注，模型始终运行。
  - `local_console/config.py`：`ea_status_path`（env `XAU_CONSOLE_EA_STATUS_FILE`，置空串 = 关闭接入）+ `ea_status_max_age_seconds`。
  - `local_console/service.py`：`_safe_ea_status()`（异常静默不可用）；gate_payload 新增 `ea_status` 摘要——**只含风险机制字段，明确不含持仓/盈亏**。
- **纪律执行**：持仓、篮子、`win_rate`、`realized_pl`、`dd` 等事后测量**未进入**闸门与模型事实包。
- **验证**：新增 24 个测试（读取失败安全 + 映射 + service 接线），全量 `tests/local_console` **198 passed** 无回归；真实文件（已陈旧 7 天，EA 未运行）实测按不可用忽略；模拟新鲜 `PAUSED_NEWS` 端到端实测闸门正确降为 WAIT 且禁模型。
- **生效条件**：Python 改动需**重启控制台**；EA 未运行时接入完全不改变控制台行为。

---

## 6. 风险与边界（HY3 口径）

- **[事实]** 控制台无交易权限；EA 在 demo，live use forbidden。任何接口都**不得**把执行层状态误读为"可下单"或"有预测能力"。
- **[事实]** 两系统当前独立；接入需新增只读读取层。引入 `ng_status.json` 读取必须失败安全——EA 未运行/终端关闭时文件缺失或陈旧，读取层应忽略而非阻断分析（接口 B 已按此实现并实测）。
- **[推断]** 即便接入接口 B/C，EA 的 `win_rate_pct` / `realized_pl` 等属于**事后测量**，不等于预测能力证据（遵守工作区测量层纪律）。

---

## 7. 下一步（待用户确认）

1. ~~接口 B（闸门对齐）~~ **已落地（2026-07-31）**，见 5.2 实施记录。后续观察：EA 运行期间闸门降级是否符合预期（降级事件会进 runlog 与 gate_payload）。
2. 若优先落地**接口 A（日历单源）**：需先确认 Cerberus 的 `ff_cache.json` 字段结构，并决定以 `.json` 还是 `.xml` 为唯一上游，写一层格式归一。
3. **接口 C** 建议暂不落地，待 B 在 EA 运行期验证后再评估边界。

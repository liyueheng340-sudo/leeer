# XAU 分析控制台审查报告（2026-08-03）

审查范围：`local_console/` 全量 33 个模块 + 前端（`static/app.js`、`static/index.html`）+ LLM 客户端层
（`tradingagents/llm_clients/`）+ 运行中系统实测。

验证方式：全模块通读、全部导入自检、`tests/local_console` 306 个测试全绿（19.3s）、
运行中服务（127.0.0.1:8767）API 实测与 runlog/任务目录取证。

---

## 1. 系统链条图

```
┌─ MT5 子进程（只读，独立进程）─────────────────────────────┐
│  合并采集 snapshot+ticks（默认）→ 失败回退双脚本           │
│  review bars（M5 历史，复盘专用）                          │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 事实层（确定性，无模型推断）─────────────────────────────┐
│  snapshot_facts：关键价位层（±1.0 ATR 容差）/ 参考 ATR      │
│  resonance：多周期方向共振（H4=4权…M5=1权，score∈[-1,1]）   │
│  regime：双周期 ADX 趋势/震荡 + RSI 极端 + StdDev 波动确认  │
│  session_context：伦敦定盘/COMEX 开盘分钟数（纯时间函数）   │
│  spread_percentile：点差历史分位（≥5 样本才出值）           │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 事实闸门 guard.py（军师模式：只降级不锁死）───────────────┐
│  BLOCKED 仅当：身份/品种不匹配、报价不可用、快照>60s        │
│  其余一切（事件窗口/未核验/共振不明/震荡市/点差/EA 风控/    │
│  非活跃时段）→ warnings 随报告呈现，永不阻断                │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 模型层 ─────────────────────────────────────────────────┐
│  快评 brief：Qwen 快速模型，1 次应用层重试，120s 超时       │
│  深度复盘 debate：Qwen/DeepSeek/GLM 三家真辩论（3 轮），    │
│    并行 + 修复轮 + 交叉质疑 + 分歧收敛，≥2 家有效出共识    │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 验收层 report_validation.py ────────────────────────────┐
│  真实性：source_ids 锚定 mt5_snapshot、中文正文白名单      │
│  几何：TP>entry>SL 方向正确、幅度 ≤ 模式 ATR 上限           │
│  证据：evidence_fields 必须解析到 facts 真实字段路径       │
│  建议：suggestions/scenarios/avoid 中文非空数组           │
│  纪律（标注不拒）：共振相悖/震荡市强方向/入场不贴关键价位   │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 任务/服务层 ────────────────────────────────────────────┐
│  JobStore 原子写（Windows 锁竞争退避）+ 陈旧任务回收       │
│  单 worker 串行用户任务；家务/自主调度在独立守护线程        │
│  runlog JSONL 全阶段落盘                                  │
└────────────────────────────┬──────────────────────────────┘
                             ▼
┌─ 复盘测量层 review（事后 MT5 M5 K 线判定）────────────────┐
│  TP_FIRST/SL_FIRST/NOT_TRIGGERED/EXPIRED_UNRESOLVED       │
│  同根 K 线双命中保守记 SL；按闸门/共振/方向/模式/波动环境    │
│  单维切分（刻意不做多维交叉防过拟合），全程测量层声明        │
└────────────────────────────┬──────────────────────────────┘
                             ▼
                    前端（轮询 /api/jobs，四视图）
```

## 2. 静态审查结论（各层要点）

| 层 | 结论 |
|---|---|
| 事实层 | 全部确定性复算，无模型推断；关键价位层 16 个上限防 prompt 膨胀；缺失数据逐项跳过 |
| 闸门 | 宪法"数据保真，永不锁死"贯彻一致；EA 风控只读风险机制字段、绝不读持仓盈亏（HY3 纪律） |
| Prompt | `PROMPT_VERSION=1.7.0` 版本化落盘可关联复盘；MODE_RULES 与验收共用同一张表；facts_paths 扁平化清单让模型照抄 |
| LLM 客户端 | provider registry 单一事实源；key 缺失报错清晰；Responses API/DeepSeek reasoning 回传/本地兼容均有专项处理 |
| 验收 | 真实性/几何/证据/建议四类校验分层；方向纪律 1.5.0 起由硬拒改为标注；叙述性字段缺失自动补默认 |
| 任务层 | 快评验收失败重试 1 次自愈；深度复盘不重试（慢模型重试只会独占 worker）；异常必落 runlog |
| 服务层 | 单实例纪律（禁 SO_REUSEADDR）；陈旧任务扫描节流；worker 异常回调防静默卡单 |
| 前端 | 轮询指数退避不判死；全部模型/错误文本转义；遗留英文报告隐藏不冒充合规中文报告 |
| 复盘 | 测量层定位明确；小样本标注；PENDING 任务复跑覆盖 |

测试：306 通过，覆盖文件锁退避、超时降级、JSON 容错解析、陈旧回收等失败路径。

## 3. 运行中系统实测

服务状态：`/api/status` → service ready；自检 `mt5=ok / fred=configured / calendar=stale`；自主调度开启（900s 节奏，最近触发 03:26 UTC）。

任务存量（171 单）：`COMPLETE 131 / REJECTED 26 / FAILED 14`；快评 150、深度复盘 21。

复盘统计（实机）：已决 55/73，胜率 32.7%，平均 R −0.118。
按闸门：**ANALYSE 组胜率 16.1% / avg R −0.625 vs WATCH 组胜率 54.2% / avg R +0.536**
（与 prompt_rules 1.3.0 变更注释中"ANALYSE −0.23 vs WATCH +0.54"的方向一致，且差距进一步拉大）。
按 1.7.0 提示词：18 已决中胜率 5.6%、avg R −0.911（样本小，18 单 PENDING 未决）。

### 实机发现的风险点

**[P0] 运行中的服务进程是旧代码**（最严重）
- 证据：实机 REJECTED 原因 `报告引用了未提供的数据源：session_context/tick_health/timeframe_resonance`
  ——该消息在当前 `report_validation.py` 源码中**已不存在**（当前只要求锚定 mt5_snapshot）。
- 时间线：服务 09:54（本地）启动；`report_validation.py`/`prompt_rules.py` 于 11:27 被修改（未提交），
  运行进程从未重启加载 → 实机执行的是放宽前的旧校验逻辑。
- 影响：8 单（08-03 当天）因此类旧逻辑被误拒；工作区里已写好的修复（source 白名单放宽、
  facts_paths 注入、NARRATIVE_DEFAULTS、items[] 索引支持）**尚未生效**。
- 处置：重启控制台服务；确认工作区 5 个改动文件 + test_brief.py 无遗漏后提交。

**[P0] 事件日历层整体失效（静默降级掩盖了故障）**
- 证据：`calendar.json` 与 `event_context.json` 均不存在；实测拉取
  `ff_calendar_thisweek.xml` 抛 `SSL: UNEXPECTED_EOF_WHILE_READING`。
- 影响：每一单闸门都带"事件上下文未核验"标注（fail-safe 按设计工作，但事件驱动信息
  自 7/31 起已全部缺失——`next_event`/`past_events` 从不出现在 prompt）。
- 处置：更换日历源或修复 TLS（见建议 2）。

**[P1] `key_levels` 在 source 白名单中、却不在 facts 字段里 → 必然误拒**
- `allowed_source_ids` 允许 `key_levels`，但 `build_facts` 输出的快照字典没有 `key_levels` 键
  （它由 `_key_levels(snapshot)` 即时计算）。模型按 prompt 引用 `key_levels` 做 evidence_fields
  时，`_resolve_evidence_path` 解析失败 → REJECTED（08-03 实测 2 次）。
- 修复：在 `facts_builder.build_facts` 把 `_key_levels` 结果注入 `facts["key_levels"]`，
  或从 source 白名单移除 `key_levels`。

**[P1] 嵌套路径 `background_macro.series.*` 模型易猜错**
- 实测 1 单引用 `background_macro.DFII10.latest`（真实路径是 `background_macro.series.DFII10.latest`）→ REJECTED。
- facts_paths 清单已缓解（模型照抄而非猜），但 brief 路径 `loose_evidence=False` 仍硬拒；
  可评估将证据路径错误降级为警告（同方向纪律一样标注不拒）。

**[P2] FAILED 14 单构成**：模型层失败 8（"Qwen 分析多次重试仍失败"）+ 陈旧扫描 6（"模型响应超时"）。
深度复盘 21 单中 7 FAILED、2 REJECTED（含 1 单"辩论失败：0 家有效报告"）——三家全灭时无兜底降级到快评。

**[P2] 被拒报告的模型输出不落盘**：`validate_report` 失败时 `report=None`，只留一句原因。
无法事后审计模型到底输出了什么（哪些字段/路径违规），排查只能靠重跑。

## 4. 修复建议（按优先级）

1. **重启控制台服务**加载当前工作区代码，并提交未提交改动（brief/debate/job_runner/prompt_rules/report_validation + 对应测试）。
2. **修复日历拉取**：为 `refresh_calendar_from_url` 增加 TLS 失败重试/备用源（如
   `https://www.forexfactory.com/calendar` 或配置 `XAU_CONSOLE_CALENDAR_URL`），
   并让 `self_check.calendar` 在长期拉取失败时给出更显眼的告警（而非仅 "stale"）。
3. **对齐 source 白名单与 facts 字段**：`key_levels` 注入 facts 顶层，消除白名单/证据路径不一致。
4. **证据路径校验分级**：`evidence_fields` 未解析到时先转 warning（军师模式一致），
   仍保留"列表索引越界/格式非法"为硬拒，降低 brief 误拒率。
5. **被拒报告落盘**：REJECTED 时把原始模型输出与验收失败详情写入 runlog 或独立 jsonl，
   使 26 单 REJECTED 可被事后诊断（当前完全黑盒）。
6. **辩论全灭兜底**：三家全部无效时降级为快评单模型（或至少落盘三家原始输出），
   避免深度复盘 1/3 概率直接 REJECTED。
7. **关注复盘信号**：WATCH 组 avg R +0.536 显著优于 ANALYSE 组 −0.625，且 1.7.0 强方向组
   继续恶化——1.3.0 收紧强方向的方向正确，建议在 1.8.0 继续收敛"共振不明确/震荡市/非活跃
   时段"下的强方向输出倾向（把更多情境推向 WATCH/NEUTRAL）。

## 5. 结论

系统架构成熟度较高：确定性事实层、军师模式闸门、双模式纪律、版本化 prompt、复盘测量闭环
均已落地且有 306 测试保障；实机链路（快照→闸门→模型→验收→复盘）可端到端跑通。

但运行态存在两个必须立即处理的实机缺陷：**服务进程运行旧代码（修复未生效）** 与
**事件日历层自 7/31 起失效**。另有 key_levels 白名单/字段不一致、证据路径硬拒、被拒报告
不落盘等可改进项。建议先执行建议 1-2，其余按优先级排期。

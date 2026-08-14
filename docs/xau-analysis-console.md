# XAU Analysis Console

启动（仓库根目录）：

```powershell
.venv\Scripts\python.exe scripts\run_xau_analysis_console.py
```

浏览器会打开 `http://127.0.0.1:8767`。服务只绑定本机，不向局域网开放。
如果控制台已经在这个地址运行，重复启动会直接接管现有实例，不会再拉起第二个服务。

页面有两个操作：

- 刷新 MT5 并生成简报：创建实时任务，顺序显示读取 MT5、事实校验、Qwen 分析、报告校验和完成状态。
- 深度复盘：使用 Qwen 3.8，流程相同，通常耗时更长。

进度来自服务端持久任务记录。刷新浏览器后会恢复当前任务，不是前端倒计时。

## 门态语义（2026-08-03 修订，见 docs/xau-system-constitution.md 第二/九条）

系统是军师不是保安：**分析层永不锁死**——模型永远运行，任何时刻都给出分析。
入场**方案**受实证纪律约束（宪法第九条）：`ANALYSE` 门态按 `directional_plan_allowed` 分两个子态：

- `ANALYSE + directional_plan_allowed=true`：常态子态，输出完整方向计划（入场/TP/SL + 观察建议）。
  事件窗口、市场状态、流动性、共振、EA 风控等风险因素都转为 `warnings` 标注，
  随报告呈现给交易者（前端琥珀色标注条），不阻断方向建议。
- `ANALYSE + directional_plan_allowed=false`：入场纪律闸门触发——不输出方向计划，
  照常输出观察/等待条件与风险标注（"不给方案"，不是"不开口"）。当前闸门与证据编号
  （宪法第九条第 2 款）：
  - 点差成本闸门：`spread_percentile ≥ 0.8` 或峰值 ≥ 0.5 价格单位（高位组 12% 胜率 / −23.3R）；
  - 时段闸门：scalp 模式亚洲时段（外部共识 + 本地 51 单亏损池回放验证）。
  模型照常运行、报告照常产出、拦截原因随 `gate.warnings` 呈现，禁止静默拦截。
- `BLOCKED`：唯一保底——第一手数据不可用（快照过期/报价无效/身份不匹配）。此时模型不运行。
  入场纪律不得新增任何 BLOCKED 路径（第九条第 5 款）。
- `REJECTED`：模型报告违反真实性约束（引用未提供数据的硬约束、
  几何/幅度越界、非中文）。方向纪律**标注**类（共振相悖、震荡市强方向、
  入场不贴关键价位、scalp 追高追低）不拒绝，经 `report.validation_warnings` 随报告呈现
  （2026-08-03 余量修正：追价由硬拒改标注——低质量建议如实呈现，简报始终可产出）。
- 旧 `WATCH`/`WAIT` 门态已废除：事件未核验/事件窗口只产生标注，不再禁模型。

控制台只调用已有的只读 MT5 行情快照脚本。它没有下单、仓位、平仓、止损止盈、账户设置或 MT5 配置入口。

## 数据层（2026-07-31 升级）

任务在 GATE 阶段汇聚四类事实，全部失败安全（传感器故障只降级、不阻断）：

| 层 | 来源 | 频率 | 用途 |
|---|---|---|---|
| MT5 快照 | 合并采集脚本（见下），内部复用外部只读快照逻辑 | 分钟级 | 第一事实来源：报价、ATR、多周期结构 |
| tick 传感器 | 与快照同一 MT5 会话产出（零额外调用） | 近 60 秒 tick | 点差峰值 ≥0.5 或分位 ≥0.8 → **入场纪律拦截禁方向**（宪法第九条）；报价停滞 → warnings 标注（不阻断） |
| 事件日历 | `data_cache/xau_analysis_console/calendar.json` | 自动拉取（多源回退+退避）/可选 URL 覆写 | 高影响事件前 60 分钟至后 30 分钟 → wait 状态 → 转为 warnings 标注（不锁模型） |
| FRED 宏观背景 | DFII10 / DTWEXBGS / DGS10 / T10YIE | 日频，缓存 6 小时 | 中期背景层，禁止用于盘中价位描述 |
| EA 风控态（2026-07-31 接入） | Cerberus `ng_status.json`（MT5 终端 `MQL5/Files/`，只读） | EA 约 30 秒写一次；陈旧 >120 秒即忽略 | 风险标注：`PAUSED_NEWS`/`PAUSED_VOLATILITY`/`regime_blocked`/`hour.blocked` 全部转为 warnings（军师模式不阻断）；只消费风险机制字段，不含持仓/盈亏。详见 `docs/cerberus-ea-interface-map.md` |

## 性能架构（2026-07-31）

消除重复调用与关键路径压缩：

- **合并 MT5 会话**：`scripts/mt5_xau_snapshot_with_ticks_once.py` 在单个子进程、单个 MT5 会话内同时产出 `market_context` 与 `tick_health` 两条 JSONL 记录（快照本体动态加载外部脚本的 `build_market_context`，不复制逻辑）。实测（热终端）：旧路径两个子进程串行 2.6s → 合并后 1.4s，且 tick 传感器从此零成本常驻。合并采集失败时自动回退到旧的两个独立脚本，行为不变。
- **宏观层移出关键路径**：FRED 拉取与 MT5 采集并行执行；四个序列内部并行（冷缓存 4×RTT → 约 1×RTT）；6 小时缓存命中时零网络。
- **状态扫描节流**：前端每秒轮询会触发服务端陈旧任务扫描（全目录遍历），已节流到每 2 秒一次；历史列表改为按文件修改时间排序、只解析所需条数，不再全目录逐文件读取。
- 每个数据层单次调用：合并采集 1 次、事件评估 1 次、宏观 1 次（缓存命中则 0 次）、模型 1 次（max_retries=0，无隐性重试）。

## 事件日历格式

```json
{
  "updated_at": "2026-07-31T00:10:00+00:00",
  "source": "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml",
  "schema_version": 1,
  "events": [
    {"title": "美国核心 PCE", "utc": "2026-07-31T12:30:00+00:00", "impact": "high"}
  ]
}
```

- 信任标记：`source`（非空字符串）与 `schema_version`（=1）由 `refresh_calendar_from_url` 代码生成写入，
  缺失任一即视为手工/残留文件（不可信 → unverified）。手工维护请用 `event_context.json` 覆写，不要手写 calendar.json。
- 日历超过 36 小时未更新 → 视为不可信（unverified），事件信息随报告标注"未核验"（并携带具体原因），不阻断分析。
- 高影响事件落在周末（周六/周日）→ 日期数据错误，同样判 unverified（美国宏观数据从不在周末发布）。
- 手工覆写仍优先：`event_context.json` 写入合法的 `verified_clear`/`wait` 即直接生效。
- 可选自动拉取：设置 `XAU_CONSOLE_CALENDAR_URL` 指向一个返回同格式 JSON 的地址，每次评估前尝试刷新，失败回退本地文件。
  - 只接受 http/https 源（拒绝 file:// 等本地协议）；响应体上限 2 MiB，超限拒绝；
  - XML 解析拒绝带 DOCTYPE/ENTITY 的文档（实体扩展攻击面收敛）。

## 报告验收规则（validator）

模型输出除原有的来源白名单与中文字段校验外，新增：

- `evidence_fields`（1-20 个）：结论依据的 facts 字段路径列表，分级校验（2026-08-03）：
  - **硬拒**（结构/格式错误）：不是列表、空、超 20 个、字段非字符串或含非法字符——重试更有价值；
  - **降级为 validation_warnings**（引用瑕疵）：路径格式合法但未解析到已提供事实（含列表索引越界）——
    模型猜错路径是常见小错，不再整单拒绝（此前 brief 高频 REJECTED 根因）。
- 数值一致性（方向 ≠ NEUTRAL 时）：
  - entry_zone / take_profit / stop_loss 必须可解析为价格；
  - 几何约束：多头止盈高于入场中点、止损低于入场中点（空头对称）；
  - 幅度约束（快照含 bid 与 ATR 时）：入场中点偏离 bid ≤ 3×ATR，止盈距离 ≤ 5×ATR，止损距离 ≤ 3×ATR。
- 追价形态纪律（scalp，标注不拒绝，宪法第九条第 2 款）：LONG 入场位于 M5 区间高位
  （range_location_8 ≥ 0.65）/ SHORT 位于低位（≤ 0.35）→ 附加 validation_warnings
  （证据：追价 23.1% 胜率 / −0.43R vs 回调 52.6% / +0.21R）。共振相悖维持标注不硬拒。
  2026-08-03 余量修正：追价由硬拒改为标注——低质量建议如实呈现，简报始终可产出。
- 方向建议（入场纪律双子态）：`directional_plan_allowed=true` 时所有门态（除 BLOCKED）
  都要求输出完整方向计划；`=false` 时（点差高位/scalp 亚洲时段）只出观察与等待条件。
  gate.warnings 注入 prompt 并要求 risk_note 逐条覆盖；验收时 `report.gate_warnings`
  （闸门风险标注）与 `report.validation_warnings`（方向纪律标注）都附加到报告，随报告呈现（不阻断验收）。

## 传感器与探针脚本

- tick 健康：`scripts/mt5_xau_tick_health_once.py --symbol XAUUSD --output <file>.jsonl`
- 市场深度探空：`scripts/probe_mt5_dom.py --symbol XAUUSD`
  （2026-07-31 实测：该经纪商 XAUUSD 深度档位为空 → DOM NO-GO）

## 环境变量

- `FRED_API_KEY`：FRED 宏观背景层（缺失时该层显示离线，不影响主流程）。
- `XAU_CONSOLE_CALENDAR_URL`：可选日历 JSON 地址。
- `XAU_CONSOLE_MT5_PYTHON` / `XAU_CONSOLE_MT5_SNAPSHOT_SCRIPT`：覆盖 MT5 解释器与快照脚本路径。

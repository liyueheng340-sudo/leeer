# 外部模型答案 —— 吸收清单（2026-08-02）

> 来源：`docs/xau-external-model-brief-2026-08-02.md` 发给外部模型后，其返回的"接入方案"。
> 本清单只收录经我们核对代码后**确认有增量价值**的 3 点；其余部分（含其给出的系统提示词）明确不吸收，原因见文末。
> 标注约定：【事实】= 代码/数据/文档直接支持；【推断】= 合理延伸；【候选假设】= 未验证。

---

## 总评（一句话）

外部模型的方案**方向对、定位错**：它把我们已落地的 IV 层、关键价位层、双模式当成"待做项"，且其提示词会破坏现有 validator 契约、缩窄英文白名单（40 → 11 个缩写，直接照用会推高 REJECTED 率）。
经逐条核对，真正有增量价值的只有以下 3 点，且其中 2 点与内部审查（docs/xau-console-review-2026-07-31.md 的 A1/A3）重叠。

---

## 改进 1：A1 交易时段上下文 + A3 点差历史分位（事实层增强）

### 来源
外部模型"位置感增强" + 内部审查 A1/A3（重叠确认，双方独立得出同一结论）。

### 现状（事实）
- 已落地：A2 关键价位层 —— `snapshot_facts._key_levels()`（前日高低/当日高低/整数关口/摆动点）已注入 prompt，并要求入场贴近（否则拒绝）。
- 未落地：**交易时段上下文**（facts 无 session_context 字段）。
- 未落地：**点差历史分位**（`tick_health` 只有绝对阈值判断，无相对历史分位）。

### 落地设计
| 项 | 内容 | 计算 | 进闸门？ |
|---|---|---|---|
| A1 交易时段 | `session_context`：当前时段（亚洲/伦敦/纽约）、距伦敦定盘（10:30/15:00 London）、COMEX 开盘（8:20 ET）分钟数 | 纯时间函数 | 否（仅 facts + prompt 上下文） |
| A3 点差分位 | `tick_health.spread_percentile`：spread_median 相对近 N 次任务的分位数（数据在 jobs/runlog 中） | 读历史统计 | 否（仅标注，超标仍由现有 tick 规则转 warnings） |

### 文件改动点（预估）
- 新增 `local_console/session_context.py`（~60 行，纯时间计算，无网络）
- `local_console/facts_builder.py`（或对应 facts 汇聚处）注入两个字段
- `local_console/prompt_rules.py`：注入规则 + 递增 `PROMPT_VERSION`（1.6.0 → 1.7.0）
- `local_console/report_validation.py`：无改动（两字段仅作 evidence_fields 可引用项，不加硬校验）
- `tests/local_console/test_session_context.py` + 现有 facts 测试补丁

### 验证方式
- 单测覆盖时段边界（伦敦定盘切换、周末、冬夏令时）
- 回归 `tests/local_console` 全量（当前基线 203 passed）
- 上线后经 `review_stats` 的 `by_prompt_version` 观察 REJECTED 率无回退

### 风险
- 低。纯确定性计算，零模型成本；不进闸门、不改变 BLOCKED 语义，符合宪法第三条。

---

## 改进 2：用户手动持仓输入 → 持仓管理板块（接口 C 的合规折中）

### 来源
外部模型建议；其价值在于**绕开了"分析层读取执行层持仓"的纪律边界**（接口 C 明确暂不落地）。

### 现状（事实）
- 系统无持仓能力：快照内 `account_trade_reads/writes=FORBIDDEN`；接口 C（读 EA `ng_status.json` 持仓/盈亏）已明确不落地。
- 报告 schema 无持仓相关字段；validator 无持仓板块校验。

### 落地设计
- 前端：简报页新增"持仓信息"输入区（方向/入场价/手数/已持仓时长，可选填写）。
- 后端：任务创建时接受 `position_context`（可选字段，不填则报告不含持仓板块）。
- Prompt：当 `position_context` 存在时，注入持仓管理规则（止损是否移动、分批止盈、剩余持仓时间、减/加/离场），并要求逐项输出。
- Schema：新增**可选**键 `position_management`（数组/对象）；`REQUIRED_KEYS` 不动（无持仓时不得强制要求，否则 REJECTED）。
- Validator：仅当 `position_context` 存在时才要求该板块非空；字段值须能对应到输入（防编造）。
- 纪律边界写死：报告顶部标注"本板块为军师建议，非执行指令"。

### 文件改动点（预估）
- `local_console/jobs.py`（JobRecord 增可选 position_context）
- `local_console/service.py`（透传）、`local_console/brief.py`（prompt 注入 + 解析）
- `local_console/prompt_rules.py`（规则 + 版本递增）
- `local_console/report_validation.py`（条件性校验）
- `local_console/static/`（前端输入区 + 展示）
- `tests/local_console/`（有/无持仓两条路径）

### 验证方式
- 有持仓：报告含完整持仓板块且字段与输入一致；
- 无持仓：报告不含该板块，validator 不拒；
- 回归全量测试。

### 风险
- 中（相对 1 为高）。改动面大（前后端 + schema + validator）；"条件性必填"逻辑若写错会重蹈"缺字段 REJECTED"覆辙。建议严格按上述"仅当输入存在才要求"实现，并用两路径测试锁死。
- 纪律风险：持仓建议不得暗示 EA 执行；需在 prompt 与前端文案双重声明"仅供参考"。

---

## 改进 3：复盘增加 vol_regime 分组维度（测量层）

### 来源
外部模型"复盘加两个分组维度（vol_regime、mode）"；其中 by_mode **已落地**，vol_regime 是新增。

### 现状（事实）
- `review_stats.compute_context_stats` 已有 `by_mode`（scalp/swing）分组（`_mode_key`，review_stats.py:94）。
- 任务 JSON 已落盘 `option_iv`（含 `iv_vs_hv`）——vol_regime 可从既有落盘数据直接分组，**无需改采集**。

### 落地设计
- `review_stats.py` 新增 `_vol_regime_key(record)`：取 `record.gate_payload` 或 facts 中 `option_iv.iv_vs_hv`（high/low/neutral）+ `iv_rank`（样本≥5 时），映射为 `vol_high / vol_low / vol_neutral / vol_na`。
- `compute_context_stats` 增加 `by_vol_regime` 分组输出。
- 前端复盘页增加对应分组展示（可选，数据层先行）。

### 文件改动点
- `local_console/review_stats.py`（~30 行）
- `local_console/review.py` / `review_runs.py`（如需要透出）
- `tests/local_console/test_review_stats.py` 补丁

### 验证方式
- 单测：构造不同 iv_vs_hv 的记录，断言分组正确、缺 IV 落入 vol_na。
- 无行为变更风险（纯新增统计键）。

### 风险
- 低。纯测量层新增；仍遵守纪律：**分组统计 ≠ edge 证据**，前端与文档须保留声明。

---

## 明确不吸收的部分（防误用）

1. **其"系统提示词"整体不采用** —— 模型输出是 JSON 契约（`REQUIRED_KEYS|TRADE_KEYS|SUGGESTION_KEYS`），其"1-7 编号板块"文本模板与 validator 契约不兼容，照用会导致 100% REJECTED。
2. **其英文白名单不采用** —— 现有 `ALLOWED_LATIN_TERMS` 40 个（含 M1/M5/PCE/GLD/ETF/LONG/SHORT/NEUTRAL/MT5/FRED/TIPS 等），其 11 个缩写清单会缩窄白名单、推高 REJECTED 率。
3. **"IV 双维度升级"不采用（已存在）** —— `iv.py` 已产出 atm_iv/skew/iv_vs_hv/iv_rank/term_slope，且已接入 facts 与 prompt（prompt_rules 1.6.0 `_iv_analysis_rule`）。
4. **"swing 纪律强化"不采用（已存在）** —— MODE_RULES + service.set_mode + prompt 按 mode 分叉已在 v1.3.0 落地。
5. **"GARCH 简化版统计波动率预测"不采用** —— 现有 HV20/60 + iv_vs_hv 已覆盖"双维度"需求；引入 GARCH 增加复杂度和维护面，收益未验证【推断】。

---

## 落地优先级建议

| 顺序 | 项 | 理由 |
|---|---|---|
| 1 | 改进 1（A1 时段 + A3 点差分位） | 收益最高、风险最低、改动最小；与内部审查一致 |
| 2 | 改进 3（vol_regime 分组） | 纯测量层，半小时级工作量，为将来积累样本 |
| 3 | 改进 2（手动持仓输入） | 价值明确但改动面大，需先出 schema 设计再动手 |

> 前置事项（独立于本清单）：35 个未提交文件与上游容错改动去留，应先于或并行处理，否则以上改动都叠在未提交基线上。

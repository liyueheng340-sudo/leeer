# XAU Analysis Console（本地 XAUUSD 分析控制台）

自研的只读 XAUUSD 行情分析控制台：定时采集 MT5 快照，经 LLM 生成交易简报，
由本地规则层（军师模式）逐项校验后输出"分析 / 谨慎 / 不交易"的结论，供人工决策。
**本模块不连接账户、不下单、不触碰交易**——全部只读。

> ⚠️ **实验性项目声明**
> 本项目是个人实验性框架，仅供学习与研究参考。简报与校验逻辑基于有限的历史
> 数据回测，**不构成任何投资建议**；LLM 输出具有不确定性，历史表现不代表未来
> 收益。**请勿用于实盘交易**。使用本项目的任何损失由使用者自行承担。

## 架构速览

```
MT5 快照/宏观/新闻/IV ──► 事实层（snapshot_facts）──► 军师闸门（guard）
                                                          │ 只打标签、永不阻断
                                                          ▼
                        LLM 简报（brief.py，军师模式：禁方向禁仓位）
                                                          │
                                                          ▼
                        规则校验（report_validation.py）
                        几何/止损宽度/盈亏比 RR≥1.5/快照对齐
                                                          │
                                                          ▼
                        REJECTED（拒）→ 不交易建议 / ACCEPTED → 交易员自行决策
```

### 风险控制（risk_controls.py）
- **连亏熔断**：连续 ≥4 单亏损后暂停给出交易方向（LOSS_STREAK_THRESHOLD）
- **单日熔断**：同一 UTC 自然日累计亏损达到 -8R（DAILY_LOSS_CIRCUIT_R）后当日不再放行
- 熔断只作用于"建议"，不阻断分析与观察

### 报告校验（report_validation.py）
- 几何校验：方向与止盈/止损/入场区间一致
- 止损宽度：距入场 ≥0.5×ATR（硬下限）；≥1.5×ATR 以下仅警示
- **盈亏比硬校验：止盈距离 ≥1.5× 止损距离（MIN_REWARD_RISK_RATIO=1.5），不达标直接拒绝**

## 环境要求

- Windows（MT5 终端已登录运行，且允许外部 Python 调用 `python.exe` 与 MetaTrader5 包）
- Python 3.12（仓库自带 `.venv` 时自动探测使用；否则 PATH 上的 `python`）
- 依赖：`requirements.txt`（含 MetaTrader5 包——需要能访问 MT5 终端的解释器）

## 快速开始

```powershell
# 1. 克隆后创建虚拟环境并安装依赖
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 配置 .env（从 .env.example 复制；至少需要 LLM 端点）
Copy-Item .env.example .env
#    编辑 .env：TRADINGAGENTS_LLM_BACKEND_URL=... 和模型名

# 3. 启动控制台（默认 127.0.0.1:8767）
.venv\Scripts\python scripts\run_xau_analysis_console.py

# 4. 验证
Invoke-RestMethod http://127.0.0.1:8767/api/status
```

## .env 配置（local_console 相关）

| 变量 | 必填 | 说明 |
|---|---|---|
| `TRADINGAGENTS_LLM_BACKEND_URL` | 是 | OpenAI 兼容 LLM 端点（如阿里云 DashScope compatible-mode） |
| `TRADINGAGENTS_QUICK_THINK_LLM` | 否 | 快模型名（默认 qwen3.7-max） |
| `TRADINGAGENTS_DEEP_THINK_LLM` | 否 | 深模型名（默认 qwen3.8-max-preview） |
| `XAU_CONSOLE_FALLBACK_BACKEND_URL` | 否 | 备用 LLM 端点（主端点额度耗尽时切换） |
| `XAU_CONSOLE_FALLBACK_API_KEY` | 否 | 备用端点密钥 |
| `FRED_API_KEY` | 否 | FRED 宏观数据（利率/通胀），免费申请 |
| `XAU_CONSOLE_AUTO_ENABLED` | 否 | 自主调度开关（默认 false；true 按间隔采样） |
| `XAU_CONSOLE_AUTO_INTERVAL_SECONDS` | 否 | 自主采样间隔（默认 900 秒） |
| `XAU_CONSOLE_MT5_PYTHON` | 否 | 指定装有 MetaTrader5 包的 python.exe（默认自动探测） |
| `XAU_CONSOLE_MT5_SNAPSHOT_SCRIPT` | 否 | 自定义 MT5 快照脚本（默认仓库 scripts/ 内自带） |
| `XAU_CONSOLE_SYMBOL` | 否 | MT5 品种名，默认 `XAUUSD`；经纪商不同命名时覆盖（如 `XAUUSD.s` / `xauusd.s`） |
| `XAU_CONSOLE_EA_STATUS_FILE` | 否 | Cerberus EA 风险态文件路径（默认关闭；配置即开启） |
| `XAU_CONSOLE_EA_STATUS_MAX_AGE` | 否 | EA 状态陈旧阈值秒数（默认 120） |

> 所有密钥只读自 `.env` / 环境变量，仓库内不存储任何凭据。

## HTTP API

| 端点 | 说明 |
|---|---|
| `GET /api/status` | 服务健康与当前配置摘要 |
| `GET /api/auto` | 自主调度状态 |
| `GET /api/mode` | 当前分析模式（scalp/swing） |
| `GET /api/history` | 历史简报与复盘统计 |
| `GET /api/review-stats` | 上下文归因复盘统计（闸门动作×胜率） |
| `GET /` | 控制台首页 |

## 测试

```powershell
.venv\Scripts\python -m pytest tests/local_console -q
```

测试覆盖：闸门规则、报告校验（几何/止损宽度/盈亏比）、双熔断、快照事实、
服务与调度、复盘统计。全部测试不触网、不碰 MT5，可离线运行。

## 目录结构

```
local_console/
  config.py            配置（环境变量 + 自动探测）
  snapshot_facts.py    快照 → 结构化事实
  guard.py             军师闸门（只打标签，永不阻断）
  brief.py             LLM 简报提示词与解析
  report_validation.py 规则校验（含 RR≥1.5 硬校验）
  risk_controls.py     连亏熔断 + 单日 -8R 熔断
  review.py / review_stats.py  历史复盘与统计
  factor_engine.py     技术因子计算
  statistical.py       统计工具（置信区间/Bonferroni）
  server.py / service.py / job_runner.py   HTTP 服务与任务编排
scripts/
  run_xau_analysis_console.py  控制台入口
  mt5_xau_market_context_once.py  只读行情快照采集
  mt5_xau_*_once.py             其余 MT5 只读采集脚本
tests/local_console/   离线测试
```

## 免责声明（再次强调）

本项目为个人学习与实验用途。所有分析输出（包括被规则层"接受"的简报）均不构成
投资建议。实盘交易有重大资金损失风险，作者不对任何使用本项目的后果负责。

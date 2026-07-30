# XAU Analysis Console

启动：

```powershell
D:\XAU\TradingAgents\.venv\Scripts\python.exe D:\XAU\TradingAgents\scripts\run_xau_analysis_console.py
```

浏览器会打开 `http://127.0.0.1:8767`。服务只绑定本机，不向局域网开放。

页面有两个操作：

- 刷新 MT5 并生成简报：创建实时任务，顺序显示读取 MT5、事实校验、Qwen 分析、报告校验和完成状态。
- 深度复盘：使用 Qwen 3.8，流程相同，通常耗时更长。

进度来自服务端持久任务记录。刷新浏览器后会恢复当前任务，不是前端倒计时。

`WATCH` 表示事件上下文未核验，只给观察条件。`WAIT` 表示已知事件窗口，模型不会运行。`BLOCKED` 表示快照不可用、过期或身份不匹配。`REJECTED` 表示模型报告引用了未提供的数据或违反约束。

控制台只调用已有的只读 MT5 行情快照脚本。它没有下单、仓位、平仓、止损止盈、账户设置或 MT5 配置入口。

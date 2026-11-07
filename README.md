# V12 十资产配置与本地纸面执行

## 新增：qmt_gate模拟柜台适配与受限首单（2026-11-07）

`python -B -m v12.execute --help` 为新增柜台入口；与下文8002纸面模拟是两条不同路径。柜台客户端固定模拟8001、策略`v12`，尚未提供实盘8003入口。

- `--dry-run`：只读候选，不提交、撤单或refresh；等开盘及输出前复查本地公告。
- `--trial-send --trial-id <授权ID> --trial-code <国内ETF> --trial-side BUY或SELL`：明确授权后，只准原计划触发的一笔最多100份逻辑试单。部分成交、拒绝或撤单后不换号补单。
- **`--send`是全计划执行，不是一手试单。** 首次试单不得误用。
- 受限授权记录在固定`runtime/trial_authorizations/`，换报告目录或重启不会再发一手；必须保留记录，不通过改ID或删状态重试。
- 每次可能提交的POST前复查本地公告。待核更正、覆盖不完整、条款变化或文件不可读时停止新增；已有成交仍正常对账，业务停止和账本对齐分别报告。
- 目前QDII溢价按最新模拟施工方案仅记录、不拦截。下文旧纸面观察的估值门槛不得误认为柜台策略已有过滤。

禁网离线测试：`python -B scripts/run_offline_tests.py`。本机72项通过；详细运行顺序、现场待核与实盘前最小适配见 [实盘前准备清单](docs/实盘前准备清单.md)。本次代码提交和推送不授权任何柜台写入。

从仓库根运行 `python -m v12 --help`。Python 3.10+，依赖见 requirements.txt。策略代码使用 pathlib，Windows 与 WSL 使用同一份输入、配置和事件协议。数据根目录与源码位置独立。

## WSL 快速运行

以下为本机已验证的环境。代码移入 Linux 原生目录后，将 cd 路径改为新仓库，DATA 继续指向已有研究数据。新环境也可用安装了 requirements.txt 的独立 Python。

```sh
cd /mnt/d/src/new_policy_test/v12_engine_v1
PY=/home/wjc/src/new_polict/.venv/bin/python
DATA=/mnt/d/src/new_policy_test
"$PY" -B -m unittest discover -s tests -v
"$PY" -B -m v12 baseline_replay --data-root "$DATA" --output outputs/my-baseline
"$PY" -B -m v12 event_replay --settings examples/settings.json --events examples/events.jsonl --output outputs/my-events
"$PY" -B -m v12 event_replay --settings examples/settings.json --events examples/events.jsonl --output outputs/my-events --resume
"$PY" -B -m v12 market_probe --code 513100.SH
```

Windows PowerShell 在仓库中使用同一组模块参数。例如：

```powershell
$py='C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -B -m v12 baseline_replay --data-root 'D:\src\new_policy_test' --output outputs/my-baseline
```

## 交易日的只读观察

每次命令在前台读取一次十资产快照并返回。既有状态用 --resume，首次运行使用一个新的输出目录。建议持续维护时把纸面状态放在 WSL 的 Linux 文件系统。market_probe 只读取一个代码的行情，便于单独验证连接。

1. 准备已核对的中国交易日历（每行 YYYY-MM-DD），以及直到前一收盘的总收益、原始收盘价、拆合和分红数据。prepare_session 接受与冻结归档相同布局的数据根。已交付归档截止 2026-11-04，后续交易日需要更新输入。也可由本地数据流程直接提供相同格式的 settings.json、session.json 和 returns.csv。
2. 如下生成当日输入。DAY 和 CAL 指向实际观察日和已确认日历。延续同一账户时增加 --start-day 原开户日期。日历可追加，原有日期、已生效权益和关键配置受恢复校验保护。
3. 在 session.json 的 contracts 填入当日已核验的行情解释。键为真实证券代码，值含 source、valid_date、status_map（原始状态值字符串映射到 TRADABLE/SUSPENDED 等）、depth_multiplier（档位原始数量到份的倍率）、upper 和 lower（当日价格边界）。默认空对象，未知状态或单位输出等待原因。状态映射中的 TRADABLE 应只用于经核实的连续竞价状态。
4. 从零建立当日目标后，多次执行同一命令并加 --resume 可接续消费新快照。session.json 可提供已确认的本地纸面订单状态事件 confirmed_order_events，以及收盘后已核实的十只证券 close_prices。收盘结账后才可处理下一相邻交易日。未决的UNKNOWN和撤单待确认订单继续占用预留。

```sh
"$PY" -B -m v12 prepare_session --data-root "$DATA" --date "$DAY" --calendar "$CAL" --output "inputs/$DAY"
"$PY" -B -m v12 paper_observe --settings "inputs/$DAY/settings.json" --session "inputs/$DAY/session.json" --output runtime/paper
# 后续快照追加 --resume；Windows 宿主的行情服务可按需指定 --host 主机IPv4
```

8002 适配器固定向 /api/v1/tick 发 GET。空盘口、陈旧行情和无可靠规则时不能成交。观察模式的QDII买入阈值为空，持有现金并输出欠配；离线事件可提供 diagnostic 阈值和带估值时间、公布时间、有效期及份额基准的公允价值区间。风险目标、实际风险和现金差异见 risk.json，账户、逐日权益、订单与成交见 state.json 及 CSV。事件模式的限价取当时可执行的对手价，额外冲击费为零，实际模拟填单需要后续合法行情。日线回放单独使用9bp费用。

## 事件协议与恢复

examples/events.jsonl 给出可直接运行的开始、逐证券状态、报价和收盘事件。每条记录含唯一 id、type、带时区的 at。start 的 prices 是参考日实际收盘价，weights 按风险槽位给出。股票代码用于持仓及订单；固定映射和基线参数见 v12/config.py。quote 的 source_id 指源快照版本，exchange_time 是市场时间，at 是本地处理时间；深度单位为份。资金、份额、累计佣金、已见行情和事件收据在同一 SQLite 事务提交。订单查询以 state.json 中 id 为准。order_status 只接受明确纸面事件，停止新计划可使用 stop。已结束账户与未完成委托须以其状态记录交接。

完整验收结果见 IMPLEMENTATION_REPORT.md。

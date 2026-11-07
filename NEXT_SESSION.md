# 下次维护

## 2026-11-07：F1/F2已收口，先做模拟联调

当前工作分支`feat/v12-implementation`已纳入原11个未跟踪的模拟柜台适配文件，以及F1一手授权、F2提交前公告复查和26项定向测试。先读`docs/实盘前准备清单.md`与README顶部新增柜台说明；下面原M0–M3纸面流程仍保留，但不代表柜台/实盘已验证。

本机Windows禁网72项通过（原46＋新26），0失败、0错误、0跳过、0真实网络/邮件尝试。日志在`outputs/f1_f2_merge_20261107/`；可用`python -B scripts/run_offline_tests.py`重跑。此次没有重跑多年历史，没有连接任何行情或柜台服务，没有运行dry-run或试单。

下步顺序：交易时段只读dry-run；用户当次明确授权后，用`--trial-send`指定一只国内ETF、一个方向、一个稳定授权ID，最多一笔100份；之后再核验关键QDII等待/截止/部分成交及跨日账务。普通`--send`仍可能执行十只ETF，不可当作一手测试。保留固定的`runtime/trial_authorizations`，不要换授权ID补单或删除状态。

当前GateClient仅支持模拟8001/策略v12，不支持真实8003。实盘环境、账户/授权状态隔离和实际收费核对须另行最小适配和授权；不是改端口就上线。现有手工513300、旧P75、qmt_gate和其他策略不动。稀有且可见的安全停止允许人工接管，不扩张全面审计和参数优化。

工作分支 feat/v12-implementation。先读 README.md，进入仓库运行 python -B -m unittest discover -s tests -v。路径全部由命令行提供，仓库移入 WSL 后 --data-root 仍可指向 /mnt/d/src/new_policy_test。本机已验证的 Linux Python 为 /home/wjc/src/new_polict/.venv/bin/python。

已有对照账本在 outputs/final/wsl/baseline/。更新数据请建立新的输出目录，冻结 oracle 继续用于原日期回归。外部输入和 outputs/ 不随Git迁移，需要在新位置显式指定或复制自己的 V12 纸面状态。SQLite 迁移前应关闭写入连接并使用一致备份。

下一次有新交易日数据时，准备到前一交易日为止的输入和确认日历，按 README 的 prepare_session 和 paper_observe 命令运行。首次运行新建本地账户，同一账户后续加 --resume。若观察真实市场时尚无经核实的状态、单位、限价边界或估值，查看 observations.json 和 risk.json 记录的等待原因。历史样本末端不能直接冒充新交易日的63日窗口。

已完成M0–M3的离线实施与只读探测。下一项证据是交易时段的只读前瞻观察，之后按日期比对目标现金、实际持仓、等待时间和真实可见盘口。513300静态替代留作可选M4。

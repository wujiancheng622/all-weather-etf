# 下次维护

工作分支 feat/v12-implementation。先读 README.md，进入仓库运行 python -B -m unittest discover -s tests -v。路径全部由命令行提供，仓库移入 WSL 后 --data-root 仍可指向 /mnt/d/src/new_policy_test。本机已验证的 Linux Python 为 /home/wjc/src/new_polict/.venv/bin/python。

已有对照账本在 outputs/final/wsl/baseline/。更新数据请建立新的输出目录，冻结 oracle 继续用于原日期回归。外部输入和 outputs/ 不随Git迁移，需要在新位置显式指定或复制自己的 V12 纸面状态。SQLite 迁移前应关闭写入连接并使用一致备份。

下一次有新交易日数据时，准备到前一交易日为止的输入和确认日历，按 README 的 prepare_session 和 paper_observe 命令运行。首次运行新建本地账户，同一账户后续加 --resume。若观察真实市场时尚无经核实的状态、单位、限价边界或估值，查看 observations.json 和 risk.json 记录的等待原因。历史样本末端不能直接冒充新交易日的63日窗口。

已完成M0–M3的离线实施与只读探测。下一项证据是交易时段的只读前瞻观察，之后按日期比对目标现金、实际持仓、等待时间和真实可见盘口。513300静态替代留作可选M4。

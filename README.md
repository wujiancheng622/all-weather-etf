# V12 独立工程工作区

当前交付阶段：**仅设计与施工交接，尚未实现V12执行代码**。

原研究总目录与旧P75目录均无Git仓库。本目录因此单独建立Git仓库，保留`main`初始化点，在`feat/v12-implementation`分支放置本次设计与后续实施。不是对旧P75代码建立了已经提交的历史分支，也不是已经迁移旧账户。

旧P75只读参考：`D:\src\new_policy_test\p75_engine_v1`。
本目录：`D:\src\new_policy_test\v12_engine_v1`；WSL映射为`/mnt/d/src/new_policy_test/v12_engine_v1`。

实施者首先阅读 `docs/V12详细改造设计.md` 与 `docs/施工提示词.md`。
有关513100与513300的差异，阅读 `docs/同指数QDII_ETF选用说明.md`。

默认只做离线回放及8002只读行情模拟，不连接8001，不发真实或模拟柜台委托，不启动后台服务，不改变旧P75。

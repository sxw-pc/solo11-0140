# 校园食堂留样采录台

本项目为食堂食品安全员提供纯离线的菜品批次、留样容器和冷柜温控证据台账。仓内所有人名、原料和时间均为合成示例，不连接真实学校或外部服务。

## 运行

Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m mealhold demo --db demo.sqlite
python -m pytest -q
```

独立入口：

```powershell
python -m mealhold meal --db sample.sqlite --input fixtures/meal-ok.json
python -m mealhold sample --db sample.sqlite --input fixtures/sample-ok.json
python -m mealhold cold --db sample.sqlite --input fixtures/cold-ok.json
python -m mealhold exposure --db sample.sqlite --input fixtures/exposure-ok.json
python -m mealhold report --db sample.sqlite
```

每个子系统有 `*-conflict.json` 反例；`source` 与 `source_conflict` 保存原始 JSON 和 SHA-256，`decision.previous` 保留前次裁定 ID。每条导入在 SQLite 事务内写入，重复来源不重复产生决定。

## 三项独立决定

菜品子系统根据签发版次、召回批次和过敏原清洗证据决定可供餐状态，并根据供餐人数确定最低留样净重（每 100 人 125g，向上取整）。留样子系统依据毛重与皮重、两小时采样窗口、封签和双方签名决定容器是否有效；冷柜子系统根据大于 30 分钟的记录缺口、持续至少 15 分钟高于 4°C 的超温和双人销毁授权做出独立决定。

三个跨系统反例：原料召回使原本合格容器转事故保留且禁止销毁；人数从 80 增至 120 后，130g 净重从合格转不足，而温控仍合格；冷柜超温使完好封签容器转隔离。报告保留原始事件、当前状态和撤回链。

## 就餐暴露归属与两种通知合同

暴露子系统采录批次出餐时窗、出餐口、可核餐份编号、匿名就餐票的领餐时窗与窗口，以及带来源的扫码或人工交付记录。只有时窗、窗口与份号证据都相容才能把一票连到一份；归属严格一对一（一份不分两票、一票不领两份普通餐），无法识别的票保持未知而不按导入行序猜批次。系统枚举所有与证据相容的最大完整归属，对每张票判定必然、可能、不可能、未知或身份冲突：一份餐被两票同时认领或票的来源冲突时，相关票保持待核并说明身份冲突，不任选最早扫码。

确证通知只发给在所有可行归属中都接触召回批次的队列，可能接触的队列列为待核；预防通知对必然与可能接触的队列都发带“不确定”说明的提醒，不把可能接触说成已确证。两合同读取相同证据，归属歧义未解时清单实际不同。通知只用当天一次性队列代号，不储存真实学生姓名或跨天追踪标识。后到的原料召回或解除证据会改变暴露类别并产生新版通知，旧版通知、所用菜单版次与扫码来源永不改写；重放同一来源不重复产生归属或通知。`fixtures/exposure-ok.json` 与 `fixtures/exposure-scan.json` 是可运行反例：两张票覆盖召回与正常两份餐时确证通知暂不发、预防通知标可能，独立扫码证明 T2 领取正常份后，一对一约束使 T1 必然接触召回份，确证通知只发 T1 所在队列。

本版不实现真实设备接入、多校区转运、复检拆样、停电争议回放、供应商替换、多探头互证、抽检配额、监管在线查询、真实学生身份或跨天追踪。

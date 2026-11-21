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
python -m mealhold report --db sample.sqlite
```

每个子系统有 `*-conflict.json` 反例；`source` 与 `source_conflict` 保存原始 JSON 和 SHA-256，`decision.previous` 保留前次裁定 ID。每条导入在 SQLite 事务内写入，重复来源不重复产生决定。

## 三项独立决定

菜品子系统根据签发版次、召回批次和过敏原清洗证据决定可供餐状态，并根据供餐人数确定最低留样净重（每 100 人 125g，向上取整）。留样子系统依据毛重与皮重、两小时采样窗口、封签和双方签名决定容器是否有效；冷柜子系统根据大于 30 分钟的记录缺口、持续至少 15 分钟高于 4°C 的超温和双人销毁授权做出独立决定。

三个跨系统反例：原料召回使原本合格容器转事故保留且禁止销毁；人数从 80 增至 120 后，130g 净重从合格转不足，而温控仍合格；冷柜超温使完好封签容器转隔离。报告保留原始事件、当前状态和撤回链。

## 餐后不适调查预警（双合同）

投诉子系统不再按报告条数直接告警。每份报告保留报送者当日一次性代号、家庭代号、症状起止估计区间、报告时刻、就餐批次声称与独立核对来源；相同来源重放幂等并记入 `complaint_exclusion`。身份只凭可信同人凭证合并（同户不并人），互相矛盾的凭证不按先后或多数票取舍，而是保留全部极大一致解释（`identity_credential`、`identity_link`、`alert_world`）。症状区间与菜品事先登记的观察窗（`observation_window`，随餐次导入）完全落入为确定相关、仅相交为可能相关，迟到的时刻核验（`complaint_time_check`）可把可能改判为确定或无关；相邻餐次的报告各自保留原始声称，绝不跨餐次挪用。

快速哨点合同在每种可行解释都至少有 3 名不同人可能落窗时发临时核查通知，注明身份或时刻未定，不宣称已形成独立聚集；正式聚集合同要求在每种可行解释中都至少有两个不同家庭、各有一名确定落窗且餐次票据经独立核对（`complaint_ticket_check`）的报告人，才签发正式调查单。每次重估写入 `complaint_alert` 新版次（人数与独立家庭数的可行上下界、被排除的重复来源），旧版保留可查。预警只决定调查是否成立，不认定菜品致病，也不改写 `meal_batch` 的供餐或召回状态。

```powershell
python -m mealhold complaint --db sample.sqlite --input fixtures/complaint-ok.json
python -m mealhold complaint --db sample.sqlite --input fixtures/identity-roster.json
```

本版不实现真实设备接入、多校区转运、复检拆样、停电争议回放、供应商替换、多探头互证、抽检配额或监管在线查询。

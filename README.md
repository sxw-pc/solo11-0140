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

本版不实现真实设备接入、多校区转运、复检拆样、停电争议回放、供应商替换、多探头互证、抽检配额或监管在线查询。

## 增强起点：供应商原料声明

`python -m mealhold supplier --db sample.sqlite --input fixtures/supplier-ok.json` 采入原料批次的供应商过敏原声明。先导入 `meal-ok.json`，再运行 `python -m mealhold label --db sample.sqlite --input fixtures/label-query.json`，可得到当前菜品的直接原料声明汇总；缺少声明时为 `pending`。`report` 能看到声明原文来源和标签草稿。当前只读取同一批次最新一份直接声明，不处理复合原料的子成分、供应商替代品承继、不同交付场景的标签签发或声明冲突。

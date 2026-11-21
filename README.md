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
python -m mealhold line --db sample.sqlite --input fixtures/line-ok.json
python -m mealhold portion --db sample.sqlite --input fixtures/disposition-ok.json
python -m mealhold closeout --db sample.sqlite --input fixtures/closeout-ok.json
python -m mealhold registrations --db sample.sqlite --input fixtures/registrations-ok.json
python -m mealhold servable --db sample.sqlite --meal lunch-1
python -m mealhold preview --db sample.sqlite --meal lunch-1
python -m mealhold report --db sample.sqlite
```

每个子系统有 `*-conflict.json` 反例；`source` 与 `source_conflict` 保存原始 JSON 和 SHA-256，`decision.previous` 保留前次裁定 ID。每条导入在 SQLite 事务内写入，重复来源不重复产生决定。

## 三项独立决定

菜品子系统根据签发版次、召回批次和过敏原清洗证据决定可供餐状态，并根据供餐人数确定最低留样净重（每 100 人 125g，向上取整）。留样子系统依据毛重与皮重、两小时采样窗口、封签和双方签名决定容器是否有效；冷柜子系统根据大于 30 分钟的记录缺口、持续至少 15 分钟高于 4°C 的超温和双人销毁授权做出独立决定。

三个跨系统反例：原料召回使原本合格容器转事故保留且禁止销毁；人数从 80 增至 120 后，130g 净重从合格转不足，而温控仍合格；冷柜超温使完好封签容器转隔离。报告保留原始事件、当前状态和撤回链。

本版不实现真实设备接入、多校区转运、复检拆样、停电争议回放、供应商替换、多探头互证、抽检配额或监管在线查询。

## 增强起点：工位开餐前检查

`python -m mealhold line --db sample.sqlite --input fixtures/line-ok.json` 可采录一餐所在供餐线的工位检查。每个工位都要有检查通过标记、清洗单号和两名不同检查人，才把该餐的供餐线记为 `ready`。`report` 会列出原始检查和当前 `line_case`。当前检查只看本餐上报的工位清单，不追溯此前菜品在共用设备上的使用与清洗先后，也未将供餐线结论接入菜品放行。

工位结论按工位累积：后到的独立签检若把某一工位判为 `hold`，该餐 `line_case` 随之转 `hold`，但先前已签的菜单版次、人数和留样证据原样保留。途经该工位的份号仍保留实物去向，只是在 `servable` 中不再计为已核实可供餐。

## 实物去向封账

`src/mealhold/disposition.py` 建立逐份实物台账，回答的唯一主问题是：**这一生产批次能否结清，以及有多少份下落未明**。检验结论和菜单自报人数都不替代实物去向。

- 每份餐在生产时只编号一次（`meal_portion`），`plated`/`returned`/`rework`/`split`/`merge`/`served`/`destroyed`/`stock` 事件都引用原份号（`portion_event`）。返工只改变同一份号的状态，不产生第二次已出餐计数。
- 拆盒以 `split` 事件登记父子份号及各自实际质量（`portion_edge.mass_g`），子盒按质量比例继承份数；封账只数叶份号，父份被拆即消耗，不会父子同时计数。合并以 `merge` 事件写明哪些原份号消耗于新份。去向链有环即拒绝写入，返工环不能凭空生成数量。
- 只有终点为 `served` 且存在独立出餐口签收（`serving_receipt`，签字人非空、签名非空）的份号计为已送出；同一份号多条签收只计一次。销毁要有两名不同签字人，且 `declared_qty` 与 `destruction_item` 列出的份号数一致。找不到签收的份号既不当已送出，也不当销毁，而是留在未明清单。
- 拆盒或合并若把父份全部质量分给子盒，则父份不再计入；剩余质量（`residual`）作为未明份数保留。`produced = served + destroyed + stock + unknown` 始终守恒。

### 两种事先批准的封账合同

`mealhold.disposition.closeout` 以 `contract` 参数选择其一，且同一餐封账后不得事后更换合同：

- `portion`（逐份核销）：每个生产份号都要沿无环、守恒的去向链落到已签收出餐、双签销毁或可点数结存之一；任何未明份号阻止结账。
- `limit`（限额异常）：允许最多一份未明，但必须有两名不同值班员针对该确切份号签署盘点差异（`count_discrepancy`）。封账单仍列出该份号，不计为供餐或销毁。未明超过一份或缺有效双签，仍不能封账。

`preview --meal` 对同一份账同时给出两种合同的结账决定与待追责份数，二者不会相同。`report` 的 `disposition` 汇总给出 `produced/served/destroyed/stock/verified_servable/unverified/unknown` 与最近一次 `closeout`。

### 可运行样例

`fixtures/disposition-ok.json` 为 P001–P100：P001–P090 直接出餐，P091–P100 先退回，P091–P098 返工后各自再出餐，P099/P100 双签销毁。完整账是 **98 份已送出、2 份销毁**，8 份返工不会被当成额外生产（不是 108）。`fixtures/disposition-missing-receipt.json` 去掉 P098 的出餐口签收但保留返工记录和两名值班员的盘点差异：逐份合同待核，限额异常合同可封账并标出已证实出餐 97、销毁 2、未明 1。`fixtures/disposition-half-box.json` 再把 P098 拆成两个各 125g 的半盒、只签收一个：计入 97.5 份出餐、0.5 份未明，父份与半盒不重复计数。`fixtures/line-late-hold.json` 补报使 `wok-1` 转 `hold` 的独立检查：实物总量仍守恒，但 P091–P098 从已核实可供餐中剔除，封账资格按实际经过该工位的份号重裁，原菜单签发不改写。

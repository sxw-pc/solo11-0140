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

本版不实现真实设备接入、多校区转运、复检拆样、停电争议回放、多探头互证、抽检配额或监管在线查询。

## 增强起点：供应商原料声明

`python -m mealhold supplier --db sample.sqlite --input fixtures/supplier-ok.json` 采入原料批次的供应商过敏原声明（`present` 含有、`may_contact` 可能接触、`absent` 明确不含）。先导入 `meal-ok.json`，再运行 `python -m mealhold label --db sample.sqlite --input fixtures/label-query.json`，可得到当前菜品的直接原料声明汇总；缺少声明时为 `pending`。`report` 能看到声明原文来源和标签草稿。

## 复合原料组成、替代证明与标签签审

复合酱料等原料需要逐层证据，不能凭外层商品名沿用旧标签：

- `python -m mealhold composition --db sample.sqlite --input fixtures/composition-ok.json` 采录组成证据。每条组成写明父产品、子批次、用量与声明来源（`declared_by`），可附清洗证明 `cleaned`。一个父产品可有多条组成（一对多）；同一子批次经两条路径到达只算一份来源。
- `python -m mealhold substitution --db sample.sqlite --input fixtures/substitution-ok.json` 采录替代证明。证明须限定覆盖的子成分（`covers_components`）与过敏原类别（`covers_allergens`），并由两名不同签核人签发，否则记为无效且不产生任何承继。替代品只承继被覆盖范围的结论，未覆盖的子成分保持未证实。
- `python -m mealhold sign --db sample.sqlite --input fixtures/sign-query.json` 对当前标签草稿做一次签审。两份事先确定的交付委托：`school-window`（校内窗口，证据不完整时明示“可能接触”即可发布普通警示标签）与 `sesame-zero-contact`（对外供餐，要求所有实际子成分明确不含芝麻且有清洗证明，缺一项即待核）。每次签审追加一个不可改写的签审版本，新证据不会悄悄改动旧版本。

标签核证沿实际供餐版次的原料逐层展开，区分“含有”“可能接触”“尚未证实”三种状态，尚未证实不当作不含。两份有效声明对同一子批次给出相反结论、或组成关系形成循环时，标签转 `review` 并在 `meal_label.detail` 中指出冲突范围；无关菜品的标签不受影响。子批次被召回或被证明含有原配方未声明的过敏原时，供餐裁定随之转为 `recalled`/`allergen_hold`（干净的清洗记录不能解除召回）；菜品当前版次同时限定哪份标签草稿可签。`report` 的 `label_trace` 列出每条标签声明对应的子成分路径与签核来源，`label_signoff` 保存两类委托各自的签审历史。

完整示例见 `fixtures/meal-s2.json`、`composition-s.json`、`composition-ok.json`、`substitution-ok.json`、`supplier-soy-base.json`、`supplier-sesame-absent.json`、`supplier-sesame-may.json`、`sign-query.json`、`sign-zero.json`：旧酱料 S 的大豆底料证明经替代证明部分承继到新酱料 S2，芝麻辅料只剩可能接触声明，校内普通警示标签可签发（含大豆、可能接触芝麻），对外零接触标签必须待核。


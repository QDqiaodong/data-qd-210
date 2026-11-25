# data-qd-210 · 档案引用谱系

从档案文献与引用边建立稳定谱系、根文献、深度及循环证据；不做全文检索。

本仓主要交付是Python业务API和可供下游使用的结构化数据，不是通用CLI、报表或CRUD模板。字段、身份、单位、上限与失败边界见 [数据契约](docs/contract.md)。

## 安装与验证

Python >=3.11，实测CPython3.12.14、uv0.12.19。运行依赖仅标准库，构建固定setuptools80.9.0、wheel0.45.1；每仓独立 `.venv`。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
```

上述命令已在本地缓存和独立环境真实执行；样例输出写入 `run/sample.json`，已有目标拒绝覆盖。API可这样调用：

```python
from citation_lineage import process
from citation_lineage.io import load, publish
publish("run/another.json", process(load("fixtures/input.json")))
```

样例摘要：

```json
{"citations": 2, "cycles": 0, "documents": 3, "max_depth": 2, "roots": 1}
```

## 引用修订流水线（`revise`）

`revise(data, batches)` 在已发布引用关系之上按顺序应用修订批次，每批原子
生效，并为初始版本与每个已接受版本保留完整的 `process` 谱系结果、逐文献
`document_depths`、实际新增/撤销定位与相对前一版本的影响集合。批次校验、
重放/身份冲突、跨循环边界深度与失败隔离规则见
[修订契约](docs/revision.md)。

```python
from citation_lineage import RevisionError, revise

try:
    outcome = revise(data, batches)
except RevisionError as exc:
    outcome = exc.result   # 已接受版本、failure 与 unprocessed_batches
```

合成演示（含一次故意构造的原子拒绝）：

```sh
python -I examples/run_revision.py
```

已通过 151 项业务测试（含 `process`、修订流水线、目录归并、双时间快照、离线移交编排与处置评估），并核对非法输入拒绝；样例数据
全部原创合成，见 [来源](fixtures/SOURCES.md)。

## 多来源目录归并（`consolidate`）

`consolidate(catalogs, identity_claims)` 用显式 `same` / `different` 声明把多个
合作方的本地文献和引用投影为一份规范目录。它会保留别名、逐字段来源观察、选择
依据、身份/字段冲突分量、阻断引用、内部引用证据与多来源聚合定位，并在
`process` 字段中返回现有谱系结果；没有可发布文献时该字段为 `null`。规则、输出
结构、上限与确定性保证见 [归并契约](docs/consolidation.md)。

```python
from citation_lineage import consolidate, materialize

outcome = consolidate(catalogs, identity_claims)
lineage = outcome["process"]  # 无可发布文献时为 None/JSON null
```

## 双时间发布快照（`materialize`）

`materialize(data, assertions, queries)` 在固定档案目录之上，按业务时间与
知识时间两个独立时间轴回答审计问题：某个业务时点哪些材料有效，以及截至某个
知识截止时间已经掌握哪些声明。它只让 `recorded_at <= known_at` 的声明可见，
在同实体的纠正 DAG（`supersedes`）上求当前前沿，再按左闭右开的业务区间判定
`available` / `withdrawn` / `unknown` / `conflict`，绝不按记录时间、ID 或
输入顺序对冲突擅自决胜。后来补录、业务生效更早的撤回或纠正不会污染旧知识截止
的快照。

每个快照给出文献与引用的当前状态及前沿/覆盖声明证据、规范 `documents` /
`citations`、端点阻断与引用自身撤回/缺证/冲突的分类证据，以及现有 `process`
对规范目录计算的完整结果；没有可发布文献时 `process` 为 `null` 但审计证据
完整。字段、纠正 DAG 校验、输出结构、确定性与性能边界见
[双时间快照契约](docs/snapshot.md)。

```python
from citation_lineage import MaterializationError, materialize

try:
    result = materialize(data, assertions, queries)
except MaterializationError as exc:  # ValueError 子类，整次调用失败
    ...
snapshots = result["snapshots"]      # 按 query_id 稳定排序
registry = result["assertions"]      # 完整声明注册表，快照按 ID 引用
```

## 离线档案移交编排（`prepare_handoff`）

`prepare_handoff(data, request, acknowledgements)` 从 seed 集合计算
`cited_ancestors` 引用闭包，把每个强连通分量作为不可拆分的原子单元，按成员 ID
字典序和文献数/字节双容量限制生成确定性批次，并依据 manifest 匹配的连续回执
前缀决定已确认文献、引用与 `next_batch`。超限原子单元返回 `orderable=False`
和结构化 `violations`；非法回执抛 `ValueError`。批次摘要使用规范紧凑 JSON 的
SHA-256，调用方可独立复算。闭包、装箱、回执与 manifest 规则见
[移交契约](docs/handoff.md)。

```python
from citation_lineage import prepare_handoff

handoff = prepare_handoff(data, request, acknowledgements)
next_batch = handoff["next_batch"]
```

## 保管期限与引用证据联动的处置评估（`assess_disposition`）

`assess_disposition(data, records, holds, request)` 把保管期限、法律保全（hold）
与引用证据依赖合并成一个可审计的处置决定：给定一批拟销毁文献和评估时点
`as_of`，逐候选给出稳定决定及**全部**直接原因，并返回处置后的规范目录与
`process` 谱系。

文献自身过期不代表可以处置：所有非候选文献，以及候选中永久保留、尚未过期或
处于 active hold 的文献构成保留锚点，每个锚点沿引用方向可达的全部被引文献都须
保留。该判断针对整次请求共同完成——一个被保管期或 hold 阻止的候选会成为锚点并
继续保护它引用的候选，跨级引用链与循环同样服从闭包规则。每个引用证据阻断都给
出确定性的最短定位见证（最小可达锚点 + 字典序最小的最少边定位序列），保留实际
页码。字段、含截止日口径、失败边界与确定性保证见
[处置契约](docs/disposition.md)。

```python
from citation_lineage import DispositionError, assess_disposition

try:
    result = assess_disposition(data, records, holds, request)
except DispositionError:  # ValueError 子类，整次调用失败
    ...
allowed = result["allowed_document_ids"]
process_after = result["process_after"]  # 全部处置时为 None
```

```sh
python -I examples/run_disposition.py
```

## 固化副本修复规划（`plan_preservation`）

`plan_preservation(data, assets, sites, replicas, request)` 在规范引用图之上，
为一组重点文献及其沿引用方向可达的全部被引证据，共同求解满足故障域分散要求的
数字载体修复计划。副本状态只由本次输入确定：站点 `online` 且观测摘要等于登记
摘要才是 `healthy`，摘要不一致为 `corrupt`，站点离线或观测摘要为 `null` 为
`unavailable`；不做多数表决，corrupt/unavailable 副本不能作为读取源但可作为
`replace` 目标。

每个仍缺的故障域恰好规划一个动作，目标已有损坏/不可读记录时为 `replace`、否则
为 `create`；已有 healthy 域不消耗站点 slot。规划对整个保护闭包共同求解，站点
`write_slots` 是所有文献动作共享的全局容量——逐文献贪心可能让局部低成本站点耗尽
后续文献唯一可用站点的 slot，因此实现使用向量费用最小费用流：先最小化总
`write_cost`，再按完整动作键字典序唯一决胜。无 healthy 源、单篇故障域不足或全局
slot 容量不可行时返回 `status="blocked"` 与结构化 `violations`，绝不给出部分动作。
字段、副本状态、阻断分类与确定性保证见 [固化副本契约](docs/preservation.md)。

```python
from citation_lineage import PreservationError, plan_preservation

try:
    plan = plan_preservation(data, assets, sites, replicas, request)
except PreservationError:  # ValueError 子类，整次调用失败
    ...
if plan["status"] == "planned":
    actions = plan["actions"]
    total_cost = plan["total_write_cost"]
```

```sh
python -I examples/run_preservation.py
```

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。

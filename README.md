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

已通过 112 项业务测试（含 `process`、修订流水线、目录归并、双时间快照与离线移交编排），并核对非法输入拒绝；样例数据
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

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。

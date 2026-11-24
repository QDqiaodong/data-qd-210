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

已通过 46 项业务测试（含修订流水线 39 项），并核对非法输入拒绝；样例数据
全部原创合成，见 [来源](fixtures/SOURCES.md)。

## 多来源目录归并（`consolidate`）

`consolidate(catalogs, identity_claims)` 用显式 `same` / `different` 声明把多个
合作方的本地文献和引用投影为一份规范目录。它会保留别名、逐字段来源观察、选择
依据、身份/字段冲突分量、阻断引用、内部引用证据与多来源聚合定位，并在
`process` 字段中返回现有谱系结果；没有可发布文献时该字段为 `null`。规则、输出
结构、上限与确定性保证见 [归并契约](docs/consolidation.md)。

```python
from citation_lineage import consolidate

outcome = consolidate(catalogs, identity_claims)
lineage = outcome["process"]  # 无可发布文献时为 None/JSON null
```

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。

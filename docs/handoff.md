# 离线档案移交契约（`prepare_handoff`）

`prepare_handoff(data, request, acknowledgements=None)` 在现有
[`process`](contract.md) 谱系结果上计算指定 seed 的引用闭包、强连通原子单元、
满足双容量限制的确定性批次，以及回执确认后的移交状态。

```python
from citation_lineage import prepare_handoff

result = prepare_handoff(data, request, acknowledgements)
assert result["orderable"]
next_batch = result["next_batch"]       # 前缀后的第一批；全部确认时为 None
```

本功能只使用 Python 标准库，不访问网络、数据库或持久化服务，也不会修改调用方
传入的任何对象。

## 输入

### `data`

必须完全符合 [`process`](contract.md) 的输入契约。函数内部调用 `process(data)`
完成文献、引用、ID、文本和数量上限校验。

### `request`

`request` 必须且只能包含以下字段：

| 字段 | 约束 |
| --- | --- |
| `handoff_id` | 非空字符串；与回执中的身份精确匹配 |
| `seed_ids` | 1..100 个唯一 ID，且每个 ID 必须存在于 `data` |
| `payloads` | 覆盖 `data` 中**全部**文献的 payload 列表 |
| `max_documents_per_batch` | `1..1000` 的整数（布尔值不合法） |
| `max_bytes_per_batch` | `1..1_000_000_000` 的整数 |
| `purpose` | 非空字符串 |

每个 payload 必须恰好对应一篇 `data` 文献：

```json
{
  "document_id": "d1",
  "sha256": "64 位小写十六进制摘要",
  "size_bytes": 123
}
```

输入也接受 `"id"` 作为文献 ID 字段名；规范化输出同时给出 `id` 与
`document_id`。`sha256` 必须匹配 `[0-9a-f]{64}`，`size_bytes` 必须是
`1..100_000_000` 的整数。payload 缺失、重复、未知文献、摘要格式错误或大小越界
均抛 `HandoffError`（`ValueError` 子类）。

payload 必须覆盖整个 `data` 目录，而不只是本次 seed 闭包；批次中只装入闭包内
文献的 payload。

### `acknowledgements`

回执列表可为 `[]`。每项必须且只能包含：

```json
{
  "handoff_id": "handoff-1",
  "batch_number": 1,
  "manifest_sha256": "<批次摘要>",
  "received_at": "非空字符串"
}
```

回执必须确认从第 1 批开始的连续前缀。错 `handoff_id`、错摘要、重复批号、缺口、
越界或同批内容冲突都会在任何状态返回前抛出 `ValueError`。列表顺序没有语义；
结果中的有效回执按批号排序。相同前缀可在后续调用中反复传入，计划和 manifest
不因此改变。

## 闭包、引用与原子单元

移交文献集合为：

```text
{seed_ids} ∪ ⋃ process(data)["cited_ancestors"][seed]
```

因此与任一 seed 可达关系无关的文献和引用不会进入移交计划。闭包内引用保留全部
`from` / `to` / `page` 定位；同一文献对的多个页码分别保留，但依赖图按去重后的
文献边 `(from, to)` 计算。

闭包子图中的每个强连通分量（SCC）是不可拆分的原子单元：

- 大小大于 1 的 SCC 中所有文献必须在同一批次；
- 不在循环中的每篇文献各自构成单文献单元；
- 单元字节数为其全部 payload `size_bytes` 之和。

SCC 使用迭代 Kosaraju 算法计算，复杂度为 `O(V + E)`，不递归遍历千级链，也不
枚举两两文献或全部简单环。

若任一原子单元自身的文献数超过 `max_documents_per_batch`，或字节数超过
`max_bytes_per_batch`，返回：

```json
{
  "orderable": false,
  "status": "unorderable",
  "plan": null,
  "violations": [
    {
      "unit_id": "unit_1",
      "document_ids": ["a", "b"],
      "document_count": 2,
      "total_bytes": 12,
      "triggered_limits": ["max_documents_per_batch", "max_bytes_per_batch"]
    }
  ],
  "next_batch": null
}
```

`violations` 会列出全部超限单元、排序后的成员、实际文献数/字节数以及触发的
限制。不可编排结果不包含部分批次计划，也不接受任何回执。

## 确定性装箱

原子单元按其排序后的成员 ID 列表做字典序排序。单元间依赖方向为：若文献 `a`
引用闭包内另一原子单元中的文献 `b`，则 `a` 所在单元依赖 `b` 所在单元。

每一批：

1. 在批开始时枚举所有外部依赖已经在更早批次安排、且尚未安排的单元；
2. 按成员 ID 列表字典序考察；
3. 若加入后同时满足文献数和字节数上限则装入；
4. 若当前容量放不下则跳过，继续考察后面的单元；
5. 不再有单元可装入时封存批次；
6. 下一批重新考察全部依赖已满足的未安排单元。

外部单元依赖必须位于更早批次；同一批次内共同交付只用于同一原子单元（尤其是
循环）内部的引用。所有原子单元单独通过上限检查后，空批次一定能装入至少一个
单元，因此算法必然推进到覆盖完整闭包。

## 批次与 manifest

`plan["batches"]` 中每批至少包含：

| 字段 | 含义 |
| --- | --- |
| `batch_number` | 从 1 开始的连续批号 |
| `document_ids` | 本批全部文献 ID，按 ID 排序 |
| `documents` | 按 ID 排序的完整 `{id,date,title,payload}` 文献 |
| `payloads` | 与 `document_ids` 对齐的 payload；`documents[*].payload` 是同一数据的内嵌形式 |
| `citations` | 本批 `from` 文献新交付的全部闭包引用定位 |
| `document_count` | 本批文献数 |
| `total_bytes` | 本批 payload 字节数总和 |
| `manifest_sha256` | 规范批次内容摘要 |

引用定位在其 `from` 文献所在批次交付。此时其 `to` 文献要么在更早批次，要么
属于同一原子单元并在同批交付。

manifest 的独立复算规则为：取批次对象，仅删除 `manifest_sha256` 字段，然后：

```python
canonical = json.dumps(
    unsigned_batch,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
manifest_sha256 = hashlib.sha256(canonical).hexdigest()
```

可直接使用
`citation_lineage.handoff.manifest_sha256(batch)` 复算。

## 计划与确认状态

可编排结果的 `plan` 包含：

- `seed_ids`；
- `closure` 以及扁平的 `closure_document_ids` / `closure_documents` /
  `closure_citations`；
- `atomic_units`：成员、计数、字节数、循环标记和依赖；
- `unit_dependencies`：单元依赖及去重文献边证据；
- `batches` 与 `batch_count`；
- `process` / `full_process`：对完整 `data` 调用现有 `process` 得到的完整谱系
  结果；
- `closure_process`：仅对闭包文献和闭包引用运行 `process` 的完整结果，便于在
  不引入闭包外材料的情况下复核移交子图。

顶层状态字段包括：

- `acknowledgements`：排序后的有效回执；
- `confirmed_batch_numbers` / `unconfirmed_batch_numbers` /
  `pending_batch_numbers`：连续批号列表；
- `confirmed_batches` / `unconfirmed_batches` / `pending_batches` 以及对应的
  `*_details`：完整批次映射（可直接读取 `manifest_sha256` 和文献），这些引用
  对象与对应批号整数相等，因此 `confirmed_batches == [1]` 也成立；
- `confirmed_document_ids`、`confirmed_citations`；
- `pending_document_ids`、`pending_citations`；
- `confirmed_documents` / `pending_documents` 及对应 `*_details` 是带 payload
  的完整文献映射，并与文献 ID 字符串相等；
- `next_batch`：确认前缀后的第一个完整批次；全部确认时为 `null`。

确认批号 `1..k` 后，只有这些批次中的 `from` 文献所对应的引用定位属于已确认
引用；`next_batch` 是第 `k+1` 批。因此新增、删除或修改一条有效回执会真实改变
已获得交付资格的文献/引用集合以及下一批内容，而不只是改变展示字段。

## 确定性与规模

文献顺序、引用书写顺序、seed 顺序、payload 顺序和回执顺序被打乱时，规范化后的
闭包、单元、依赖、批次边界、manifest 和确认状态完全相同。

实现使用图遍历和 SCC，不通过两两比较文献构造关系；在 `process` 基线的
1000 篇文献、5000 条引用规模下可迭代运行。整个返回值只包含可被
`json.dumps(..., allow_nan=False)` 序列化的标准 JSON 类型。

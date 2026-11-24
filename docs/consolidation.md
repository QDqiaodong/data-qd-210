# 多来源档案目录归并契约（`consolidate`）

`consolidate(catalogs, identity_claims)` 将多个合作方的本地文献与引用目录，
以及显式身份声明，归并为可交给 [`process`](contract.md) 的规范目录。

归并只使用显式的 `same` / `different` 声明：不做标题相似度、日期推断、网络或
外部档案查询。函数不会修改 `catalogs` 或 `identity_claims`；任何输入错误均抛出
`ValueError`（具体类型为 `ConsolidationError`，业务上限为
`ConsolidationLimitError`，二者都是 `ValueError` 子类），不返回部分结果。

## 输入

### `catalogs`

必须是 1..50 个来源对象，每个对象只能包含：

| 字段 | 约束 |
| --- | --- |
| `source_id` | 字符串，所有来源中唯一；与 `local_id` 拼成的 `source_id:local_id` 必须通过现有文献 ID 校验（ASCII、长度 1..64） |
| `authority_rank` | 非负整数；布尔值不算整数 |
| `documents` | 文献数组；允许为空 |
| `citations` | 引用数组；允许为空 |

每篇文献只能包含 `local_id`、`date`、`title`：

- `local_id` 字符串在同一 `source_id` 内唯一。
- `date` / `title` 沿用 `process` 的非空有界文本规则。
- 原始记录由 `(source_id, local_id)` 唯一标识。

每条引用只能包含 `from`、`to`、`page`：

- `from` / `to` 必须是同一来源内已存在的 `local_id`。
- `page` 沿用现有非空有界文本规则。
- 原始 `from == to` 不会在输入阶段拒绝；归并后进入内部引用证据。

全部来源合计最多 5000 篇本地文献、25000 条原始引用。

### `identity_claims`

必须是最多 10000 项的数组，每项只能包含：

| 字段 | 约束 |
| --- | --- |
| `claim_id` | 唯一的非空字符串；为方便合成数据也接受非布尔整数，全部声明中唯一 |
| `left` / `right` | 完整记录定位；推荐 `{"source_id": ..., "local_id": ...}`，也接受 `(source_id, local_id)` 二元组/数组或无歧义的 `source_id:local_id` 字符串，必须已存在 |
| `relation` | `"same"` 或 `"different"` |
| `evidence` | 非空字符串 |

以下情况均为输入错误：

- 来源、文献、字段或枚举值非法；
- 数量越界；
- `claim_id` 重复；
- 同一无序记录对出现两次同类关系（交换 `left` / `right` 也视为同一对）；
- 一条 `different` 声明的两端是同一条记录。

同一对记录分别出现 `same` 与 `different` 不是重复声明；它会在身份分量核对时
形成可复核的身份冲突。

## 身份分量与隔离

实现使用并查集处理全部 `same` 声明，复杂度接近线性，不枚举所有记录对。

1. 先对全部 `same` 声明做无向、可传递并查集合并。
2. 再核对全部 `different`：只要其两端落在同一身份分量，该分量整体身份冲突。
3. 无论是否已有身份冲突，都继续解析 `date` 与 `title`，因此同一分量同时存在
   身份冲突和字段冲突时会一起报告。
4. 对每个字段，取分量内最小 `authority_rank` 的全部观察：
   - 最强层级只有一个不同值时选择该值；
   - 最强层级有多个不同值时产生字段冲突；
   - 较弱层级的异文不阻止发布，但全部观察保留。
5. 只有无身份冲突、`date` 无冲突、`title` 无冲突的分量可以发布。

发布分量的 `canonical_id` 按以下顺序选择代表记录：

```text
(authority_rank 最小, source_id 字典序, local_id 字典序)
```

最终 ID 为 `source_id:local_id`。

## 返回结构

所有列表和证据均按稳定语义键排序；打乱来源、文献、引用或声明的输入顺序不会
改变 JSON 结果。主要字段如下。

| 字段 | 含义 |
| --- | --- |
| `documents` | 可直接交给 `process` 的规范文献输入，元素恰为 `{id,date,title}` |
| `citations` | 可直接交给 `process` 的规范引用输入，元素恰为 `{from,to,page}` |
| `canonical_catalog` / `canonical_input` | `{"documents": ..., "citations": ...}` 规范输入 |
| `document_evidence` | 每个发布分量的代表 ID、全部成员/别名、选中值、字段观察与选择依据、支持性 `same` 声明 |
| `aliases` | `{canonical_id: [非代表别名 ID...]}` |
| `field_evidence` | `{canonical_id: {date: ..., title: ...}}` |
| `components` | 全部分量（含发布与隔离）的完整证据 |
| `quarantined_components` | 隔离分量；兼容别名 `quarantine`、`conflicted_components` |
| `blocked_citations` | 端点落入隔离分量的每条原始引用 |
| `internal_citation_evidence` | 投影到同一发布分量的每条原始引用 |
| `citation_evidence` | 外部规范引用的聚合及其全部原始来源定位 |
| `identity_claim_evidence` | 每条声明的分类：同分量、身份冲突或不同分量 |
| `process` | `process(canonical_catalog)` 的完整结果；没有可发布文献时为 `null` |
| `lineage` / `process_result` | `process` 的兼容别名 |
| `summary` | 各类输入、分量、发布、隔离和引用证据计数 |

返回值只包含 JSON 可序列化类型，可执行
`json.dumps(result, allow_nan=False)`。

### 分量证据

`components[]` 与 `quarantined_components[]` 的主要字段：

```json
{
  "component_id": "s1:A",
  "canonical_id": "s1:A",
  "publishable": false,
  "status": "quarantined",
  "members": ["s1:A", "s2:B", "s3:C"],
  "member_locations": [
    {"source_id": "s1", "local_id": "A"}
  ],
  "aliases": ["s2:B", "s3:C"],
  "conflict_types": ["identity", "field:date"],
  "conflict_kinds": ["field", "identity"],
  "conflict_fields": ["date"],
  "conflicts": [],
  "identity_conflict": {},
  "field_evidence": {
    "date": {"status": "conflict", "observations": []},
    "title": {"status": "selected", "observations": []}
  },
  "same_claims": [],
  "different_claims": []
}
```

`conflicts[]` 中：

- 身份冲突为 `{"type": "identity", "different_claims": [...], "same_claims": [...]}`；
- 字段冲突沿用字段证据，并带 `"type": "field"` 与 `"field": "date" | "title"`；
- `conflicting_values` 给出最强层级的每个不同值及其记录，便于复核同层冲突。

即使分量被隔离，`component_id` / `canonical_id` 仍是按代表记录规则得到的稳定
引用 ID；`publishable=false` 与 `published_canonical_id=null` 表明它没有进入
规范输入。

### 引用证据

- `blocked_citations[]` 保留原始 `source_id/from/to/page` 与 `origin`，列出
  `blocked_endpoints`（`"from"` / `"to"`）、投影分量 ID、冲突类型和可到
  `quarantined_components` 复核的 `component_id`。
- `internal_citation_evidence[]` 逐条保留投影到同一发布分量的原始引用及
  `canonical_id`；这些边不会作为自引交给 `process`。
- `citation_evidence[]` 对应一条规范 `(from,to,page)`，包含：
  - `sources`：去重并排序的来源 ID；
  - `source_locations` / `origins` / `provenance`：按稳定顺序排列的全部原始
    `{source_id,from,to,page}`；
  - `raw_citation_count`：原始定位条数。

不同 `page` 不会聚合；完全重复的原始定位会保留为重复的来源证据。

## `process` 上限

归并不会静默截断。若存在可发布文献，则规范输入必须满足现有契约：

- 发布分量最多 1000 个；
- 聚合后的外部规范引用最多 5000 条。

超过任一上限会抛出 `ConsolidationLimitError`（仍是 `ValueError`）。没有任何可
发布文献时，`documents` 与 `citations` 均为空数组，`process` 为 `null`；此时不
调用 `process`，因此不会放宽其“至少一篇文献”的限制。隔离分量、阻断引用和内部
引用证据仍完整返回。

## 确定性与规模

- 身份关系：并查集 + 排序后的分量/声明证据。
- 字段选择：只依赖 `authority_rank` 与字段值，不依赖输入顺序。
- 引用聚合：以规范 `(from,to,page)` 为键，来源定位按
  `(source_id,from,to,page)` 排序。
- 列表输出均排序；集合只用于去重，不直接决定输出顺序。
- 可处理 5000 篇本地文献、25000 条引用、10000 条声明的合法输入；身份发现不枚举
  记录对。

## 示例

```python
from citation_lineage import consolidate, process

result = consolidate(
    catalogs=[
        {
            "source_id": "library-a",
            "authority_rank": 0,
            "documents": [
                {"local_id": "A", "date": "1920", "title": "Archive"},
                {"local_id": "B", "date": "1921", "title": "Reply"},
            ],
            "citations": [
                {"from": "B", "to": "A", "page": "12"},
            ],
        }
    ],
    identity_claims=[],
)

canonical_input = {
    "documents": result["documents"],
    "citations": result["citations"],
}
assert result["process"] == process(canonical_input)
```

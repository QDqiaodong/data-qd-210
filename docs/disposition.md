# 处置评估契约（`assess_disposition`）

`assess_disposition(data, records, holds, request)` 在现有
[`process`](contract.md) 引用图上，把**保管期限**、**法律保全（hold）** 与
**引用证据依赖**合并成一个可审计的处置决定：给定一批拟销毁文献和评估时点
`as_of`，逐候选给出稳定决定及全部直接原因，并返回处置后的规范目录与谱系。

```python
from citation_lineage import assess_disposition

result = assess_disposition(data, records, holds, request)
result["allowed_document_ids"]   # 允许处置
result["blocked_document_ids"]   # 必须保留
result["process_after"]          # 处置后 process；全部处置时为 None
```

本功能只使用 Python 标准库，不访问网络、数据库或持久化服务，也不会修改调用方
传入的 `data` / `records` / `holds` / `request` 任何对象。任一输入非法时抛出
`DispositionError`（`ValueError` 子类），整次调用失败，不返回部分决定。

## 输入

### `data`

必须完全符合 [`process`](contract.md) 的输入契约（1..1000 篇文献、0..5000 条
引用定位）。函数内部调用 `process(data)` 完成校验并取得规范引用图与
`cited_ancestors`。`data` 中各文献原有的 `date` 仍是不解释的档案标签，**不得**
用来推断保管期限——保管期限只来自 `records` 的 `retention_until`。

### `records`

列表必须**恰好覆盖 `data` 中全部文献**，每项必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `id` | 文献 ID，必须存在于 `data` |
| `record_class` | 非空有界文本 |
| `retention_until` | 真实存在的 `YYYY-MM-DD` 日期或 `null` |
| `size_bytes` | `1..100_000_000` 的整数（布尔值不合法） |

`retention_until = null` 表示**永久保留**。记录缺失、重复、未知文献、非法日期
或越界大小都使整次调用抛 `ValueError`。

### `holds`

0..1000 项，每项必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `hold_id` | 唯一、非空有界文本 |
| `document_ids` | 1..100 个唯一 ID，必须存在于 `data` |
| `status` | `active` 或 `released` |
| `reason` | 非空有界文本 |

只有 `active` hold 直接保护其列出的文献；`released` hold 不再阻止处置，但仍作为
审计输入和逐文献 `hold_evidence` 证据保留（`released_hold_ids`）。重复 `hold_id`
或未知文献抛 `ValueError`。

### `request`

必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `disposition_id` | 唯一、非空有界文本 |
| `as_of` | 真实存在的 `YYYY-MM-DD` 日期 |
| `candidate_ids` | 1..1000 个唯一 ID，必须存在于 `data` |
| `reason` | 非空有界文本 |

## 处置规则

保管期采用**含截止日**口径：仅当 `as_of` **严格晚于**非空
`retention_until` 时，文献自身才过期（`expired`）；`as_of == retention_until`
视为尚未过期。

先形成**必须保留的锚点集合**：

1. 所有**不在 `candidate_ids` 中**的文献；以及
2. 候选中满足任一：`retention_until = null`（永久保留）、尚未过期、或被任一
   `active` hold 保护。

保留集合是锚点集合在引用图上的**前向闭包**：每个锚点沿引用方向
`from → to` 可达的全部被引文献都须保留（与 `process` 的 `cited_ancestors` 语义
一致）。

一篇候选只有在**自身已过期、没有 active hold，且不在任何锚点的引用证据闭包内**
时才可处置。

该判断针对整次请求**共同完成**，不能逐候选独立判断：

- 一个因保管期尚未到期或 active hold 而受保护的候选会成为锚点，并继续保护它
  引用的其他候选；
- 只检查直接引用不够：若 `A` 必须保留、`A` 引用 `B`、`B` 引用 `C`，即使 `B`、
  `C` 都过期且都在候选中，二者仍须保留；
- 循环同样服从该规则：循环中任一成员成为锚点，整个循环内的候选都被保护；若
  循环全部由已过期、无 hold 的候选组成，且没有其他保留锚点能到达它们，则可随
  本次处置一并移除。

## 输出

结果为标准 JSON 对象，可用 `json.dumps(..., allow_nan=False)` 序列化，且按身份
（而非输入顺序）稳定排序。至少包含：

| 字段 | 含义 |
| --- | --- |
| `disposition_id` / `as_of` | 请求身份与评估时点 |
| `records` | 规范化并按 `id` 排序的记录 |
| `holds` | 规范化并按 `hold_id` 排序、`document_ids` 已排序的 hold |
| `request` | 规范化请求（`candidate_ids` 已排序） |
| `process` / `process_before` | 处置前完整 `process` 结果 |
| `decisions` | 逐候选决定，按 `candidate_ids` 字典序 |
| `allowed_document_ids` / `disposable_document_ids` | 允许处置的文献 ID |
| `blocked_document_ids` | 被阻止（须保留）的文献 ID |
| `total_allowed_bytes` / `allowed_bytes` | 允许处置的总字节数 |
| `anchor_document_ids` | 全部保留锚点 |
| `documents` / `citations` | 处置后仍保留的规范目录与引用定位 |
| `removed_citations` | 因任一端点被处置而移除的完整引用定位 |
| `process_after` / `post_process` | 处置后目录的完整 `process`；全部处置时为 `null` |
| `summary` | 计数摘要 |

### 逐候选决定

`decisions` 每项至少包含：

| 字段 | 含义 |
| --- | --- |
| `document_id` | 候选 ID |
| `decision` | `dispose` 或 `retain` |
| `blocked` | 是否被阻止 |
| `reasons` | **全部**直接原因的稳定有序列表 |
| `retention` | 保管期证据：`record_class`、`retention_until`、`permanent`、`not_elapsed`、`expired`、`as_of` |
| `size_bytes` | 该文献字节数 |
| `active_hold_ids` | 全部相关 `active` hold ID |
| `released_hold_ids` | 全部相关 `released` hold ID（审计） |
| `hold_evidence` | 覆盖该文献的全部 hold（`hold_id` / `status` / `reason`） |
| `citation_evidence` | 引用证据见证；无需引用证据时为 `null` |

`reasons` 至少区分：

- `permanent_retention`：`retention_until = null`；
- `retention_not_elapsed`：`as_of` 未严格晚于 `retention_until`；
- `active_hold`：至少一个 `active` hold 保护该候选；
- `required_as_citation_evidence`：该候选位于某保留锚点的引用闭包内。

同一候选可同时具有多类原因，不会因先发现一种而隐藏另一种。`reasons` 按字符串
排序（`active_hold`、`permanent_retention`、`required_as_citation_evidence`、
`retention_not_elapsed`）。

### 引用证据见证

`citation_evidence` 给出可复核的确定性见证：

- `anchor_id`：**字典序最小**、能够沿引用方向到达该候选的保留锚点；
- `document_ids`：从锚点到候选的最少文献边路径（含端点）；
- `locators`：该路径上逐边使用的完整 `(from, to, page)` 定位序列，保留实际
  页码；
- `edge_count`：路径边数（`len(locators)`）；
- `anchor`：锚点自身的保留依据（`reasons`、`retention_until`、
  `active_hold_ids`）。

见证选择规则：先取最小可达锚点，再求该锚点到候选的最少文献边路径；存在同长度
路径或同一文献边的多个页码定位时，按完整 `(from, to, page)` 定位序列的字典序
选择。见证保留实际页码，不返回全局 `max_depth`。实现先对图做 SCC 压缩并求最短
距离，再在最短路径 DAG 上逐边贪心，复杂度为 `O(V + E)`，**不枚举全部简单路径或
全部简单环**，在 1000 篇文献、5000 条引用规模下可迭代运行。

## 处置后目录

处置后保留的 `documents` 与 `citations` 按规范顺序给出，且不含引用已移除端点的
悬空定位；同一对文献的不同 `page` 仍分别保留或移除。若保留目录非空，`process_after`
为对其调用现有 `process` 的完整结果；若全部文献均可处置，则 `documents` 与
`citations` 均为空、`process_after` 为 `null`，但决定、证据与 `removed_citations`
仍完整。**不得放宽** `process` 的至少一篇文献限制。

## 确定性与失败边界

- 打乱 `documents`、`citations`、`records`、`holds`、hold 内 `document_ids` 以及
  `candidate_ids` 的无语义顺序，不改变按身份对齐后的任何结果。
- 非法日期（非 `YYYY-MM-DD` 或日历上不存在，如 `2023-02-29`）、记录覆盖不完整
  或重复、hold/request 身份重复、未知文献、非法字段或数量越界，均使整次调用
  抛 `ValueError`，不返回部分决定，也不修改任何输入。
- 图算法使用 SCC 压缩与位集可达性，避免递归遍历千级链或枚举环。

# 引用修订流水线契约（`revise`）

`revise(data, batches)` 在已发布的引用关系之上按顺序应用修订批次。`data` 与
[`process`](contract.md) 的输入契约完全相同（字段、身份、文本、数量、JSON 值与
上下限限制原样沿用）；文献集合在整个修订过程中固定为初始 `data` 声明的文献，
任何批次都不得暗中新增、删除或改名文献，也不得引用集合之外的文献。

## 批次结构

`batches` 为按顺序提交的批次列表，每个批次必须且只能包含以下字段：

| 字段 | 约束 |
| --- | --- |
| `revision_id` | 非空有界文本（沿用 `process` 文本规则），在所有已接受批次中唯一 |
| `base_revision` | 首批为 `null`；之后必须等于紧邻的上一**已接受**批次的 `revision_id` |
| `reason` | 非空有界文本，记录修订原因 |
| `changes` | 1..5000 项变更，每项含 `from`、`to`、`page`、`action` |

`action` 取值为 `"add"`（新增）或 `"remove"`（撤销）；定位三元组
`(from, to, page)` 与 `process` 的引用定位同义，端点必须存在且不同。

## 校验顺序与原子性

每个批次作为一个整体生效，校验全部通过后才提交：

1. 批次与变更结构校验（字段集合、身份、文本、文献集合封闭性、禁止自引）。
2. 批次内同一三元组不得出现一次以上（无论动作是否相同）。
3. 顺序校验：`base_revision` 必须指向紧邻的上一已接受批次（首批必须为 `null`）。
4. 语义校验基于**批次开始时**的定位集合：`add` 只能针对当前不存在的定位，
   `remove` 只能针对当前存在的定位。

任一项无效即拒绝该批次：此前已接受的版本与调用方输入对象都不会被修改，
处理立即停止，失败批次之后的批次列入 `unprocessed_batches`，不做部分应用。

拒绝通过 `RevisionError`（`ValueError` 子类）抛出：异常携带 `revision_id`、
批次下标 `index`、可定位的业务原因 `reason`，以及与成功路径同构的结构化
`result`——其中保留失败前已接受的全部版本，并在 `failure` /
`failed_batch` 与 `unprocessed_batches` 中标记失败批次与未处理批次。
全部批次有效时 `revise` 正常返回顶层结果，`status` 为 `"completed"`。

## 重放与身份冲突

- 同一提交中 `revision_id` 不得出现两次且内容不同：预扫描会在任何应用之前
  将其判定为身份冲突并拒绝（包括两个从未接受过的同 ID 批次），不产生部分
  修订；内容完全相同的同 ID 重复按下一条重放规则处理。
- 已接受的 `revision_id` 若再次出现且 `base_revision`、`reason`、变更集合
  （以定位三元组归一化，批次内书写顺序不影响判定）完全相同，视为**确认重放**：
  返回原已接受版本及其原影响，不推进版本、不重复应用变更，也不改变当前版本头。
- 相同 `revision_id` 携带不同原因、父版本或变更内容属于**身份冲突**，直接拒绝。
- 重放不绕过顺序校验：重放之后的批次仍须指向真实的紧邻已接受版本。

## 版本结果

每个版本（含初始版本）包含：

| 字段 | 含义 |
| --- | --- |
| `revision_id` | 初始版本为 `null`，修订版本为批次 ID |
| `base_revision` | 父版本 ID；初始版本为 `null` |
| `reason` | 修订原因；初始版本为 `null` |
| `process` | 该版本完整的 `process` 结果（独立计算，不与其他版本互相覆盖） |
| `document_depths` | 逐文献深度，见下 |
| `applied` | 实际 `added` / `removed` 的定位（按 from/to/page 排序） |
| `impact` | 与前一版本相比的文献影响集合；初始版本四个集合均为空 |

### `document_depths`

为每篇文献给出非负整数或 `null`：

- **无环版本**：沿引用方向（from→to）从任一根文献到该文献的最长路径边数；
  根文献与孤立文献为 `0`。
- **含环版本**：先按 `process` 的定义识别大小大于 1 的强连通分量；环内文献
  以及沿引用方向从任一环成员可达的全部文献记为 `null`。其余文献在剔除这些
  环分量后的无环子图上，按不经过环分量的根到文献最长路径取值。

因此，位于环上游但自身不能从环到达的文献、以及与环不连通的无环部分，不会仅
因全局存在环而变成 `null`。该字段是修订 API 的附加结果，不改变 `process` 的
全局 `summary.max_depth` 契约：版本中该字段原样保留 `process` 的值，任一环
存在时仍为 `null`。

### `impact` 影响集合

四类集合均按文献 ID 排序：

- `roots`：根身份发生变化的文献（进入或退出根集合）。
- `cycle_members`：循环成员身份发生变化的文献（进入或退出任一大小大于 1 的
  强连通分量）；循环从有环变无环或反向变化时，所有状态翻转的文献都会列出。
- `cited_ancestors`：可达被引集合发生变化的文献（逐项比较有序列表）。
- `document_depths`：逐文献深度发生变化的文献，逐项比较前后版本的值，
  整数与 `null` 之间的变化同样计入；不得由全局 `max_depth` 推断。

## 顶层结果

成功返回值与失败时 `RevisionError.result` 结构相同：

```json
{
  "status": "completed | failed",
  "initial_revision_id": null,
  "head_revision_id": "最新已接受修订 ID 或 null",
  "accepted_revision_ids": ["..."],
  "versions": ["初始版本 + 每个已接受修订版本"],
  "failure": "失败批次的结构化原因，或 null",
  "failed_batch": "同 failure（显式别名），或 null",
  "unprocessed_batches": ["失败批次之后未处理的批次下标与 ID"]
}
```

调用示例：

```python
from citation_lineage import RevisionError, revise

try:
    outcome = revise(data, batches)
except RevisionError as exc:
    outcome = exc.result          # 含已接受版本、failure 与 unprocessed_batches
    print(exc.index, exc.reason)
```

整个结果只包含 JSON 可序列化类型（字典、列表、字符串、整数或 `null`），
可直接使用 `json.dumps(..., allow_nan=False)` 序列化。

## 确定性

无语义的顺序差异不得改变输出：文献顺序、引用书写顺序、批次内变更的书写顺序
被打乱时，规范化后的定位集合、版本结果、影响集合与排序完全一致；批次本身的
提交顺序具有语义，不能打乱。

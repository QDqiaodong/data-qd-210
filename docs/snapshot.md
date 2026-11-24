# 双时间发布快照契约（`materialize`）

`materialize(data, assertions, queries)` 在一份固定档案目录之上，按**业务时间**
（材料在业务上何时有效）与**知识时间**（审计方何时掌握该声明）两个独立时间轴，
为每个查询产出可复核的发布快照。它回答两类问题：在某个业务时点哪些材料有效，
以及截至某个知识截止时间审计方实际上已经掌握哪些声明。后来补录、但业务生效
时间更早的声明不得泄漏进更早知识截止的审计结果。

`data` 与 [`process`](contract.md) 的输入契约完全相同（文献 1..1000、引用
0..5000、字段与文本上限原样沿用），且**只用于定义可被声明引用的固定文献与引用
定位全集**：函数绝不修改调用方传入的 `data`、`assertions`、`queries`。

## 实体与定位

一个实体由 `kind` 与 `locator` 唯一标识：

| kind | locator | 说明 |
| --- | --- | --- |
| `"document"` | 文献 ID 字符串 | 与 `data.documents[].id` 同义 |
| `"citation"` | `{"from","to","page"}` | 完整 `(from, to, page)` 三元组，与 `data.citations[]` 同义 |

声明只能引用 `data` 中存在的实体；引用任何集合之外的文献或引用定位都是整次
调用级输入错误。

## 声明（assertions）

`assertions` 为 0..30000 项，每项必须且只能包含以下字段：

| 字段 | 约束 |
| --- | --- |
| `assertion_id` | 非空有界文本，全局唯一 |
| `kind` | `"document"` 或 `"citation"` |
| `locator` | 文献 ID，或 `from/to/page` 对象 |
| `state` | `"available"` 或 `"withdrawn"` |
| `valid_from` | 带 `Z` 的 UTC RFC 3339 秒级文本 |
| `valid_to` | 同上，或 `null`；非空时必须严格晚于 `valid_from` |
| `recorded_at` | 同时间格式，按真实日历时间比较 |
| `reason` | 非空有界文本 |
| `supersedes` | 0..20 个同一实体内的 `assertion_id` |

业务区间采用**左闭右开**语义：`valid_from <= valid_at < valid_to`；
`valid_to` 为 `null` 表示右端开放。时间只接受 `YYYY-MM-DDTHH:MM:SSZ` 形态，
非法日历日期（如 `2020-13-01T...`、`2020-02-30T...`）一律拒绝。

### 纠正关系（supersedes）

`supersedes` 表示本声明纠正了同一实体的哪些既有声明，规则如下，任一违反都
使**整次调用**抛出 `ValueError`（具体为 `MaterializationError`），不返回部分
结果：

- 每个目标必须存在（禁止悬空引用），且在同一声明内不得重复；
- 目标必须与本声明属于**同一实体**，禁止跨实体纠正；
- 目标的 `recorded_at` 必须**严格早于**本声明；
- 纠正关系不得成环（含自纠正）；
- 一条声明可同时纠正 0..20 个既有分支，使冲突在后续裁决中真正收敛；
- 声明先后只由 `recorded_at` 决定，**输入顺序不具备任何语义**。

## 查询（queries）

`queries` 为 0..1000 项，每项必须且只能包含唯一 `query_id`、业务时间
`valid_at` 与知识截止时间 `known_at`（同一时间格式）。

### 可见性

对某个查询，只有 `recorded_at <= known_at` 的声明可见；未来才记录的声明连同
它的纠正关系都不得影响该查询。

### 前沿与状态判定

对同一实体，在其可见声明组成的纠正 DAG 上：

1. 被任一**可见**声明直接或间接 supersede 的声明退出当前前沿（frontier）；
   不可见的未来声明不参与前沿计算。
2. 在前沿声明中，取业务区间覆盖 `valid_at` 的声明作为覆盖声明。
3. 状态判定：
   - 没有覆盖声明 → `"unknown"`；
   - 覆盖声明全部 `available` 或全部 `withdrawn` → 采用该状态，并保留全部
     支持声明；
   - 两种状态同时存在 → `"conflict"`。**禁止**用较晚 `recorded_at`、较大 ID
     或输入顺序擅自决胜。
4. 区间不覆盖查询时点的前沿声明仍作为可见历史保留，但不参与当前状态判断。

### 引用发布联动

先独立求每个文献与引用的状态，再据此构造规范目录：

- 只有状态为 `available` 的文献进入规范 `documents`；
- 一条引用仅在**自身 `available` 且两个端点文献也 `available`** 时才发布到
  规范 `citations`；
- 自身可发布、但任一端点为 `withdrawn` / `unknown` / `conflict` 的引用进入
  `blocked_citations`，逐项列出被哪个端点（`from`/`to`）及何种状态阻断；
- 引用自身为 `withdrawn` / `unknown` / `conflict` 时**不**进入规范图，分别
  保留在 `withdrawn_citations` / `unevidenced_citations` /
  `conflicted_citations`，与端点阻断严格区分、不得混类。

文献状态变化会联动改变引用发布与最终图结果，即使引用自己的前沿声明没有变化。

### 规范目录与 `process`

- 没有任何 `available` 文献时，规范 `documents` 与 `citations` 均为空，
  `process` 为 `null`；状态、冲突与阻断证据仍完整保留。这不会放宽
  `process` 至少一篇文献的限制——空目录根本不会调用 `process`。
- 有可发布文献时，规范输入继续满足 `process` 的全部上限与字段契约，并在
  快照的 `process` 字段返回其完整结果。

## 返回结构

返回 `{"assertions": [...], "snapshots": [...]}`，可直接
`json.dumps(..., allow_nan=False)` 序列化（无 NaN/Infinity，仅 JSON 基础类型）。

- 顶层 `assertions` 是按 `assertion_id` 排序的**完整声明注册表**，只构造一次；
  各快照通过 ID 引用其中条目，避免在每个快照重复复制声明字典。
- `snapshots` 按 `query_id` 稳定排序（查询书写顺序无语义），每个快照含：
  - `query_id`、`valid_at`、`known_at`（原文时间文本回显）；
  - `visible_assertion_ids`：本查询可见的全部声明 ID（按 ID 排序的元组）；
  - `document_states` / `citation_states`：以实体定位为键的状态映射，每个值含
    `status` 与 `evidence`；`evidence` 给出可见、被纠正、前沿、覆盖、
    available/withdrawn 覆盖、不覆盖前沿等全部 ID 分区，跨查询内容相同的证据
    对象共享；
  - 规范 `documents` / `citations`；
  - `blocked_citations`、`withdrawn_citations`、`unevidenced_citations`、
    `conflicted_citations` 四类未发布引用证据；
  - `process`（无可用文献时为 `null`）与计数 `summary`。

## 确定性与双时间差异

- 打乱 `documents`、`citations`、`assertions`、各 `supersedes` 列表与
  `queries` 的无语义顺序，不改变按身份对齐后的输出。
- 后来记录、但业务生效更早的撤回或纠正，只能改变 `known_at` 已覆盖它的查询；
  更早知识截止的快照保持原样、可独立复核。
- 当一条后续声明同时 supersede 两个冲突分支时，较晚知识截止的快照可从
  `conflict` 收敛为单一状态，而较早查询仍保留当时的冲突。

## 性能边界

实现先一次性校验全部输入并按实体、`recorded_at` 建立索引，再按 `known_at`
递增扫描查询：每个声明在整个扫描中至多进入可见集一次、其纠正出边闭包至多
展开一次（总开销 O(声明数 + 纠正边数)），不会为每个查询重扫全部声明或重建
与查询无关的纠正关系。完整声明注册表与等价证据对象在查询间共享，以控制
30000 声明 × 1000 查询规模下的内存占用。整个计算不访问网络、数据库或任何
外部档案服务。

## 错误

任何结构、时间、定位、上限或纠正 DAG 规则错误都抛出
`MaterializationError`（`ValueError` 子类）；错误在任何快照计算之前检出，
调用失败时调用方输入对象保持不变。

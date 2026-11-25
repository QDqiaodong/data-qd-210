# 档案引用谱系契约

documents为1..1000个唯一ASCII id、date/title非空字符串（最多256代码点，无控制/代理项）。date为不解释的档案标签，不推断时间先后。citations为0..5000项from/to/page，两个端点必须存在且不同，page非空同样有界文本。相同(from,to,page)重复拒绝；同一对文献不同页码可保留多条定位引用，图可达性和深度按去重文献边计算。

方向是引用文献from→被引用文献to。roots指没有其他文献引用它的文献（图入度0），不是时间最早的原始来源。cited_ancestors为沿出边可达的被引文献id，排除自身；不推断引用就意味着抄录。cycles返回大小>1的强连通分量，每组成员及组间排序稳定，不枚举全部简单环。含任意循环时max_depth=null；无环时为最长引用边链长度，孤点深度0。使用迭代图遍历，允许合法1000文献链而不触发Python递归上限。

在已发布结果之上按批次修订引用关系（新增/撤销定位、原子提交、版本谱系、重放与逐文献深度）由独立的 `revise` API 承担，契约见 [revision.md](revision.md)；本文件的 `process` 输入输出契约保持不变，修订版本的完整谱系仍由 `process` 逐版本计算。

从多个合作方的本地目录和显式身份声明生成可交给 `process` 的规范目录，由独立的 `consolidate` API 承担，契约见 [consolidation.md](consolidation.md)；该 API 不修改或放宽本文件中的 `process` 文献、引用与 ID 限制。

在固定档案目录之上按业务时间与知识时间回答“某时点哪些材料有效、截至当时掌握哪些声明”，并以 `supersedes` 纠正 DAG 求前沿、按左闭右开区间判定 available/withdrawn/unknown/conflict，由独立的 `materialize` API 承担，契约见 [snapshot.md](snapshot.md)；该 API 不修改调用方输入，也不放宽本文件中的任何 `process` 限制。

在本文件的规范引用图与 `cited_ancestors` 闭包之上，生成满足文献数/字节上限、强连通分量原子性和回执连续前缀要求的可续传离线移交批次，由独立的 `prepare_handoff` API 承担，契约见 [handoff.md](handoff.md)；该 API 同样不修改调用方输入。

把保管期限、法律保全（`active` / `released` hold）与引用证据依赖合并为可审计的处置决定，由独立的 `assess_disposition` API 承担，契约见 [disposition.md](disposition.md)；该 API 复用本文件的规范引用图与 `cited_ancestors` 语义，不修改调用方输入，也不放宽本文件的任何 `process` 限制。`data` 中的 `date` 仍是不解释的档案标签，处置评估不得据此推断保管期限。

在本文件的规范引用图与 `cited_ancestors` 闭包之上，为重点文献及其全部被引证据共同求解满足故障域分散要求的数字载体固化副本修复计划（副本状态判定、replace/create 动作、共享站点 slot 的全局最小费用流与字典序决胜、blocked 结构化证据），由独立的 `plan_preservation` API 承担，契约见 [preservation.md](preservation.md)；该 API 不修改调用方输入，登记摘要只来自 `assets`，不做多数表决，也不放宽本文件的任何 `process` 限制。

输出documents按id、citations按from/to/page排序。保留所有定位原文；不做全文索引、博客CMS、图书借阅或文献CRUD界面。后续异文来源证据、版次身份及时间约束可复用document/citation locator与可达结果，当前无OCR、外部目录或自动真实性判断。API错误抛ValueError；IO重复键拒绝、原子新建结果、不覆盖已有文件或链接。

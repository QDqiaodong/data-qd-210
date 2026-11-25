"""档案保管期限与引用证据联动的处置评估。

档案机构会定期提出一批拟销毁文献。一篇文献自身保管期已过，并不当然意味着可以
处置：仍须保留的文献可能直接或间接引用它作为证据。本模块把三件事合并成一个可
审计的处置决定：

* 保管期限：``retention_until`` 为 ``null`` 表示永久保留，否则只有 ``as_of``
  严格晚于该日期（含截止日口径）时文献自身才过期；
* 法律保全：只有 ``active`` 状态的 hold 直接保护其列出的文献，``released``
  状态仍作为审计输入保留但不阻止处置；
* 引用依赖：每个保留锚点沿现有引用方向可达的全部被引文献都必须保留。

锚点集合为所有非候选文献，加上永久保留、尚未过期或被任一 active hold 保护的
候选文献。保留集合是锚点集合在引用图上的前向闭包，因此某候选即使自身过期、
没有 hold，只要位于某个锚点的引用证据闭包内，仍须保留。整个判断针对整次请求
一次完成：一个因保管期或 hold 被阻止的候选会成为锚点，并继续保护它引用的其他
候选；循环成员同样服从该闭包规则。

实现复用 :func:`citation_lineage.service.process` 的规范引用图与
``cited_ancestors`` 语义，使用强连通分量压缩后的位集计算可达性，并为每个需要
引用证据的候选给出确定性的最短定位见证，不枚举全部简单路径或全部简单环。
模块只读取调用方输入，绝不修改 ``data`` / ``records`` / ``holds`` / ``request``。
仅使用 Python 标准库。
"""

from __future__ import annotations

import re
from collections import deque
from datetime import date

from .checks import array, ident, integer
from .service import process, text

MAX_HOLDS = 1000
MAX_HOLD_DOCUMENTS = 100
MAX_CANDIDATES = 1000
MAX_SIZE_BYTES = 100_000_000

_RECORD_FIELDS = frozenset(
    {"id", "record_class", "retention_until", "size_bytes"}
)
_HOLD_FIELDS = frozenset({"hold_id", "document_ids", "status", "reason"})
_REQUEST_FIELDS = frozenset(
    {"disposition_id", "as_of", "candidate_ids", "reason"}
)
_HOLD_STATUS = frozenset({"active", "released"})

# 严格 YYYY-MM-DD；再用真实日历校验（拒绝 2023-02-29 等）。
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

_PERMANENT = "permanent_retention"
_NOT_ELAPSED = "retention_not_elapsed"
_ACTIVE_HOLD = "active_hold"
_CITATION_EVIDENCE = "required_as_citation_evidence"
_OUTSIDE_CANDIDATES = "outside_candidate_set"

_DECISION_DISPOSE = "dispose"
_DECISION_RETAIN = "retain"


class DispositionError(ValueError):
    """处置请求输入或业务边界不合法；整次调用失败，不返回部分决定。"""


def _exact_fields(value, expected, label):
    if not isinstance(value, dict):
        raise DispositionError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DispositionError(
            f"{label} has invalid fields; missing={missing}, extra={extra}"
        )
    return value


def _calendar_date(value, label):
    """严格解析真实存在的 ``YYYY-MM-DD`` 日期，返回 ``date`` 对象。"""
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        raise DispositionError(f"{label} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DispositionError(
            f"{label} {value!r} is not a real calendar date"
        ) from exc


def _unique_id_list(value, low, high, label, document_ids):
    """校验 1..high 个唯一且存在的文献 ID，返回按输入顺序的列表。"""
    raw = array(value, low, high)
    result = []
    seen = set()
    for item in raw:
        document_id = ident(item)
        if document_id in seen:
            raise DispositionError(f"{label} contains duplicate ids")
        if document_id not in document_ids:
            raise DispositionError(
                f"{label} references unknown document {document_id!r}"
            )
        seen.add(document_id)
        result.append(document_id)
    return result


def _normalise_records(records, document_ids):
    """校验 records 恰好覆盖全部文献，返回按 id 排序的规范记录。"""
    array(records, 1, 1000)
    by_id = {}
    for index, record in enumerate(records):
        label = f"records[{index}]"
        _exact_fields(record, _RECORD_FIELDS, label)
        document_id = ident(record["id"])
        if document_id not in document_ids:
            raise DispositionError(
                f"{label} references unknown document {document_id!r}"
            )
        if document_id in by_id:
            raise DispositionError(f"duplicate record for {document_id!r}")

        record_class = text(record["record_class"])
        raw_until = record["retention_until"]
        if raw_until is None:
            until_text = None
            until_date = None
        else:
            until_date = _calendar_date(
                raw_until, f"{label}.retention_until"
            )
            until_text = raw_until
        size_bytes = integer(record["size_bytes"], 1, MAX_SIZE_BYTES)

        by_id[document_id] = {
            "id": document_id,
            "record_class": record_class,
            "retention_until": until_text,
            "size_bytes": size_bytes,
            "until_date": until_date,
        }

    missing = sorted(document_ids - set(by_id))
    if missing:
        raise DispositionError(f"records must cover every document; missing={missing}")
    if len(records) != len(document_ids):
        raise DispositionError(
            "records must cover every document exactly once"
        )
    return [by_id[document_id] for document_id in sorted(by_id)]


def _normalise_holds(holds, document_ids):
    """校验 hold 列表，返回规范列表和按文献索引的活跃/全部 hold 证据。"""
    array(holds, 0, MAX_HOLDS)
    normalised = []
    seen_hold_ids = set()
    active_by_document = {}
    holds_by_document = {}

    for index, hold in enumerate(holds):
        label = f"holds[{index}]"
        _exact_fields(hold, _HOLD_FIELDS, label)

        hold_id = text(hold["hold_id"])
        if hold_id in seen_hold_ids:
            raise DispositionError(f"duplicate hold_id {hold_id!r}")
        seen_hold_ids.add(hold_id)

        status = hold["status"]
        if not isinstance(status, str) or status not in _HOLD_STATUS:
            raise DispositionError(
                f"{label}.status must be 'active' or 'released'"
            )
        reason = text(hold["reason"])
        document_list = _unique_id_list(
            hold["document_ids"], 1, MAX_HOLD_DOCUMENTS,
            f"{label}.document_ids", document_ids,
        )
        document_list.sort()

        normalised.append(
            {
                "hold_id": hold_id,
                "document_ids": document_list,
                "status": status,
                "reason": reason,
            }
        )
        if status == "active":
            for document_id in document_list:
                active_by_document.setdefault(document_id, []).append(hold_id)
        for document_id in document_list:
            holds_by_document.setdefault(document_id, []).append(
                {"hold_id": hold_id, "status": status, "reason": reason}
            )

    normalised.sort(key=lambda item: item["hold_id"])
    for values in active_by_document.values():
        values.sort()
    for values in holds_by_document.values():
        values.sort(key=lambda item: item["hold_id"])
    return normalised, active_by_document, holds_by_document


def _normalise_request(request, document_ids):
    """校验处置请求身份、时点、候选集合与理由。"""
    _exact_fields(request, _REQUEST_FIELDS, "request")
    disposition_id = text(request["disposition_id"])
    as_of_text = request["as_of"]
    as_of_date = _calendar_date(as_of_text, "request.as_of")
    candidates = _unique_id_list(
        request["candidate_ids"], 1, MAX_CANDIDATES,
        "request.candidate_ids", document_ids,
    )
    reason = text(request["reason"])
    return {
        "disposition_id": disposition_id,
        "as_of": as_of_text,
        "candidate_ids": sorted(candidates),
        "reason": reason,
    }, as_of_date


def _component_masks(node_ids, citations, cycles):
    """按强连通分量压缩计算每个文献的正向/反向可达位集。

    返回 ``(reach, reverse_reach)``：``reach[i]`` 是从文献 ``i`` 沿引用方向经
    至少一条边可达的文献位集；``reverse_reach[i]`` 是经至少一条边可达文献
    ``i`` 的文献位集。两者都不包含文献自身，与 ``process`` 的
    ``cited_ancestors`` 语义一致。复杂度为 ``O((V + E) * 位集宽度)``。
    """
    size = len(node_ids)
    index_of = {document_id: i for i, document_id in enumerate(node_ids)}
    component_of = [-1] * size
    components = []

    for members in cycles:
        component_id = len(components)
        members = [index_of[document_id] for document_id in members]
        components.append(members)
        for node in members:
            component_of[node] = component_id
    for node in range(size):
        if component_of[node] < 0:
            component_of[node] = len(components)
            components.append([node])

    component_count = len(components)
    successors = [set() for _ in range(component_count)]
    predecessors = [set() for _ in range(component_count)]
    for citation in citations:
        source = component_of[index_of[citation["from"]]]
        target = component_of[index_of[citation["to"]]]
        if source != target:
            successors[source].add(target)
            predecessors[target].add(source)

    indegree = [len(predecessors[c]) for c in range(component_count)]
    pending = deque(
        sorted(c for c in range(component_count) if indegree[c] == 0)
    )
    topological = []
    while pending:
        component = pending.popleft()
        topological.append(component)
        for target in sorted(successors[component]):
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if len(topological) != component_count:  # 压缩图必为 DAG，防御性检查。
        raise DispositionError("internal citation graph condensation is cyclic")

    bits = [1 << node for node in range(size)]
    member_mask = [0] * component_count
    for component, members in enumerate(components):
        mask = 0
        for node in members:
            mask |= bits[node]
        member_mask[component] = mask

    descendants = [0] * component_count
    for component in reversed(topological):
        mask = 0
        for target in successors[component]:
            mask |= member_mask[target] | descendants[target]
        descendants[component] = mask

    ancestors = [0] * component_count
    for component in topological:
        mask = 0
        for source in predecessors[component]:
            mask |= member_mask[source] | ancestors[source]
        ancestors[component] = mask

    reach = [0] * size
    reverse_reach = [0] * size
    for node in range(size):
        component = component_of[node]
        own = member_mask[component] & ~bits[node]
        reach[node] = own | descendants[component]
        reverse_reach[node] = own | ancestors[component]
    return reach, reverse_reach


def _shortest_locator_path(anchor_index, target_index, node_ids, adjacency):
    """返回锚点到目标的最少文献边路径及其字典序最小定位序列。

    先在引用图上从目标反向 BFS 求每个文献到目标的最短边数，再自锚点起在
    最短路径 DAG 上逐边选择最小的 ``(to, page)`` 定位。所有候选最短路径长度
    相同，因此逐位贪心等价于对完整 ``(from, to, page)`` 序列求字典序最小。
    不枚举全部简单路径，复杂度为 ``O(V + E + L)``。
    """
    size = len(node_ids)
    distance = [-1] * size
    distance[target_index] = 0
    pending = deque([target_index])
    reverse = adjacency["reverse"]
    while pending:
        node = pending.popleft()
        next_distance = distance[node] + 1
        for source, _page in reverse[node]:
            if distance[source] < 0:
                distance[source] = next_distance
                pending.append(source)
    if distance[anchor_index] < 0:  # 调用方已保证可达；防御性检查。
        raise DispositionError("anchoring document cannot reach candidate")

    locators = []
    document_ids = [node_ids[anchor_index]]
    node = anchor_index
    forward = adjacency["forward"]
    while node != target_index:
        best = None
        for target, page in forward[node]:
            if distance[target] == distance[node] - 1:
                candidate = (target, page)
                if best is None or candidate < best:
                    best = candidate
        if best is None:  # 最短路径 DAG 上必有出边；防御性检查。
            raise DispositionError("failed to reconstruct citation evidence")
        target, page = best
        locators.append(
            {"from": node_ids[node], "to": node_ids[target], "page": page}
        )
        node = target
        document_ids.append(node_ids[node])

    return document_ids, locators


def _anchor_reasons(document_id, candidate_set, records, active_by_document,
                    as_of_date):
    reasons = []
    if document_id not in candidate_set:
        reasons.append(_OUTSIDE_CANDIDATES)
    until_date = records[document_id]["until_date"]
    if until_date is None:
        reasons.append(_PERMANENT)
    elif as_of_date <= until_date:
        reasons.append(_NOT_ELAPSED)
    if active_by_document.get(document_id):
        reasons.append(_ACTIVE_HOLD)
    return sorted(reasons)


def _retention_evidence(record, as_of_date):
    until_date = record["until_date"]
    permanent = until_date is None
    not_elapsed = until_date is not None and as_of_date <= until_date
    expired = until_date is not None and as_of_date > until_date
    return {
        "record_class": record["record_class"],
        "retention_until": record["retention_until"],
        "permanent": permanent,
        "not_elapsed": not_elapsed,
        "expired": expired,
    }


def assess_disposition(data, records, holds, request):
    """评估一批候选文献在给定 ``as_of`` 时点能否处置。

    参数：

    * ``data``：现有 :func:`citation_lineage.service.process` 输入契约；
    * ``records``：恰好覆盖 ``data`` 全部文献的保管记录，每项含 ``id``、
      非空 ``record_class``、``retention_until``（``YYYY-MM-DD`` 或 ``None``）
      与 ``size_bytes``；
    * ``holds``：0..1000 项法律保全，每项含唯一 ``hold_id``、1..100 个唯一
      ``document_ids``、``status``（``active`` / ``released``）与 ``reason``；
    * ``request``：唯一 ``disposition_id``、``as_of``、1..1000 个唯一
      ``candidate_ids`` 与非空 ``reason``。

    返回完整处置结果；任一输入非法时抛 :class:`DispositionError`
    （``ValueError`` 子类），不返回部分决定，也不修改任何输入。返回对象只含
    可被 ``json.dumps(..., allow_nan=False)`` 序列化的标准 JSON 类型。
    """
    if not isinstance(data, dict):
        raise DispositionError("data must be an object matching process contract")

    # process 完成 data 契约校验且不修改输入；其规范引用图供后续复用。
    lineage = process(data)
    document_ids = set(lineage["documents"])
    ordered_document_ids = list(lineage["documents"])

    normalised_records = _normalise_records(records, document_ids)
    record_by_id = {item["id"]: item for item in normalised_records}

    normalised_holds, active_by_document, holds_by_document = _normalise_holds(
        holds, document_ids
    )
    normalised_request, as_of_date = _normalise_request(request, document_ids)

    candidate_ids = normalised_request["candidate_ids"]
    candidate_set = set(candidate_ids)

    # 必须保留的锚点：全部非候选文献，以及永久保留、尚未过期或有 active hold
    # 的候选文献。保管期限此时只决定候选自身；引用闭包随后扩展保留集合。
    anchor_ids = sorted(
        document_id
        for document_id in ordered_document_ids
        if document_id not in candidate_set
        or record_by_id[document_id]["until_date"] is None
        or as_of_date <= record_by_id[document_id]["until_date"]
        or bool(active_by_document.get(document_id))
    )
    anchor_set = set(anchor_ids)

    node_ids = ordered_document_ids
    index_of = {document_id: i for i, document_id in enumerate(node_ids)}
    reach, reverse_reach = _component_masks(
        node_ids, lineage["citations"], lineage["cycles"]
    )

    anchor_mask = 0
    for document_id in anchor_ids:
        anchor_mask |= 1 << index_of[document_id]
    evidence_mask = 0
    for document_id in anchor_ids:
        evidence_mask |= reach[index_of[document_id]]
    retained_mask = anchor_mask | evidence_mask

    # 引用定位邻接表（按文献下标索引）：process 输出已按 (from, to, page)
    # 排序，且下标与 ID 字典序一致，因此每个邻接列表天然按 (to, page) 升序，
    # 可直接用于字典序最小的路径重建。同一文献对的不同 page 分别保留。
    adjacency = {
        "forward": [[] for _ in range(len(node_ids))],
        "reverse": [[] for _ in range(len(node_ids))],
    }
    for citation in lineage["citations"]:
        source = index_of[citation["from"]]
        target = index_of[citation["to"]]
        adjacency["forward"][source].append((target, citation["page"]))
        adjacency["reverse"][target].append((source, citation["page"]))

    decisions = []
    allowed_ids = []
    blocked_ids = []
    total_allowed_bytes = 0

    for document_id in candidate_ids:
        node = index_of[document_id]
        record = record_by_id[document_id]
        retention = _retention_evidence(record, as_of_date)
        active_hold_ids = list(active_by_document.get(document_id, []))
        released_hold_ids = [
            item["hold_id"]
            for item in holds_by_document.get(document_id, [])
            if item["status"] == "released"
        ]

        has_evidence = bool((evidence_mask >> node) & 1)
        reasons = []
        if retention["permanent"]:
            reasons.append(_PERMANENT)
        if retention["not_elapsed"]:
            reasons.append(_NOT_ELAPSED)
        if active_hold_ids:
            reasons.append(_ACTIVE_HOLD)
        if has_evidence:
            reasons.append(_CITATION_EVIDENCE)
        reasons.sort()

        blocked = bool((retained_mask >> node) & 1)
        if blocked:
            blocked_ids.append(document_id)
        else:
            allowed_ids.append(document_id)
            total_allowed_bytes += record["size_bytes"]

        citation_evidence = None
        if has_evidence:
            anchor_bits = reverse_reach[node] & anchor_mask
            lowest_bit = anchor_bits & -anchor_bits
            anchor_index = lowest_bit.bit_length() - 1
            anchor_id = node_ids[anchor_index]
            path_ids, locators = _shortest_locator_path(
                anchor_index, node, node_ids, adjacency
            )
            citation_evidence = {
                "anchor_id": anchor_id,
                "anchor": {
                    "document_id": anchor_id,
                    "reasons": _anchor_reasons(
                        anchor_id, candidate_set, record_by_id,
                        active_by_document, as_of_date,
                    ),
                    "retention_until": record_by_id[anchor_id][
                        "retention_until"
                    ],
                    "active_hold_ids": list(
                        active_by_document.get(anchor_id, [])
                    ),
                },
                "document_ids": path_ids,
                "locators": locators,
                "edge_count": len(locators),
            }

        decisions.append(
            {
                "document_id": document_id,
                "decision": _DECISION_RETAIN if blocked else _DECISION_DISPOSE,
                "blocked": blocked,
                "reasons": reasons,
                "retention": {
                    "record_class": retention["record_class"],
                    "retention_until": retention["retention_until"],
                    "permanent": retention["permanent"],
                    "not_elapsed": retention["not_elapsed"],
                    "expired": retention["expired"],
                    "as_of": normalised_request["as_of"],
                },
                "size_bytes": record["size_bytes"],
                "active_hold_ids": active_hold_ids,
                "released_hold_ids": sorted(released_hold_ids),
                "hold_evidence": [
                    dict(item)
                    for item in holds_by_document.get(document_id, [])
                ],
                "citation_evidence": citation_evidence,
            }
        )

    disposed_set = set(allowed_ids)
    retained_document_ids = [
        document_id for document_id in node_ids if document_id not in disposed_set
    ]
    retained_set = set(retained_document_ids)

    post_citations = [
        dict(citation)
        for citation in lineage["citations"]
        if citation["from"] in retained_set and citation["to"] in retained_set
    ]
    removed_citations = [
        dict(citation)
        for citation in lineage["citations"]
        if citation["from"] not in retained_set
        or citation["to"] not in retained_set
    ]
    post_documents = [
        {
            "id": document_id,
            "date": lineage["documents"][document_id]["date"],
            "title": lineage["documents"][document_id]["title"],
        }
        for document_id in retained_document_ids
    ]

    if post_documents:
        # 保留目录非空时正常计算处置后谱系；空目录不得放宽 process 的
        # 至少一篇文献限制，因此返回 None 而不是空输入。
        process_after = process(
            {"documents": post_documents, "citations": post_citations}
        )
    else:
        process_after = None

    held_document_ids = sorted(
        document_id
        for document_id, values in active_by_document.items()
        if values
    )
    released_hold_ids = sorted(
        item["hold_id"] for item in normalised_holds
        if item["status"] == "released"
    )

    result = {
        "disposition_id": normalised_request["disposition_id"],
        "as_of": normalised_request["as_of"],
        "records": [
            {
                "id": record["id"],
                "record_class": record["record_class"],
                "retention_until": record["retention_until"],
                "size_bytes": record["size_bytes"],
            }
            for record in normalised_records
        ],
        "holds": [dict(hold) for hold in normalised_holds],
        "request": dict(normalised_request),
        "process": lineage,
        "process_before": lineage,
        "anchor_document_ids": anchor_ids,
        "held_document_ids": held_document_ids,
        "released_hold_ids": released_hold_ids,
        "decisions": decisions,
        "allowed_document_ids": allowed_ids,
        "disposable_document_ids": list(allowed_ids),
        "blocked_document_ids": blocked_ids,
        "total_allowed_bytes": total_allowed_bytes,
        "allowed_bytes": total_allowed_bytes,
        "retained_document_ids": retained_document_ids,
        "documents": post_documents,
        "citations": post_citations,
        "removed_citations": removed_citations,
        "process_after": process_after,
        "post_process": process_after,
        "summary": {
            "candidate_documents": len(candidate_ids),
            "allowed_documents": len(allowed_ids),
            "blocked_documents": len(blocked_ids),
            "allowed_bytes": total_allowed_bytes,
            "anchor_documents": len(anchor_ids),
            "retained_documents": len(retained_document_ids),
            "removed_citations": len(removed_citations),
            "remaining_documents": len(post_documents),
            "remaining_citations": len(post_citations),
        },
    }
    return result


__all__ = [
    "assess_disposition",
    "DispositionError",
    "MAX_HOLDS",
    "MAX_HOLD_DOCUMENTS",
    "MAX_CANDIDATES",
    "MAX_SIZE_BYTES",
]

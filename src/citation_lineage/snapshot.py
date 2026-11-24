"""档案引用的双时间发布快照。

档案机构会持续收到关于固定文献/引用实体的 ``available`` / ``withdrawn``
声明，也会在以后补录或纠正早先的声明。本模块在同一份固定档案目录
（:func:`citation_lineage.service.process` 的输入契约）之上，回答两类审计问题：

* 业务时间 ``valid_at`` 上哪些材料有效；
* 知识截止时间 ``known_at`` 时审计方实际上已经掌握哪些声明。

核心规则（详见 ``docs/snapshot.md``）：

* 每个查询只能看到 ``recorded_at <= known_at`` 的声明；未来才记录的声明及其
  纠正关系不得影响该查询。
* 同一实体可见声明组成纠正 DAG：被任一可见声明直接或间接 supersede 的声明
  退出当前前沿。
* 再从前沿中取业务区间（左闭右开）覆盖 ``valid_at`` 的声明决定状态：
  无覆盖为 ``unknown``；覆盖声明状态一致时采用该状态；两种状态并存为
  ``conflict``，绝不按记录时间、ID 或输入顺序擅自决胜。
* 只有 ``available`` 文献进入规范目录；引用自身 ``available`` 且两个端点文献
  也 ``available`` 时才发布，否则按端点阻断 / 撤回 / 缺证 / 冲突分类留证。

实现上先一次性校验全部输入并按实体、记录时间建立索引，再按 ``known_at``
递增扫描查询，增量维护每个实体的可见集合与被纠正集合，避免为每个查询重扫
全部声明、重复重建与查询无关的纠正关系。本模块只读取调用方输入，绝不修改
``data`` / ``assertions`` / ``queries`` 对象，也不访问网络、数据库或外部服务。
"""

from __future__ import annotations

import re
from datetime import datetime

from .checks import array, fields, ident
from .service import process, text

MAX_ASSERTIONS = 30000
MAX_QUERIES = 1000
MAX_SUPERSEDES_PER_ASSERTION = 20

_STATE_AVAILABLE = "available"
_STATE_WITHDRAWN = "withdrawn"
_STATES = (_STATE_AVAILABLE, _STATE_WITHDRAWN)

_KIND_DOCUMENT = "document"
_KIND_CITATION = "citation"
_KINDS = (_KIND_DOCUMENT, _KIND_CITATION)

# 带 Z 的 UTC RFC 3339 秒级文本：唯一允许的时间字面量格式。
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_ASSERTION_FIELDS = frozenset((
    "assertion_id", "kind", "locator", "state", "valid_from", "valid_to",
    "recorded_at", "reason", "supersedes",
))
_QUERY_FIELDS = frozenset(("query_id", "valid_at", "known_at"))
_LOCATOR_FIELDS = frozenset(("from", "to", "page"))


class MaterializationError(ValueError):
    """双时间快照输入或业务边界不合法；整次调用失败，不返回部分结果。"""


def _timestamp(value, label):
    """严格解析 UTC 秒级 RFC 3339 文本，按真实日历时间比较。"""
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise MaterializationError(
            f"{label} must be a UTC RFC 3339 second-level timestamp ending "
            f"with 'Z', got {value!r}"
        )
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise MaterializationError(
            f"{label} {value!r} is not a valid calendar timestamp"
        ) from exc


def _entity_key(kind, locator):
    if kind == _KIND_DOCUMENT:
        return _KIND_DOCUMENT, locator
    return _KIND_CITATION, tuple(locator)


def _public_locator(kind, locator):
    if kind == _KIND_DOCUMENT:
        return locator
    source, target, page = locator
    return {"from": source, "to": target, "page": page}


def _parse_assertions(assertions, document_ids, citation_locators):
    """结构与语义校验全部声明，返回内部节点数组与按实体分组的索引。

    绝不写入调用方对象；所有错误都在任何快照计算之前抛出。
    """
    array(assertions, 0, MAX_ASSERTIONS)

    nodes = []
    by_id = {}
    entity_of = {}

    for index, assertion in enumerate(assertions):
        label = f"assertions[{index}]"
        if not isinstance(assertion, dict):
            raise MaterializationError(f"{label} must be an object")
        if set(assertion) != _ASSERTION_FIELDS:
            missing = sorted(_ASSERTION_FIELDS - set(assertion))
            extra = sorted(set(assertion) - _ASSERTION_FIELDS)
            raise MaterializationError(
                f"{label} has invalid fields; missing={missing}, extra={extra}"
            )

        assertion_id = text(assertion["assertion_id"])
        if assertion_id in by_id:
            raise MaterializationError(
                f"duplicate assertion_id {assertion_id!r}"
            )

        kind = assertion["kind"]
        if kind not in _KINDS:
            raise MaterializationError(
                f"{label}.kind must be one of {list(_KINDS)}, got {kind!r}"
            )

        raw_locator = assertion["locator"]
        if kind == _KIND_DOCUMENT:
            if not isinstance(raw_locator, str):
                raise MaterializationError(
                    f"{label}.locator must be the document id string"
                )
            locator = ident(raw_locator)
            if locator not in document_ids:
                raise MaterializationError(
                    f"{label} references document {locator!r} outside the "
                    "fixed data universe"
                )
        else:
            if not isinstance(raw_locator, dict) or \
                    set(raw_locator) != _LOCATOR_FIELDS:
                raise MaterializationError(
                    f"{label}.locator must be an object with exactly "
                    "from/to/page"
                )
            source = ident(raw_locator["from"])
            target = ident(raw_locator["to"])
            page = text(raw_locator["page"])
            locator = (source, target, page)
            if locator not in citation_locators:
                raise MaterializationError(
                    f"{label} references citation "
                    f"{list(locator)} outside the fixed data universe"
                )

        state = assertion["state"]
        if state not in _STATES:
            raise MaterializationError(
                f"{label}.state must be one of {list(_STATES)}, got {state!r}"
            )

        valid_from = _timestamp(assertion["valid_from"], f"{label}.valid_from")
        if assertion["valid_to"] is None:
            valid_to = None
        else:
            valid_to = _timestamp(assertion["valid_to"], f"{label}.valid_to")
        if valid_to is not None and not valid_to > valid_from:
            raise MaterializationError(
                f"{label}.valid_to must be strictly later than valid_from "
                "(intervals are left-closed, right-open)"
            )

        recorded_at = _timestamp(
            assertion["recorded_at"], f"{label}.recorded_at"
        )
        reason = text(assertion["reason"])

        raw_supersedes = assertion["supersedes"]
        if not isinstance(raw_supersedes, list) or not (
            0 <= len(raw_supersedes) <= MAX_SUPERSEDES_PER_ASSERTION
        ):
            raise MaterializationError(
                f"{label}.supersedes must be a list of 0.."
                f"{MAX_SUPERSEDES_PER_ASSERTION} assertion ids"
            )
        target_ids = []
        for target_id in raw_supersedes:
            if not isinstance(target_id, str):
                raise MaterializationError(
                    f"{label}.supersedes entries must be assertion id strings"
                )
            target_ids.append(text(target_id))
        if len(set(target_ids)) != len(target_ids):
            raise MaterializationError(
                f"{label}.supersedes contains duplicate targets"
            )

        node = {
            "index": len(nodes),
            "assertion_id": assertion_id,
            "kind": kind,
            "locator": locator,
            "state": state,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "recorded_at": recorded_at,
            "reason": reason,
            "supersedes_ids": target_ids,
            "supersedes_nodes": [],
        }
        nodes.append(node)
        by_id[assertion_id] = node
        entity_of[node["index"]] = _entity_key(kind, locator)

    # 纠正关系校验：悬空、跨实体、记录时间倒挂、环均为整次调用级输入错误。
    for node in nodes:
        same_entity = entity_of[node["index"]]
        for target_id in node["supersedes_ids"]:
            target = by_id.get(target_id)
            if target is None:
                raise MaterializationError(
                    f"assertion {node['assertion_id']!r} supersedes dangling "
                    f"assertion_id {target_id!r}"
                )
            if entity_of[target["index"]] != same_entity:
                raise MaterializationError(
                    f"assertion {node['assertion_id']!r} supersedes "
                    f"{target_id!r} from a different entity; corrections must "
                    "stay within one entity"
                )
            if not target["recorded_at"] < node["recorded_at"]:
                raise MaterializationError(
                    f"assertion {node['assertion_id']!r} supersedes "
                    f"{target_id!r} whose recorded_at is not strictly earlier"
                )
            node["supersedes_nodes"].append(target["index"])

    _reject_cycles(nodes)

    by_entity = {}
    for node in nodes:
        by_entity.setdefault(entity_of[node["index"]], []).append(node["index"])
    for indices in by_entity.values():
        indices.sort(key=lambda i: (nodes[i]["recorded_at"],
                                    nodes[i]["assertion_id"]))

    # 全局时间线：recorded_at 相同的节点按 assertion_id 定序，保证确定性；
    # 纠正边只连接严格更早 recorded_at 的节点，同刻节点之间不存在边。
    timeline = sorted(
        range(len(nodes)),
        key=lambda i: (nodes[i]["recorded_at"], nodes[i]["assertion_id"]),
    )
    id_order = sorted(range(len(nodes)), key=lambda i: nodes[i]["assertion_id"])
    return nodes, by_id, entity_of, by_entity, timeline, id_order


def _reject_cycles(nodes):
    """迭代 DFS 检测纠正环（白/灰/黑着色），不依赖递归深度。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * len(nodes)
    for start in range(len(nodes)):
        if color[start] != WHITE:
            continue
        stack = [(start, iter(nodes[start]["supersedes_nodes"]))]
        color[start] = GRAY
        while stack:
            node_index, iterator = stack[-1]
            child = next(iterator, None)
            if child is None:
                color[node_index] = BLACK
                stack.pop()
            elif color[child] == GRAY:
                raise MaterializationError(
                    "supersedes relations contain a cycle involving "
                    f"assertion {nodes[child]['assertion_id']!r}"
                )
            elif color[child] == WHITE:
                color[child] = GRAY
                stack.append((child, iter(nodes[child]["supersedes_nodes"])))


def _parse_queries(queries):
    array(queries, 0, MAX_QUERIES)
    parsed = []
    seen = set()
    for index, query in enumerate(queries):
        label = f"queries[{index}]"
        if not isinstance(query, dict) or set(query) != _QUERY_FIELDS:
            raise MaterializationError(
                f"{label} must be an object with exactly query_id/valid_at/"
                "known_at"
            )
        query_id = text(query["query_id"])
        if query_id in seen:
            raise MaterializationError(f"duplicate query_id {query_id!r}")
        seen.add(query_id)
        valid_at = _timestamp(query["valid_at"], f"{label}.valid_at")
        known_at = _timestamp(query["known_at"], f"{label}.known_at")
        parsed.append({
            "query_id": query_id,
            "valid_at": valid_at,
            "known_at": known_at,
            "valid_at_text": query["valid_at"],
            "known_at_text": query["known_at"],
        })
    return parsed


def _covers(node, valid_at):
    """左闭右开区间覆盖判断：valid_from <= valid_at < valid_to。"""
    if valid_at < node["valid_from"]:
        return False
    return node["valid_to"] is None or valid_at < node["valid_to"]


def _public_assertion(node):
    return {
        "assertion_id": node["assertion_id"],
        "kind": node["kind"],
        "locator": _public_locator(node["kind"], node["locator"]),
        "state": node["state"],
        "valid_from": node["valid_from_text"],
        "valid_to": node["valid_to_text"],
        "recorded_at": node["recorded_at_text"],
        "reason": node["reason"],
        "supersedes": sorted(node["supersedes_ids"]),
    }


def _entity_evidence(nodes, bucket, valid_at, evidence_cache):
    """计算单个实体在某查询下的前沿、覆盖与完整可见历史证据。

    一次遍历完成全部划分，只在末尾对各分区做稳定排序；证据中仅保留
    assertion_id（元组形式），完整声明由顶层共享的 ``assertions`` 注册表
    统一承载。内容完全相同的证据通过 ``evidence_cache`` 复用同一对象：
    在查询数很多而各实体状态变化有限时，避免为每个查询重复分配等价字典。
    """
    visible_indices = bucket["visible"]
    superseded = bucket["superseded"]

    visible_ids = []
    superseded_ids = []
    frontier_ids = []
    available_ids = []
    withdrawn_ids = []
    non_covering_ids = []

    for index in visible_indices:
        assertion_id = nodes[index]["assertion_id"]
        visible_ids.append(assertion_id)
        if index in superseded:
            superseded_ids.append(assertion_id)
            continue
        frontier_ids.append(assertion_id)
        if _covers(nodes[index], valid_at):
            if nodes[index]["state"] == _STATE_AVAILABLE:
                available_ids.append(assertion_id)
            else:
                withdrawn_ids.append(assertion_id)
        else:
            non_covering_ids.append(assertion_id)

    visible_ids.sort()
    superseded_ids.sort()
    frontier_ids.sort()
    non_covering_ids.sort()
    available_ids.sort()
    withdrawn_ids.sort()
    covering_ids = sorted(available_ids + withdrawn_ids)

    signature = (
        tuple(visible_ids), tuple(superseded_ids), tuple(frontier_ids),
        tuple(covering_ids), tuple(available_ids), tuple(withdrawn_ids),
        tuple(non_covering_ids),
    )
    cached = evidence_cache.get(signature)
    if cached is not None:
        return cached

    if not covering_ids:
        status = "unknown"
    elif available_ids and withdrawn_ids:
        status = "conflict"
    elif available_ids:
        status = _STATE_AVAILABLE
    else:
        status = _STATE_WITHDRAWN

    evidence = {
        "visible_assertion_ids": signature[0],
        "superseded_assertion_ids": signature[1],
        "frontier_assertion_ids": signature[2],
        "covering_assertion_ids": signature[3],
        "available_assertion_ids": signature[4],
        "withdrawn_assertion_ids": signature[5],
        "non_covering_frontier_assertion_ids": signature[6],
    }
    result = (status, evidence)
    evidence_cache[signature] = result
    return result


def _citation_entry(citation, status, evidence):
    return {
        "from": citation[0],
        "to": citation[1],
        "page": citation[2],
        "status": status,
        "evidence": evidence,
    }


def materialize(data, assertions, queries):
    """计算双时间发布快照序列。

    :param data: 与 :func:`citation_lineage.process` 相同的输入契约，定义唯一
        合法的文献与引用定位全集；不会被修改。
    :param assertions: 0..30000 条声明，字段与纠正 DAG 规则见模块文档。
    :param queries: 0..1000 个双时间查询。
    :return: ``{"snapshots": [...]}``；快照按 ``query_id`` 稳定排序，全部内容
        仅含 JSON 基础类型，可直接 ``json.dumps(..., allow_nan=False)``。
    :raises ValueError: 任一输入或业务规则无效时整次调用抛出
        :class:`MaterializationError`，不返回部分结果，调用方输入保持不变。
    """
    # 先沿用现有 process 完成 data 契约校验并取得固定实体全集；process 返回的
    # 是全新对象，后续不会触碰调用方的 data。
    baseline = process(data)
    document_records = baseline["documents"]
    document_ids = set(document_records)
    citation_locators = {
        (c["from"], c["to"], c["page"]) for c in baseline["citations"]
    }
    ordered_documents = sorted(document_ids)
    ordered_citations = sorted(citation_locators)

    nodes, _by_id, entity_of, by_entity, timeline, id_order = \
        _parse_assertions(assertions, document_ids, citation_locators)
    parsed_queries = _parse_queries(queries)

    # 缓存原文时间文本，输出时原样回显，避免把 datetime 泄漏到 JSON 结果。
    for node in nodes:
        node["valid_from_text"] = assertions[node["index"]]["valid_from"]
        node["valid_to_text"] = assertions[node["index"]]["valid_to"]
        node["recorded_at_text"] = assertions[node["index"]]["recorded_at"]

    # 顶层共享的完整声明注册表（只构造一次）；各快照只携带 assertion_id
    # 列表，避免 O(声明数 × 查询数) 的字典复制。
    assertion_registry = [_public_assertion(nodes[i]) for i in id_order]

    # 每个实体一份随 known_at 增长的可见列表与被纠正集合。
    entity_state = {
        key: {"visible": [], "superseded": set()} for key in by_entity
    }

    def state_for(key):
        return entity_state.setdefault(
            key, {"visible": [], "superseded": set()}
        )

    visible_flag = [False] * len(nodes)
    cursor = 0
    # 跨查询共享的证据对象缓存：相同证据签名只保留一份字典与 id 元组。
    evidence_cache = {}

    snapshots = []
    # 按知识截止时间递增扫描；输出前再按 query_id 排序，查询书写顺序无语义。
    for query in sorted(parsed_queries, key=lambda q: (q["known_at"],
                                                       q["query_id"])):
        known_at = query["known_at"]
        while cursor < len(timeline) and \
                nodes[timeline[cursor]]["recorded_at"] <= known_at:
            node_index = timeline[cursor]
            node = nodes[node_index]
            key = entity_of[node_index]
            bucket = state_for(key)
            bucket["visible"].append(node_index)
            visible_flag[node_index] = True
            # 新声明一旦可见，它直接/间接纠正的全部声明退出前沿。
            # 节点首次被标记时已遍历过其整条出边闭包，之后再次遇到可直接
            # 跳过：每个节点在整个扫描中至多展开一次，总开销 O(节点+边)。
            for target in node["supersedes_nodes"]:
                stack = [target]
                while stack:
                    current = stack.pop()
                    if current in bucket["superseded"]:
                        continue
                    bucket["superseded"].add(current)
                    stack.extend(nodes[current]["supersedes_nodes"])
            cursor += 1

        visible_assertion_ids = tuple(
            nodes[i]["assertion_id"] for i in id_order if visible_flag[i]
        )

        document_states = {}
        document_status = {}
        for doc_id in ordered_documents:
            key = (_KIND_DOCUMENT, doc_id)
            status, evidence = _entity_evidence(
                nodes, state_for(key), query["valid_at"], evidence_cache
            )
            document_status[doc_id] = status
            # 以文献 ID 为键的紧凑状态映射；evidence 为跨查询共享的证据对象。
            document_states[doc_id] = {
                "status": status,
                "evidence": evidence,
            }

        citation_states = {}
        blocked_citations = []
        withdrawn_citations = []
        unevidenced_citations = []
        conflicted_citations = []
        published_citations = []

        for citation_key in ordered_citations:
            key = (_KIND_CITATION, citation_key)
            status, evidence = _entity_evidence(
                nodes, state_for(key), query["valid_at"], evidence_cache
            )
            # 以完整 (from,to,page) 定位拼接键标识引用实体；状态映射只保留
            # 状态与共享证据，定位可由键和固定 data 全集复核。
            citation_states["|".join(citation_key)] = {
                "status": status,
                "evidence": evidence,
            }

            if status != _STATE_AVAILABLE:
                # 引用自身不具备可发布状态：按撤回 / 缺证 / 冲突分别留证，
                # 与端点阻断严格区分，不进入规范图。
                entry = _citation_entry(citation_key, status, evidence)
                if status == _STATE_WITHDRAWN:
                    withdrawn_citations.append(entry)
                elif status == "unknown":
                    unevidenced_citations.append(entry)
                else:
                    conflicted_citations.append(entry)
                continue

            blocked_endpoints = []
            for endpoint, doc_id in (("from", citation_key[0]),
                                     ("to", citation_key[1])):
                endpoint_status = document_status[doc_id]
                if endpoint_status != _STATE_AVAILABLE:
                    blocked_endpoints.append({
                        "endpoint": endpoint,
                        "document_id": doc_id,
                        "status": endpoint_status,
                    })
            if blocked_endpoints:
                blocked_citations.append({
                    "from": citation_key[0],
                    "to": citation_key[1],
                    "page": citation_key[2],
                    "status": _STATE_AVAILABLE,
                    "blocked_endpoints": blocked_endpoints,
                    "evidence": evidence,
                })
            else:
                published_citations.append({
                    "from": citation_key[0],
                    "to": citation_key[1],
                    "page": citation_key[2],
                })

        available_documents = [
            doc_id for doc_id in ordered_documents
            if document_status[doc_id] == _STATE_AVAILABLE
        ]

        if not available_documents:
            # 不得放宽 process 的至少一篇文献限制：无可用文献时不调用
            # process，规范数组均为空，但状态与阻断证据完整保留。
            canonical_documents = []
            canonical_citations = []
            process_result = None
        else:
            canonical_documents = [
                {"id": doc_id,
                 "date": document_records[doc_id]["date"],
                 "title": document_records[doc_id]["title"]}
                for doc_id in available_documents
            ]
            canonical_citations = sorted(
                published_citations,
                key=lambda c: (c["from"], c["to"], c["page"]),
            )
            process_result = process({
                "documents": canonical_documents,
                "citations": canonical_citations,
            })

        snapshots.append({
            "query_id": query["query_id"],
            "valid_at": query["valid_at_text"],
            "known_at": query["known_at_text"],
            "visible_assertion_ids": visible_assertion_ids,
            "document_states": document_states,
            "citation_states": citation_states,
            "documents": canonical_documents,
            "citations": canonical_citations,
            "blocked_citations": blocked_citations,
            "withdrawn_citations": withdrawn_citations,
            "unevidenced_citations": unevidenced_citations,
            "conflicted_citations": conflicted_citations,
            "process": process_result,
            "summary": {
                "visible_assertions": len(visible_assertion_ids),
                "available_documents": len(available_documents),
                "published_citations": len(canonical_citations),
                "blocked_citations": len(blocked_citations),
                "withdrawn_citations": len(withdrawn_citations),
                "unevidenced_citations": len(unevidenced_citations),
                "conflicted_citations": len(conflicted_citations),
            },
        })

    snapshots.sort(key=lambda snapshot: snapshot["query_id"])
    return {
        # 按 assertion_id 排序的完整声明注册表；各快照通过 id 引用其中条目。
        "assertions": assertion_registry,
        "snapshots": snapshots,
    }


__all__ = [
    "materialize",
    "MaterializationError",
    "MAX_ASSERTIONS",
    "MAX_QUERIES",
    "MAX_SUPERSEDES_PER_ASSERTION",
]

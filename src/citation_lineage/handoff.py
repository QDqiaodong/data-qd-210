"""离线档案移交编排。

本模块在现有 :func:`citation_lineage.process` 谱系结果之上计算 seed 闭包、
强连通原子单元、确定性容量批次以及回执确认状态。仅使用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import json
import re

from .checks import ident, integer
from .service import process

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_FIELDS = frozenset(
    {
        "handoff_id",
        "seed_ids",
        "payloads",
        "max_documents_per_batch",
        "max_bytes_per_batch",
        "purpose",
    }
)
_ACK_FIELDS = frozenset(
    {"handoff_id", "batch_number", "manifest_sha256", "received_at"}
)
_PAYLOAD_FIELDS = frozenset(
    {"id", "document_id", "sha256", "size_bytes"}
)


class HandoffError(ValueError):
    """移交请求或回执不合法。"""


class BatchReference(dict):
    """完整批次映射；与批号整数比较时按批号相等。

    这样既可以直接读取批次/manifest，也允许调用方把确认批次集合当作批号列表
    断言。``json.dumps`` 仍将其序列化为普通批次对象。
    """

    def __eq__(self, other):
        if isinstance(other, int) and not isinstance(other, bool):
            return self.get("batch_number") == other
        return dict.__eq__(self, other)

    def __hash__(self):
        return hash(self.get("batch_number"))


class DocumentReference(dict):
    """完整文献映射；与文献 ID 字符串比较时按 ID 相等。"""

    def __eq__(self, other):
        if isinstance(other, str):
            return self.get("id") == other
        return dict.__eq__(self, other)

    def __hash__(self):
        return hash(self.get("id"))


def _exact_fields(value, expected, label):
    if not isinstance(value, dict):
        raise HandoffError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise HandoffError(
            f"{label} has invalid fields; missing={missing}, extra={extra}"
        )
    return value


def _non_empty_string(value, label):
    if not isinstance(value, str) or not value:
        raise HandoffError(f"{label} must be a non-empty string")
    return value


def canonical_json(value):
    """按移交契约返回规范 JSON 文本。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def manifest_sha256(batch):
    """计算或复算批次 manifest；自动忽略已有的摘要字段。"""
    unsigned = {
        key: item for key, item in batch.items() if key != "manifest_sha256"
    }
    return sha256_text(canonical_json(unsigned))


def _parse_request(request, document_ids):
    if not isinstance(request, dict):
        raise HandoffError("request must be an object")
    actual = set(request)
    if actual != _REQUEST_FIELDS:
        raise HandoffError(
            "request has invalid fields; "
            f"missing={sorted(_REQUEST_FIELDS - actual)}, "
            f"extra={sorted(actual - _REQUEST_FIELDS)}"
        )

    handoff_id = _non_empty_string(request["handoff_id"], "handoff_id")
    purpose = _non_empty_string(request["purpose"], "purpose")

    raw_seeds = request["seed_ids"]
    if not isinstance(raw_seeds, list) or not 1 <= len(raw_seeds) <= 100:
        raise HandoffError("seed_ids must contain 1..100 unique ids")
    seeds = []
    seen_seeds = set()
    for value in raw_seeds:
        seed_id = ident(value)
        if seed_id in seen_seeds:
            raise HandoffError("duplicate seed_id")
        if seed_id not in document_ids:
            raise HandoffError(f"seed does not exist: {seed_id}")
        seen_seeds.add(seed_id)
        seeds.append(seed_id)

    max_documents = integer(request["max_documents_per_batch"], 1, 1000)
    max_bytes = integer(request["max_bytes_per_batch"], 1, 1_000_000_000)

    raw_payloads = request["payloads"]
    if not isinstance(raw_payloads, list):
        raise HandoffError("payloads must be a list")

    payloads = {}
    for index, raw_payload in enumerate(raw_payloads):
        label = f"payloads[{index}]"
        if not isinstance(raw_payload, dict):
            raise HandoffError(f"{label} must be an object")
        fields = set(raw_payload)
        if fields - _PAYLOAD_FIELDS or not (
            {"id", "document_id"} & fields
        ) or not {"sha256", "size_bytes"} <= fields:
            raise HandoffError(f"{label} has invalid fields")

        has_id = "id" in raw_payload
        has_document_id = "document_id" in raw_payload
        if has_id and has_document_id:
            id_value = ident(raw_payload["id"])
            document_id = ident(raw_payload["document_id"])
            if id_value != document_id:
                raise HandoffError(f"{label} has conflicting document ids")
            document_id = id_value
        elif has_document_id:
            document_id = ident(raw_payload["document_id"])
        else:
            document_id = ident(raw_payload["id"])

        if document_id not in document_ids:
            raise HandoffError(f"{label} references unknown document")
        if document_id in payloads:
            raise HandoffError(f"duplicate payload for {document_id}")

        digest = raw_payload["sha256"]
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise HandoffError(f"{label}.sha256 must be 64 lowercase hex")
        size_bytes = integer(raw_payload["size_bytes"], 1, 100_000_000)

        payloads[document_id] = {
            "id": document_id,
            "document_id": document_id,
            "sha256": digest,
            "size_bytes": size_bytes,
        }

    missing = sorted(set(document_ids) - set(payloads))
    if missing:
        raise HandoffError(f"missing payloads for documents: {missing}")
    # 未知文献与重复 payload 已在逐项解析时拒绝；长度检查提供最终保障。
    if len(payloads) != len(document_ids) or len(raw_payloads) != len(
        document_ids
    ):
        raise HandoffError("payloads must cover every document exactly once")

    return {
        "handoff_id": handoff_id,
        "purpose": purpose,
        "seed_ids": sorted(seeds),
        "payloads": payloads,
        "max_documents_per_batch": max_documents,
        "max_bytes_per_batch": max_bytes,
    }


def _parse_acknowledgements(acknowledgements, handoff_id):
    if acknowledgements is None:
        return []
    if not isinstance(acknowledgements, list):
        raise HandoffError("acknowledgements must be a list")

    parsed = []
    seen_numbers = set()
    for index, ack in enumerate(acknowledgements):
        label = f"acknowledgements[{index}]"
        _exact_fields(ack, _ACK_FIELDS, label)
        if not isinstance(ack["handoff_id"], str) or not ack["handoff_id"]:
            raise HandoffError(f"{label}.handoff_id must be a string")
        batch_number = integer(ack["batch_number"], 1, 10**9)
        digest = ack["manifest_sha256"]
        if not isinstance(digest, str) or not digest:
            raise HandoffError(f"{label}.manifest_sha256 must be a string")
        _non_empty_string(ack["received_at"], f"{label}.received_at")

        if batch_number in seen_numbers:
            raise HandoffError(f"duplicate acknowledged batch {batch_number}")
        seen_numbers.add(batch_number)

        parsed.append(
            {
                "handoff_id": ack["handoff_id"],
                "batch_number": batch_number,
                "manifest_sha256": digest,
                "received_at": ack["received_at"],
            }
        )
    return parsed


def _strong_components(nodes, adjacency, reverse):
    """迭代 Kosaraju，返回按成员 ID 列表字典序排序的 SCC。"""
    visited = set()
    finish_order = []

    for start in sorted(nodes):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(adjacency[start])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                finish_order.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(adjacency[child]))))

    seen = set()
    components = []
    for start in reversed(finish_order):
        if start in seen:
            continue
        seen.add(start)
        pending = [start]
        members = []
        while pending:
            node = pending.pop()
            members.append(node)
            for parent in sorted(reverse[node]):
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        components.append(sorted(members))

    return sorted(components, key=tuple)


def _closure_data(lineage, seeds):
    ancestors = lineage["cited_ancestors"]
    closure_set = set(seeds)
    for seed in seeds:
        closure_set.update(ancestors[seed])

    closure_ids = sorted(closure_set)
    closure_id_set = set(closure_ids)
    citations = [
        dict(citation)
        for citation in lineage["citations"]
        if citation["from"] in closure_id_set
        and citation["to"] in closure_id_set
    ]

    document_edges = {
        (citation["from"], citation["to"]) for citation in citations
    }
    adjacency = {document_id: set() for document_id in closure_ids}
    reverse = {document_id: set() for document_id in closure_ids}
    for source, target in document_edges:
        adjacency[source].add(target)
        reverse[target].add(source)

    components = _strong_components(closure_ids, adjacency, reverse)
    return closure_ids, closure_id_set, citations, document_edges, components


def _build_units(components, payloads, document_edges):
    unit_of_document = {}
    for unit_index, members in enumerate(components):
        unit_id = f"unit_{unit_index + 1}"
        for document_id in members:
            unit_of_document[document_id] = unit_id

    dependencies = {f"unit_{i + 1}": set() for i in range(len(components))}
    dependency_documents = {
        unit_id: set() for unit_id in dependencies
    }
    edge_groups = {unit_id: {} for unit_id in dependencies}

    for source, target in sorted(document_edges):
        source_unit = unit_of_document[source]
        target_unit = unit_of_document[target]
        if source_unit == target_unit:
            continue
        dependencies[source_unit].add(target_unit)
        dependency_documents[source_unit].add(target)
        group = edge_groups[source_unit].setdefault(
            target_unit,
            set(),
        )
        group.add((source, target))

    units = []
    dependency_records = []
    for unit_index, members in enumerate(components):
        unit_id = f"unit_{unit_index + 1}"
        total_bytes = sum(
            payloads[document_id]["size_bytes"] for document_id in members
        )
        dep_ids = sorted(dependencies[unit_id])
        dep_docs = sorted(dependency_documents[unit_id])
        unit = {
            "unit_id": unit_id,
            "document_ids": members,
            # 兼容按“成员/member_ids”读取原子单元的调用方。
            "members": members,
            "member_ids": members,
            "document_count": len(members),
            "total_bytes": total_bytes,
            "contains_cycle": len(members) > 1,
            "kind": (
                "strongly_connected_component"
                if len(members) > 1
                else "single_document"
            ),
            "dependencies": dep_ids,
            "depends_on": dep_ids,
            "depends_on_unit_ids": dep_ids,
            "dependency_document_ids": dep_docs,
        }
        units.append(unit)

        edges = []
        for target_unit in dep_ids:
            for source, target in sorted(
                edge_groups[unit_id][target_unit]
            ):
                edges.append(
                    {
                        "from": source,
                        "to": target,
                        "from_document_id": source,
                        "to_document_id": target,
                    }
                )
        dependency_records.append(
            {
                "unit_id": unit_id,
                "unit": unit_id,
                "document_ids": members,
                "dependencies": dep_ids,
                "depends_on": dep_ids,
                "depends_on_unit_ids": dep_ids,
                "dependency_document_ids": dep_docs,
                "edges": edges,
            }
        )

    return units, dependency_records, unit_of_document, dependencies


def _violations(units, max_documents, max_bytes):
    violations = []
    for unit in units:
        triggered = []
        if unit["document_count"] > max_documents:
            triggered.append("max_documents_per_batch")
        if unit["total_bytes"] > max_bytes:
            triggered.append("max_bytes_per_batch")
        if not triggered:
            continue
        violation = {
            "unit_id": unit["unit_id"],
            "unit": unit["unit_id"],
            "document_ids": unit["document_ids"],
            "members": unit["document_ids"],
            "member_ids": unit["document_ids"],
            "document_count": unit["document_count"],
            "actual_document_count": unit["document_count"],
            "actual_documents": unit["document_count"],
            "total_bytes": unit["total_bytes"],
            "actual_total_bytes": unit["total_bytes"],
            "actual_bytes": unit["total_bytes"],
            "triggered_limits": triggered,
            "violated_limits": triggered,
            "limits": triggered,
            "max_documents_per_batch": max_documents,
            "max_bytes_per_batch": max_bytes,
        }
        if len(triggered) == 1:
            violation["triggered_limit"] = triggered[0]
            violation["violated_limit"] = triggered[0]
            violation["limit"] = triggered[0]
        violations.append(violation)
    return violations


def _document_records(document_ids, lineage):
    documents = lineage["documents"]
    return [
        {
            "id": document_id,
            "date": documents[document_id]["date"],
            "title": documents[document_id]["title"],
        }
        for document_id in document_ids
    ]


def _build_batches(
    components,
    units,
    payloads,
    closure_citations,
    dependencies,
    lineage,
    max_documents,
    max_bytes,
):
    unit_ids = [unit["unit_id"] for unit in units]
    unit_index = {unit_id: i for i, unit_id in enumerate(unit_ids)}
    remaining_dependencies = {
        unit_id: len(dependencies[unit_id]) for unit_id in unit_ids
    }
    required_by = {unit_id: set() for unit_id in unit_ids}
    for source_unit, target_units in dependencies.items():
        for target_unit in target_units:
            required_by[target_unit].add(source_unit)

    scheduled = set()
    batches = []
    batch_number = 1

    while len(scheduled) < len(unit_ids):
        ready = [
            unit_id
            for unit_id in unit_ids
            if unit_id not in scheduled
            and remaining_dependencies[unit_id] == 0
        ]
        # unit_ids 已按成员 ID 列表字典序排列。
        selected = []
        selected_documents = []
        document_count = 0
        total_bytes = 0

        for unit_id in ready:
            unit = units[unit_index[unit_id]]
            next_count = document_count + unit["document_count"]
            next_bytes = total_bytes + unit["total_bytes"]
            if (
                next_count <= max_documents
                and next_bytes <= max_bytes
            ):
                selected.append(unit_id)
                selected_documents.extend(unit["document_ids"])
                document_count = next_count
                total_bytes = next_bytes

        if not selected:  # 防御性检查；原子单元预检后正常图不会发生。
            raise HandoffError("unable to make batch progress")

        selected_document_ids = sorted(selected_documents)
        selected_document_set = set(selected_document_ids)
        batch_citations = [
            dict(citation)
            for citation in closure_citations
            if citation["from"] in selected_document_set
        ]
        batch_payloads = [
            dict(payloads[document_id])
            for document_id in selected_document_ids
        ]
        batch_documents = _document_records(
            selected_document_ids, lineage
        )
        for document in batch_documents:
            document["payload"] = dict(payloads[document["id"]])
        batch = {
            "batch_number": batch_number,
            "document_ids": selected_document_ids,
            "documents": batch_documents,
            "payloads": batch_payloads,
            "citations": batch_citations,
            "document_count": len(selected_document_ids),
            "total_bytes": total_bytes,
        }
        batch["manifest_sha256"] = manifest_sha256(batch)
        batches.append(batch)

        for unit_id in selected:
            scheduled.add(unit_id)
        for unit_id in selected:
            for dependent_unit in required_by[unit_id]:
                remaining_dependencies[dependent_unit] -= 1
        batch_number += 1

    return batches


def _validate_acks_against_plan(parsed_acks, handoff_id, batches):
    batch_by_number = {batch["batch_number"]: batch for batch in batches}
    batch_count = len(batches)
    if len(parsed_acks) > batch_count:
        raise HandoffError("too many acknowledgements")

    for ack in parsed_acks:
        number = ack["batch_number"]
        if not 1 <= number <= batch_count:
            raise HandoffError(f"acknowledged batch out of range: {number}")
        if ack["handoff_id"] != handoff_id:
            raise HandoffError(
                f"acknowledgement handoff_id mismatch for batch {number}"
            )
        expected_digest = batch_by_number[number]["manifest_sha256"]
        if ack["manifest_sha256"] != expected_digest:
            raise HandoffError(
                f"manifest_sha256 mismatch for batch {number}"
            )

    numbers = sorted(ack["batch_number"] for ack in parsed_acks)
    prefix = list(range(1, len(numbers) + 1))
    if numbers != prefix:
        raise HandoffError("acknowledgements must confirm a continuous prefix")

    return sorted(parsed_acks, key=lambda ack: ack["batch_number"])


def _state_collections(
    confirmed_count,
    batches,
    closure_ids,
    closure_citations,
    payloads,
    lineage,
):
    confirmed_batch_records = batches[:confirmed_count]
    unconfirmed_batch_records = batches[confirmed_count:]
    confirmed_batch_refs = [
        BatchReference(batch) for batch in confirmed_batch_records
    ]
    unconfirmed_batch_refs = [
        BatchReference(batch) for batch in unconfirmed_batch_records
    ]
    confirmed_numbers = [
        batch["batch_number"] for batch in confirmed_batch_records
    ]
    unconfirmed_numbers = [
        batch["batch_number"] for batch in unconfirmed_batch_records
    ]

    confirmed_ids = sorted(
        {
            document_id
            for batch in confirmed_batch_records
            for document_id in batch["document_ids"]
        }
    )
    confirmed_set = set(confirmed_ids)
    closure_set = set(closure_ids)
    pending_ids = [
        document_id
        for document_id in closure_ids
        if document_id not in confirmed_set
    ]

    confirmed_citations = [
        dict(citation)
        for citation in lineage["citations"]
        if citation["from"] in confirmed_set
        and citation["from"] in closure_set
        and citation["to"] in closure_set
    ]
    pending_citations = [
        dict(citation)
        for citation in closure_citations
        if citation["from"] not in confirmed_set
    ]

    confirmed_documents = _document_records(confirmed_ids, lineage)
    pending_documents = _document_records(pending_ids, lineage)
    for document in confirmed_documents:
        document["payload"] = dict(payloads[document["id"]])
    for document in pending_documents:
        document["payload"] = dict(payloads[document["id"]])
    confirmed_document_refs = [
        DocumentReference(document) for document in confirmed_documents
    ]
    pending_document_refs = [
        DocumentReference(document) for document in pending_documents
    ]
    confirmed_payloads = [
        dict(payloads[document_id]) for document_id in confirmed_ids
    ]
    pending_payloads = [
        dict(payloads[document_id]) for document_id in pending_ids
    ]

    return {
        # 状态列表使用连续批号；完整批次对象见 *_details 与 plan["batches"]。
        "confirmed_batch_numbers": confirmed_numbers,
        "confirmed_batches": confirmed_batch_refs,
        "acknowledged_batch_numbers": confirmed_numbers,
        "acknowledged_batches": confirmed_batch_refs,
        "accepted_batch_numbers": confirmed_numbers,
        "accepted_batches": confirmed_batch_refs,
        "confirmed_batch_details": confirmed_batch_refs,
        "confirmed_batch_records": confirmed_batch_records,
        "unconfirmed_batch_numbers": unconfirmed_numbers,
        "unconfirmed_batches": unconfirmed_batch_refs,
        "unacknowledged_batch_numbers": unconfirmed_numbers,
        "unacknowledged_batches": unconfirmed_batch_refs,
        "unconfirmed_batch_details": unconfirmed_batch_refs,
        "unconfirmed_batch_records": unconfirmed_batch_records,
        "pending_batch_numbers": unconfirmed_numbers,
        "remaining_batch_numbers": unconfirmed_numbers,
        "pending_batches": unconfirmed_batch_refs,
        "remaining_batches": unconfirmed_batch_refs,
        "pending_batch_details": unconfirmed_batch_refs,
        "pending_batch_records": unconfirmed_batch_records,
        "confirmed_document_ids": confirmed_ids,
        "confirmed_documents": confirmed_document_refs,
        "confirmed_document_details": confirmed_document_refs,
        "confirmed_document_records": confirmed_documents,
        "confirmed_payloads": confirmed_payloads,
        "confirmed_citations": confirmed_citations,
        "delivered_document_ids": confirmed_ids,
        "delivered_citations": confirmed_citations,
        "accepted_document_ids": confirmed_ids,
        "accepted_citations": confirmed_citations,
        "pending_document_ids": pending_ids,
        "pending_documents": pending_document_refs,
        "pending_document_details": pending_document_refs,
        "pending_document_records": pending_documents,
        "pending_payloads": pending_payloads,
        "pending_citations": pending_citations,
        "remaining_document_ids": pending_ids,
        "remaining_citations": pending_citations,
        "outstanding_document_ids": pending_ids,
        "outstanding_citations": pending_citations,
        "unconfirmed_document_ids": pending_ids,
        "unconfirmed_citations": pending_citations,
        "next_batch": (
            unconfirmed_batch_records[0]
            if unconfirmed_batch_records
            else None
        ),
    }


def _base_result(parsed_request, parsed_acks):
    return {
        "handoff_id": parsed_request["handoff_id"],
        "purpose": parsed_request["purpose"],
        "max_documents_per_batch": parsed_request[
            "max_documents_per_batch"
        ],
        "max_bytes_per_batch": parsed_request["max_bytes_per_batch"],
        "acknowledgements": parsed_acks,
        "confirmed_acknowledgements": parsed_acks,
        "accepted_acknowledgements": parsed_acks,
    }


def prepare_handoff(data, request, acknowledgements=None):
    """准备离线档案移交批次并计算回执确认状态。

    该函数不修改 ``data``、``request`` 或 ``acknowledgements``。不可编排的
    原子单元以 ``orderable=False`` 结果返回；非法输入或回执抛
    :class:`HandoffError`（``ValueError`` 子类）。
    """
    if not isinstance(data, dict):
        raise HandoffError("data must be an object matching process contract")

    # process 本身不修改输入；先取得规范谱系，同时完成 data 契约校验。
    lineage = process(data)
    document_ids = list(lineage["documents"])
    parsed_request = _parse_request(request, document_ids)
    parsed_acks = _parse_acknowledgements(
        acknowledgements, parsed_request["handoff_id"]
    )

    seeds = parsed_request["seed_ids"]
    payloads = parsed_request["payloads"]
    (
        closure_ids,
        closure_set,
        closure_citations,
        document_edges,
        components,
    ) = _closure_data(lineage, seeds)

    units, dependency_records, _, dependencies = _build_units(
        components, payloads, document_edges
    )
    violations = _violations(
        units,
        parsed_request["max_documents_per_batch"],
        parsed_request["max_bytes_per_batch"],
    )

    closure_documents = _document_records(closure_ids, lineage)
    closure_data = {
        "documents": closure_documents,
        "citations": closure_citations,
    }
    closure_process = process(closure_data)
    closure_summary = {
        "document_ids": closure_ids,
        "documents": closure_documents,
        "citations": closure_citations,
        "document_count": len(closure_ids),
        "citation_count": len(closure_citations),
        "seed_ids": seeds,
        "process": closure_process,
    }
    pending_document_refs = []
    for document in closure_documents:
        detailed = dict(document)
        detailed["payload"] = dict(payloads[document["id"]])
        pending_document_refs.append(DocumentReference(detailed))

    if violations:
        if parsed_acks:
            # 不可编排结果没有可匹配 manifest 的批次；任何回执都超出批次前缀。
            raise HandoffError(
                "acknowledgements are not accepted for an unorderable handoff"
            )
        result = _base_result(parsed_request, [])
        result.update(
            {
                "orderable": False,
                "feasible": False,
                "planable": False,
                "status": "unorderable",
                "violations": violations,
                "limit_violations": violations,
                "oversized_units": violations,
                "unorderable_units": violations,
                "infeasible_units": violations,
                "closure": closure_summary,
                "closure_document_ids": closure_ids,
                "closure_documents": closure_summary["documents"],
                "closure_citations": closure_citations,
                "atomic_units": units,
                "units": units,
                "unit_dependencies": dependency_records,
                "dependencies": dependency_records,
                "plan": None,
                "handoff_plan": None,
                "batches": [],
                "confirmed_batch_numbers": [],
                "confirmed_batches": [],
                "confirmed_batch_details": [],
                "unconfirmed_batch_numbers": [],
                "unconfirmed_batches": [],
                "pending_batch_numbers": [],
                "pending_batches": [],
                "confirmed_document_ids": [],
                "confirmed_documents": [],
                "confirmed_document_details": [],
                "confirmed_payloads": [],
                "confirmed_citations": [],
                "pending_document_ids": closure_ids,
                "pending_documents": pending_document_refs,
                "pending_document_details": pending_document_refs,
                "pending_document_records": closure_summary["documents"],
                "pending_payloads": [
                    dict(payloads[document_id])
                    for document_id in closure_ids
                ],
                "pending_citations": closure_citations,
                "unconfirmed_document_ids": closure_ids,
                "unconfirmed_citations": closure_citations,
                "next_batch": None,
                "all_confirmed": False,
                "process": lineage,
                "full_process": lineage,
                "lineage": lineage,
                "closure_process": closure_process,
            }
        )
        result["confirmed"] = {
            "batch_numbers": [],
            "batches": [],
            "batch_details": [],
            "document_ids": [],
            "documents": [],
            "document_details": [],
            "payloads": [],
            "citations": [],
        }
        result["acknowledged"] = result["confirmed"]
        result["pending"] = {
            "batch_numbers": [],
            "batches": [],
            "batch_details": [],
            "document_ids": closure_ids,
            "documents": pending_document_refs,
            "document_details": pending_document_refs,
            "payloads": result["pending_payloads"],
            "citations": closure_citations,
            "next_batch": None,
        }
        result["unconfirmed"] = result["pending"]
        return result

    batches = _build_batches(
        components,
        units,
        payloads,
        closure_citations,
        dependencies,
        lineage,
        parsed_request["max_documents_per_batch"],
        parsed_request["max_bytes_per_batch"],
    )
    valid_acks = _validate_acks_against_plan(
        parsed_acks, parsed_request["handoff_id"], batches
    )

    dependency_map = {
        unit["unit_id"]: list(unit["dependencies"]) for unit in units
    }
    plan = {
        "handoff_id": parsed_request["handoff_id"],
        "purpose": parsed_request["purpose"],
        "seed_ids": seeds,
        "max_documents_per_batch": parsed_request[
            "max_documents_per_batch"
        ],
        "max_bytes_per_batch": parsed_request["max_bytes_per_batch"],
        "closure": closure_summary,
        "closure_document_ids": closure_ids,
        "closure_documents": closure_summary["documents"],
        "closure_citations": closure_citations,
        "atomic_units": units,
        "units": units,
        "unit_dependencies": dependency_records,
        "dependencies": dependency_records,
        "dependency_graph": dependency_map,
        "unit_dependency_map": dependency_map,
        "batches": batches,
        "batch_count": len(batches),
        "process": lineage,
        "full_process": lineage,
        "lineage": lineage,
        "closure_process": closure_process,
    }

    state = _state_collections(
        len(valid_acks),
        batches,
        closure_ids,
        closure_citations,
        payloads,
        lineage,
    )
    result = _base_result(parsed_request, valid_acks)
    result.update(
        {
            "orderable": True,
            "feasible": True,
            "planable": True,
            "status": "orderable",
            "violations": [],
            "plan": plan,
            "handoff_plan": plan,
            "batches": batches,
            "batch_count": len(batches),
            "all_confirmed": len(valid_acks) == len(batches),
            "process": lineage,
            "full_process": lineage,
            "lineage": lineage,
            "closure_process": closure_process,
        }
    )
    result.update(state)
    result["confirmed"] = {
        "batch_numbers": result["confirmed_batch_numbers"],
        "batches": result["confirmed_batches"],
        "batch_details": result["confirmed_batch_details"],
        "document_ids": result["confirmed_document_ids"],
        "documents": result["confirmed_documents"],
        "document_details": result["confirmed_document_details"],
        "payloads": result["confirmed_payloads"],
        "citations": result["confirmed_citations"],
    }
    result["acknowledged"] = result["confirmed"]
    result["pending"] = {
        "batch_numbers": result["pending_batch_numbers"],
        "batches": result["pending_batches"],
        "batch_details": result["pending_batch_details"],
        "document_ids": result["pending_document_ids"],
        "documents": result["pending_documents"],
        "document_details": result["pending_document_details"],
        "payloads": result["pending_payloads"],
        "citations": result["pending_citations"],
        "next_batch": result["next_batch"],
    }
    result["unconfirmed"] = result["pending"]
    return result

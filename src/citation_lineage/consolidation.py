"""多来源档案目录归并。

本模块只依据显式的 ``same`` / ``different`` 身份声明构造身份分量，不做标题、
日期相似度或外部查询。校验通过后，发布分量可直接交给
:func:`citation_lineage.service.process`；隔离分量及其引用阻断证据全部保留。
"""

from __future__ import annotations

from .checks import ident
from .service import process, text

MAX_CATALOGS = 50
MAX_LOCAL_DOCUMENTS = 5000
MAX_RAW_CITATIONS = 25000
MAX_IDENTITY_CLAIMS = 10000

# 现有 process 契约的上限。归并后的发布目录不能靠截断绕过这些限制。
MAX_PROCESS_DOCUMENTS = 1000
MAX_PROCESS_CITATIONS = 5000


class ConsolidationError(ValueError):
    """归并输入或归并业务边界不合法。"""


class ConsolidationLimitError(ConsolidationError):
    """归并结果超过现有 ``process`` 契约上限。"""


_CATALOG_FIELDS = frozenset(
    {"source_id", "authority_rank", "documents", "citations"}
)
_DOCUMENT_FIELDS = frozenset({"local_id", "date", "title"})
_CITATION_FIELDS = frozenset({"from", "to", "page"})
_CLAIM_FIELDS = frozenset(
    {"claim_id", "left", "right", "relation", "evidence"}
)
_LOCATOR_FIELDS = frozenset({"source_id", "local_id"})


def _exact_fields(value, expected, label):
    if not isinstance(value, dict):
        raise ConsolidationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConsolidationError(
            f"{label} has invalid fields; missing={missing}, extra={extra}"
        )
    return value


def _non_negative_int(value, label):
    # bool 是 int 的子类，但在档案契约中不是合法整数。
    if type(value) is not int or value < 0:
        raise ConsolidationError(f"{label} must be a non-negative integer")
    return value


def _non_empty_string(value, label):
    if not isinstance(value, str) or not value:
        raise ConsolidationError(f"{label} must be a non-empty string")
    return value


def _locator_key(value):
    if isinstance(value, dict):
        _exact_fields(value, _LOCATOR_FIELDS, "record locator")
        source_id = value["source_id"]
        local_id = value["local_id"]
    elif isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ConsolidationError(
                "record locator pair must contain source_id and local_id"
            )
        source_id, local_id = value
    elif isinstance(value, str):
        # Convenience for callers using the joined canonical candidate. The
        # contract's complete locator is normally an object or (source, local).
        matches = []
        for position, char in enumerate(value):
            if char == ":":
                matches.append((value[:position], value[position + 1:]))
        if len(matches) == 1:
            source_id, local_id = matches[0]
        else:
            raise ConsolidationError(
                "string record locator must be an unambiguous "
                "'source_id:local_id' value"
            )
    else:
        raise ConsolidationError(
            "record locator must be an object or (source_id, local_id) pair"
        )

    if not isinstance(source_id, str) or not isinstance(local_id, str):
        raise ConsolidationError(
            "record locator source_id and local_id must be strings"
        )
    return source_id, local_id


def _canonical_identifier(source_id, local_id):
    candidate = f"{source_id}:{local_id}"
    try:
        return ident(candidate)
    except ValueError as exc:
        raise ConsolidationError(
            f"canonical candidate {candidate!r} is not a valid literature id"
        ) from exc


def _parse_catalogs(catalogs):
    if not isinstance(catalogs, list) or not (
        1 <= len(catalogs) <= MAX_CATALOGS
    ):
        raise ConsolidationError(
            f"catalogs must contain 1..{MAX_CATALOGS} source catalogs"
        )

    records = []
    records_by_key = {}
    raw_citations = []
    seen_source_ids = set()
    total_documents = 0
    total_citations = 0

    for catalog_index, catalog in enumerate(catalogs):
        label = f"catalog[{catalog_index}]"
        _exact_fields(catalog, _CATALOG_FIELDS, label)

        source_id = catalog["source_id"]
        if not isinstance(source_id, str):
            raise ConsolidationError(f"{label}.source_id must be a string")
        if source_id in seen_source_ids:
            raise ConsolidationError(f"duplicate source_id: {source_id!r}")
        seen_source_ids.add(source_id)

        authority_rank = _non_negative_int(
            catalog["authority_rank"], f"{label}.authority_rank"
        )

        documents = catalog["documents"]
        citations = catalog["citations"]
        if not isinstance(documents, list):
            raise ConsolidationError(f"{label}.documents must be a list")
        if not isinstance(citations, list):
            raise ConsolidationError(f"{label}.citations must be a list")

        total_documents += len(documents)
        if total_documents > MAX_LOCAL_DOCUMENTS:
            raise ConsolidationError(
                "catalogs contain more than "
                f"{MAX_LOCAL_DOCUMENTS} local documents"
            )

        total_citations += len(citations)
        if total_citations > MAX_RAW_CITATIONS:
            raise ConsolidationError(
                "catalogs contain more than "
                f"{MAX_RAW_CITATIONS} raw citations"
            )

        local_ids = set()
        for document_index, document in enumerate(documents):
            doc_label = f"{label}.documents[{document_index}]"
            _exact_fields(document, _DOCUMENT_FIELDS, doc_label)

            local_id = document["local_id"]
            if not isinstance(local_id, str):
                raise ConsolidationError(
                    f"{doc_label}.local_id must be a string"
                )
            if local_id in local_ids:
                raise ConsolidationError(
                    f"duplicate local_id {local_id!r} in source {source_id!r}"
                )
            local_ids.add(local_id)

            canonical_id = _canonical_identifier(source_id, local_id)
            date = text(document["date"])
            title = text(document["title"])
            key = (source_id, local_id)
            record_index = len(records)
            record = {
                "index": record_index,
                "key": key,
                "source_id": source_id,
                "local_id": local_id,
                "canonical_id": canonical_id,
                "authority_rank": authority_rank,
                "date": date,
                "title": title,
            }
            records.append(record)
            records_by_key[key] = record_index

        for citation_index, citation in enumerate(citations):
            cit_label = f"{label}.citations[{citation_index}]"
            _exact_fields(citation, _CITATION_FIELDS, cit_label)

            from_id = citation["from"]
            to_id = citation["to"]
            if not isinstance(from_id, str) or from_id not in local_ids:
                raise ConsolidationError(
                    f"{cit_label}.from does not reference a local document"
                )
            if not isinstance(to_id, str) or to_id not in local_ids:
                raise ConsolidationError(
                    f"{cit_label}.to does not reference a local document"
                )
            page = text(citation["page"])
            raw_citations.append(
                {
                    "source_id": source_id,
                    "from": from_id,
                    "to": to_id,
                    "page": page,
                }
            )

    return records, records_by_key, raw_citations


def _parse_claims(identity_claims, records_by_key):
    if not isinstance(identity_claims, list) or len(identity_claims) > (
        MAX_IDENTITY_CLAIMS
    ):
        raise ConsolidationError(
            "identity_claims must be a list with at most "
            f"{MAX_IDENTITY_CLAIMS} items"
        )

    claims = []
    seen_claim_ids = set()
    seen_pair_relations = set()

    for claim_index, claim in enumerate(identity_claims):
        label = f"identity_claims[{claim_index}]"
        _exact_fields(claim, _CLAIM_FIELDS, label)

        claim_id = claim["claim_id"]
        if (
            isinstance(claim_id, bool)
            or not isinstance(claim_id, (str, int))
            or (isinstance(claim_id, str) and not claim_id)
        ):
            raise ConsolidationError(
                f"{label}.claim_id must be a non-empty string or integer"
            )
        if claim_id in seen_claim_ids:
            raise ConsolidationError(f"duplicate claim_id: {claim_id!r}")
        seen_claim_ids.add(claim_id)

        relation = claim["relation"]
        if relation not in ("same", "different"):
            raise ConsolidationError(
                f"{label}.relation must be 'same' or 'different'"
            )

        evidence = claim["evidence"]
        if not isinstance(evidence, str) or not evidence:
            raise ConsolidationError(
                f"{label}.evidence must be a non-empty string"
            )

        left_key = _locator_key(claim["left"])
        right_key = _locator_key(claim["right"])
        if left_key not in records_by_key:
            raise ConsolidationError(
                f"{label}.left references a non-existent source document"
            )
        if right_key not in records_by_key:
            raise ConsolidationError(
                f"{label}.right references a non-existent source document"
            )

        if relation == "different" and left_key == right_key:
            raise ConsolidationError(
                f"{label}: a record cannot be declared different from itself"
            )

        unordered_pair = tuple(sorted((left_key, right_key)))
        pair_relation = (unordered_pair, relation)
        if pair_relation in seen_pair_relations:
            raise ConsolidationError(
                f"duplicate {relation} claim for record pair "
                f"{list(unordered_pair)!r}"
            )
        seen_pair_relations.add(pair_relation)

        claims.append(
            {
                "claim_id": claim_id,
                "relation": relation,
                "evidence": evidence,
                "left_key": left_key,
                "right_key": right_key,
                "left_index": records_by_key[left_key],
                "right_index": records_by_key[right_key],
            }
        )

    return claims


class _DSU:
    def __init__(self, size):
        self.parent = list(range(size))
        self.size = [1] * size

    def find(self, value):
        parent = self.parent
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            nxt = parent[value]
            parent[value] = root
            value = nxt
        return root

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return left_root
        if self.size[left_root] < self.size[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size[right_root]
        return left_root


def _locator_public(record):
    return {
        "source_id": record["source_id"],
        "local_id": record["local_id"],
    }


def _scalar_id_sort_key(value):
    # Valid claim IDs are strings or integers; keep mixed-type collections
    # deterministic without relying on Python cross-type comparison.
    if isinstance(value, str):
        return (0, value)
    if isinstance(value, int) and not isinstance(value, bool):
        return (1, value)
    return (2, str(value))


def _claim_public(claim):
    left_source, left_local = claim["left_key"]
    right_source, right_local = claim["right_key"]
    return {
        "claim_id": claim["claim_id"],
        "relation": claim["relation"],
        "left": {"source_id": left_source, "local_id": left_local},
        "right": {"source_id": right_source, "local_id": right_local},
        "evidence": claim["evidence"],
    }


def _sorted_claim_public(claims):
    return [_claim_public(claim) for claim in sorted(
        claims, key=lambda item: _scalar_id_sort_key(item["claim_id"])
    )]


def _field_evidence(members, field_name):
    observations = []
    for record in sorted(
        members, key=lambda item: (item["source_id"], item["local_id"])
    ):
        observations.append(
            {
                "source_id": record["source_id"],
                "local_id": record["local_id"],
                "id": record["canonical_id"],
                "authority_rank": record["authority_rank"],
                "rank": record["authority_rank"],
                "value": record[field_name],
            }
        )

    strongest_rank = min(item["authority_rank"] for item in observations)
    strongest = [
        item for item in observations
        if item["authority_rank"] == strongest_rank
    ]
    strongest_values = sorted({item["value"] for item in strongest})
    weaker = [
        item for item in observations
        if item["authority_rank"] > strongest_rank
    ]
    weaker_values = sorted({item["value"] for item in weaker})

    base = {
        "field": field_name,
        "rule": "minimum authority_rank; a value is selected only when all "
        "observations at that strongest rank agree",
        "authority_rank": strongest_rank,
        "strongest_authority_rank": strongest_rank,
        "observations": observations,
        "strongest_observations": strongest,
        "weaker_observations": weaker,
        "weaker_distinct_values": weaker_values,
    }

    if len(strongest_values) > 1:
        values = []
        for value in strongest_values:
            values.append(
                {
                    "value": value,
                    "authority_rank": strongest_rank,
                    "records": [
                        item["id"]
                        for item in strongest
                        if item["value"] == value
                    ],
                }
            )
        base.update(
            {
                "status": "conflict",
                "conflict": True,
                "selected": None,
                "selected_value": None,
                "selection_basis": (
                    f"authority_rank {strongest_rank} is the strongest level, "
                    "but it contains multiple distinct values"
                ),
                "distinct_values": strongest_values,
                "conflicting_values": values,
            }
        )
    else:
        selected = strongest_values[0]
        base.update(
            {
                "status": "selected",
                "conflict": False,
                "selected": selected,
                "selected_value": selected,
                "selection_basis": (
                    f"selected the unique value at strongest "
                    f"authority_rank {strongest_rank}; weaker observations "
                    "are retained as evidence"
                ),
            }
        )

    # Common spelling aliases for downstream evidence consumers.
    base["value"] = base["selected_value"]
    base["chosen_value"] = base["selected_value"]
    base["basis"] = base["selection_basis"]
    base["reason"] = base["selection_basis"]
    base["selection_reason"] = base["selection_basis"]
    return base


def _build_components(records, claims, dsu):
    groups = {}
    for record in records:
        root = dsu.find(record["index"])
        groups.setdefault(root, []).append(record)

    components = []
    for members in groups.values():
        ordered_members = sorted(
            members, key=lambda item: (item["source_id"], item["local_id"])
        )
        representative = min(
            ordered_members,
            key=lambda item: (
                item["authority_rank"],
                item["source_id"],
                item["local_id"],
            ),
        )
        component_id = representative["canonical_id"]
        date_evidence = _field_evidence(ordered_members, "date")
        title_evidence = _field_evidence(ordered_members, "title")

        component = {
            "component_id": component_id,
            "representative": representative,
            "members": ordered_members,
            "date_evidence": date_evidence,
            "title_evidence": title_evidence,
            "same_claims": [],
            "different_claims": [],
        }
        components.append(component)

    by_root = {}
    for component in components:
        root = dsu.find(component["representative"]["index"])
        # All member roots are equal after find; use representative root.
        by_root[root] = component

    cross_different_claims = []
    for claim in claims:
        left_root = dsu.find(claim["left_index"])
        right_root = dsu.find(claim["right_index"])
        component = by_root[left_root]
        if claim["relation"] == "same":
            component["same_claims"].append(claim)
        elif left_root == right_root:
            component["different_claims"].append(claim)
        else:
            cross_different_claims.append(claim)

    components.sort(key=lambda item: item["component_id"])
    return components, by_root, cross_different_claims


def _component_public(component):
    members = component["members"]
    representative = component["representative"]
    component_id = component["component_id"]
    member_ids = sorted(record["canonical_id"] for record in members)
    aliases = [item for item in member_ids if item != component_id]
    date_evidence = component["date_evidence"]
    title_evidence = component["title_evidence"]

    identity_conflict = bool(component["different_claims"])
    field_conflicts = []
    if date_evidence["conflict"]:
        field_conflicts.append(date_evidence)
    if title_evidence["conflict"]:
        field_conflicts.append(title_evidence)

    conflicts = []
    conflict_types = []
    if identity_conflict:
        different = _sorted_claim_public(component["different_claims"])
        same = _sorted_claim_public(component["same_claims"])
        identity_obj = {
            "type": "identity",
            "conflict_type": "identity_conflict",
            "status": "conflict",
            "reason": (
                "a 'different' claim has both endpoints in the same "
                "transitive 'same' identity component"
            ),
            "different_claims": different,
            "conflicting_claims": different,
            "same_claims": same,
            "claim_ids": [item["claim_id"] for item in different],
        }
        conflicts.append(identity_obj)
        conflict_types.append("identity")

    for evidence in field_conflicts:
        field_name = evidence["field"]
        field_obj = dict(evidence)
        field_obj["type"] = "field"
        field_obj["conflict_type"] = "field_conflict"
        conflicts.append(field_obj)
        conflict_types.append(f"field:{field_name}")

    publishable = not identity_conflict and not field_conflicts

    member_locations = [_locator_public(record) for record in members]
    representative_locator = _locator_public(representative)
    component_claim_evidence = _sorted_claim_public(
        component["same_claims"] + component["different_claims"]
    )
    alias_locations = [
        _locator_public(record)
        for record in members
        if record["canonical_id"] != component_id
    ]
    member_records = []
    for record in members:
        member_records.append(
            {
                "source_id": record["source_id"],
                "local_id": record["local_id"],
                "id": record["canonical_id"],
                "canonical_id": record["canonical_id"],
                "authority_rank": record["authority_rank"],
                "date": record["date"],
                "title": record["title"],
            }
        )

    result = {
        "status": "published" if publishable else "quarantined",
        "publishable": publishable,
        "component_id": component_id,
        "canonical_id": component_id,
        "representative_id": component_id,
        "published_canonical_id": component_id if publishable else None,
        "canonical_record": representative_locator,
        "locator": representative_locator,
        "representative_locator": representative_locator,
        "authority_rank": representative["authority_rank"],
        "members": member_ids,
        "member_ids": member_ids,
        "member_locations": member_locations,
        "records": member_records,
        "aliases": aliases,
        "alias_ids": aliases,
        "alias_locations": alias_locations,
        "all_identifiers": member_ids,
        "same_claims": _sorted_claim_public(component["same_claims"]),
        "different_claims": _sorted_claim_public(
            component["different_claims"]
        ),
        "claims": component_claim_evidence,
        "identity_claims": component_claim_evidence,
        "conflicting_claims": _sorted_claim_public(
            component["different_claims"]
        ),
        "identity_conflict": conflicts[0] if identity_conflict else None,
        "field_evidence": {
            "date": date_evidence,
            "title": title_evidence,
        },
        "observations": {
            "date": date_evidence["observations"],
            "title": title_evidence["observations"],
        },
        "selection_basis": {
            "date": date_evidence["selection_basis"],
            "title": title_evidence["selection_basis"],
        },
        "fields": {
            "date": date_evidence,
            "title": title_evidence,
        },
        "field_conflicts": field_conflicts,
        "field_conflicts_by_field": {
            "date": date_evidence if date_evidence["conflict"] else None,
            "title": title_evidence if title_evidence["conflict"] else None,
        },
        "conflicts": conflicts,
        "conflict_types": conflict_types,
        "conflict_reasons": conflict_types,
        "conflict_type_codes": conflict_types,
        "conflict_kinds": sorted(
            {"identity" if item["type"] == "identity" else "field"
             for item in conflicts}
        ),
        "conflict_fields": [
            item["field"] for item in field_conflicts
        ],
        "has_identity_conflict": identity_conflict,
        "has_field_conflict": bool(field_conflicts),
    }

    if publishable:
        result.update(
            {
                "id": component_id,
                "date": date_evidence["selected_value"],
                "title": title_evidence["selected_value"],
                "selected_date": date_evidence["selected_value"],
                "selected_title": title_evidence["selected_value"],
            }
        )
    else:
        result.update(
            {
                "id": None,
                "date": date_evidence["selected_value"],
                "title": title_evidence["selected_value"],
                "selected_date": date_evidence["selected_value"],
                "selected_title": title_evidence["selected_value"],
            }
        )
    return result


def _citation_origin(citation):
    return {
        "source_id": citation["source_id"],
        "from": citation["from"],
        "to": citation["to"],
        "page": citation["page"],
    }


def _compact_endpoint_conflict(component_public):
    return {
        "component_id": component_public["component_id"],
        "canonical_id": component_public["canonical_id"],
        "publishable": component_public["publishable"],
        "conflict_types": list(component_public["conflict_types"]),
        "conflict_kinds": list(component_public["conflict_kinds"]),
        "conflict_fields": list(component_public["conflict_fields"]),
        "identity_conflict": component_public["identity_conflict"] is not None,
        "field_conflicts": [
            {
                "field": item["field"],
                "authority_rank": item["authority_rank"],
                "distinct_values": item["distinct_values"],
            }
            for item in component_public["field_conflicts"]
        ],
    }


def consolidate(catalogs, identity_claims=None):
    """归并多来源档案目录并计算现有谱系结果。

    ``identity_claims`` 可省略，等价于空声明列表。参数与返回结构见
    ``docs/consolidation.md``。任何输入或业务上限错误均抛出
    :class:`ValueError`，不会返回部分归并结果，也不会修改调用方输入。
    """
    if identity_claims is None:
        identity_claims = []
    records, records_by_key, raw_citations = _parse_catalogs(catalogs)
    claims = _parse_claims(identity_claims, records_by_key)

    dsu = _DSU(len(records))
    for claim in claims:
        if claim["relation"] == "same":
            dsu.union(claim["left_index"], claim["right_index"])

    raw_components, _roots, cross_different_claims = _build_components(
        records, claims, dsu
    )
    component_publics = [
        _component_public(component) for component in raw_components
    ]
    component_by_id = {
        item["component_id"]: item for item in component_publics
    }

    # record index -> component id
    component_id_by_index = {}
    for component in raw_components:
        cid = component["component_id"]
        for record in component["members"]:
            component_id_by_index[record["index"]] = cid

    published = [
        item for item in component_publics if item["publishable"]
    ]
    quarantined = [
        item for item in component_publics if not item["publishable"]
    ]

    if len(published) > MAX_PROCESS_DOCUMENTS:
        raise ConsolidationLimitError(
            f"{len(published)} publishable canonical documents exceed "
            f"process limit {MAX_PROCESS_DOCUMENTS}"
        )

    canonical_documents = [
        {
            "id": item["canonical_id"],
            "date": item["date"],
            "title": item["title"],
        }
        for item in sorted(published, key=lambda item: item["canonical_id"])
    ]

    blocked_citations = []
    internal_citation_evidence = []
    citation_groups = {}

    for citation in raw_citations:
        source_id = citation["source_id"]
        from_index = records_by_key[(source_id, citation["from"])]
        to_index = records_by_key[(source_id, citation["to"])]
        from_component_id = component_id_by_index[from_index]
        to_component_id = component_id_by_index[to_index]
        from_component = component_by_id[from_component_id]
        to_component = component_by_id[to_component_id]
        origin = _citation_origin(citation)

        if not from_component["publishable"] or not to_component["publishable"]:
            endpoint_conflicts = []
            blocked_endpoints = []
            if not from_component["publishable"]:
                blocked_endpoints.append("from")
                endpoint_conflicts.append(
                    {
                        "endpoint": "from",
                        "record": _locator_public(records[from_index]),
                        **_compact_endpoint_conflict(from_component),
                    }
                )
            if not to_component["publishable"]:
                blocked_endpoints.append("to")
                endpoint_conflicts.append(
                    {
                        "endpoint": "to",
                        "record": _locator_public(records[to_index]),
                        **_compact_endpoint_conflict(to_component),
                    }
                )
            conflict_types = sorted({
                conflict_type
                for endpoint in endpoint_conflicts
                for conflict_type in endpoint["conflict_types"]
            })
            blocked_citations.append(
                {
                    "status": "blocked",
                    "reason": "one or both endpoints belong to a quarantined "
                    "identity or field conflict component",
                    "source_id": source_id,
                    "from": citation["from"],
                    "to": citation["to"],
                    "page": citation["page"],
                    "origin": origin,
                    "source_location": origin,
                    "from_record": _locator_public(records[from_index]),
                    "to_record": _locator_public(records[to_index]),
                    "projected_from": from_component_id,
                    "projected_to": to_component_id,
                    "canonical_from": from_component_id,
                    "canonical_to": to_component_id,
                    "blocked_endpoints": blocked_endpoints,
                    "blocked_by": blocked_endpoints,
                    "endpoint_conflicts": endpoint_conflicts,
                    "conflicts": endpoint_conflicts,
                    "blocking_conflicts": endpoint_conflicts,
                    "blocked_by_conflicts": endpoint_conflicts,
                    "conflict_types": conflict_types,
                    "conflict_reasons": conflict_types,
                }
            )
            continue

        if from_component_id == to_component_id:
            internal_citation_evidence.append(
                {
                    "status": "internal",
                    "reason": "both endpoints project to one canonical document",
                    "source_id": source_id,
                    "from": citation["from"],
                    "to": citation["to"],
                    "page": citation["page"],
                    "origin": origin,
                    "source_location": origin,
                    "canonical_id": from_component_id,
                    "from_canonical_id": from_component_id,
                    "to_canonical_id": to_component_id,
                    "source_locations": [origin],
                }
            )
            continue

        key = (from_component_id, to_component_id, citation["page"])
        group = citation_groups.get(key)
        if group is None:
            group = {
                "from": from_component_id,
                "to": to_component_id,
                "page": citation["page"],
                "sources": set(),
                "source_locations": [],
            }
            citation_groups[key] = group
        group["sources"].add(source_id)
        group["source_locations"].append(origin)

    blocked_citations.sort(
        key=lambda item: (
            item["source_id"], item["from"], item["to"], item["page"]
        )
    )
    internal_citation_evidence.sort(
        key=lambda item: (
            item["source_id"], item["from"], item["to"], item["page"]
        )
    )

    citation_evidence = []
    canonical_citations = []
    for key in sorted(citation_groups):
        from_id, to_id, page = key
        group = citation_groups[key]
        origins = sorted(
            group["source_locations"],
            key=lambda item: (
                item["source_id"], item["from"], item["to"], item["page"]
            ),
        )
        source_ids = sorted(group["sources"])
        entry = {
            "from": from_id,
            "to": to_id,
            "page": page,
            "sources": source_ids,
            "source_count": len(source_ids),
            "raw_citation_count": len(origins),
            "source_locations": origins,
            "source_locators": origins,
            "original_locations": origins,
            "origins": origins,
            "provenance": origins,
        }
        citation_evidence.append(entry)
        canonical_citations.append(
            {"from": from_id, "to": to_id, "page": page}
        )

    if len(canonical_citations) > MAX_PROCESS_CITATIONS:
        raise ConsolidationLimitError(
            f"{len(canonical_citations)} aggregated canonical citations "
            f"exceed process limit {MAX_PROCESS_CITATIONS}"
        )

    canonical_catalog = {
        "documents": canonical_documents,
        "citations": canonical_citations,
    }
    if canonical_documents:
        lineage = process(canonical_catalog)
    else:
        lineage = None

    identity_claim_evidence = []
    for claim in sorted(
        claims, key=lambda item: _scalar_id_sort_key(item["claim_id"])
    ):
        public = _claim_public(claim)
        left_cid = component_id_by_index[claim["left_index"]]
        right_cid = component_id_by_index[claim["right_index"]]
        public["left_component_id"] = left_cid
        public["right_component_id"] = right_cid
        if claim["relation"] == "same":
            public["classification"] = "same_identity_component"
            public["component_id"] = left_cid
            public["conflict"] = False
        elif left_cid == right_cid:
            public["classification"] = "identity_conflict"
            public["component_id"] = left_cid
            public["conflict"] = True
        else:
            public["classification"] = "distinct_components"
            public["component_id"] = None
            public["conflict"] = False
        identity_claim_evidence.append(public)

    document_evidence_map = {
        item["canonical_id"]: item for item in published
    }
    aliases_mapping = {
        item["canonical_id"]: list(item["aliases"])
        for item in published
    }
    field_evidence_mapping = {
        item["canonical_id"]: item["field_evidence"]
        for item in published
    }

    result = {
        # 可直接交给 process 的规范输入。
        "documents": canonical_documents,
        "citations": canonical_citations,
        "canonical_documents": canonical_documents,
        "canonical_citations": canonical_citations,
        "canonical_catalog": canonical_catalog,
        "canonical_input": canonical_catalog,
        "canonical": canonical_catalog,

        # 文献、别名与字段观察证据。
        "document_evidence": published,
        "canonical_document_evidence": published,
        "document_evidence_by_id": document_evidence_map,
        "aliases": aliases_mapping,
        "alias_map": aliases_mapping,
        "field_evidence": field_evidence_mapping,

        # 全部分量与隔离分量。
        "components": component_publics,
        "identity_components": component_publics,
        "quarantined_components": quarantined,
        "quarantine": quarantined,
        "quarantined": quarantined,
        "isolated_components": quarantined,
        "conflicted_components": quarantined,

        # 引用证据。
        "citation_evidence": citation_evidence,
        "citation_provenance": citation_evidence,
        "aggregated_citations": citation_evidence,
        "canonical_citation_evidence": citation_evidence,
        "blocked_citations": blocked_citations,
        "internal_citation_evidence": internal_citation_evidence,

        # 声明分类证据。
        "identity_claim_evidence": identity_claim_evidence,
        "identity_claims": identity_claim_evidence,
        "distinct_different_claims": _sorted_claim_public(
            cross_different_claims
        ),

        # 现有 process 的完整结果；无可发布文献时按要求为 null。
        "process": lineage,
        "process_result": lineage,
        "lineage": lineage,
        "lineage_result": lineage,

        "summary": {
            "source_catalogs": len(catalogs),
            "local_documents": len(records),
            "raw_citations": len(raw_citations),
            "identity_claims": len(claims),
            "identity_components": len(component_publics),
            "published_documents": len(published),
            "quarantined_components": len(quarantined),
            "canonical_citations": len(canonical_citations),
            "aggregated_citation_locators": len(citation_evidence),
            "blocked_citations": len(blocked_citations),
            "internal_citations": len(internal_citation_evidence),
            "process_documents": 0 if lineage is None
            else lineage["summary"]["documents"],
            "process_citations": 0 if lineage is None
            else lineage["summary"]["citations"],
        },
    }
    return result


__all__ = [
    "consolidate",
    "ConsolidationError",
    "ConsolidationLimitError",
    "MAX_CATALOGS",
    "MAX_LOCAL_DOCUMENTS",
    "MAX_RAW_CITATIONS",
    "MAX_IDENTITY_CLAIMS",
]

"""档案数字载体的固化副本修复规划。

档案机构的同一篇文献可能在多个存储站点持有数字载体副本；定期校验会发现副本
损坏（摘要不一致）、不可读（``observed_sha256 = null``）或所在站点离线。本模块
在现有 :func:`citation_lineage.service.process` 规范引用图之上，为一组重点文献
及其沿引用方向可达的全部被引证据，共同求解满足故障域分散要求的修复计划。

核心规则（详见 ``docs/preservation.md``）：

* 副本状态只由本次输入确定：站点 ``online`` 且观测摘要等于登记摘要才是
  ``healthy``；``online`` 但摘要不同是 ``corrupt``；站点离线或观测摘要为
  ``null`` 是 ``unavailable``。绝不进行多数表决，corrupt/unavailable 副本既不
  能改变登记摘要，也不能作为复制源。
* 保护闭包为 ``{seed_ids} ∪ cited_ancestors[seed]``；闭包外文献不参与修复
  分配，但其当前副本审计摘要仍完整保留。
* 每个仍缺的故障域恰好规划一个动作：把文献从现有 healthy 源复制到一个
  online、尚无该文献 healthy 副本且属于未覆盖故障域的站点。目标已有
  corrupt/unavailable 记录时动作为 ``replace``，否则为 ``create``。
* 规划对整个闭包共同求解：任何一篇文献无 healthy 源、单篇可用故障域不足，或
  全局站点 slot 容量无法同时满足所有文献，均返回 ``status="blocked"`` 且不给
  出任何部分动作。
* 可行时所有方案动作数相同（等于各文献缺失故障域数之和）；先最小化总
  ``write_cost``，再按动作序列字典序决胜。实现用一个**向量费用最小费用流**
  （SSP 逐单位最短路增广 + Dijkstra + Johnson 势能）求解：第 0 分量是写入
  成本，其后每个保护文献一个有界字典序分量，站点到汇点的容量即共享 slot，
  全程不枚举站点分配组合，也不为每个 seed 重扫整张图。

本模块只读取调用方输入，绝不修改 ``data`` / ``assets`` / ``sites`` /
``replicas`` / ``request``，不访问网络或外部服务，仅使用 Python 标准库。
"""

from __future__ import annotations

import heapq
import re

from .checks import array, ident, integer
from .service import process, text

MAX_ASSETS = 1000
MAX_SITES = 100
MAX_REPLICAS = 10_000
MAX_SEEDS = 100
MAX_REQUIRED_DOMAINS = 10
MAX_SIZE_BYTES = 100_000_000
MAX_WRITE_SLOTS = 10_000
MAX_WRITE_COST = 1_000_000

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

_ASSET_FIELDS = frozenset({"id", "sha256", "size_bytes"})
_SITE_FIELDS = frozenset(
    {"site_id", "failure_domain", "status", "write_slots", "write_cost"}
)
_REPLICA_FIELDS = frozenset(
    {"replica_id", "document_id", "site_id", "observed_sha256", "checked_at"}
)
_REQUEST_FIELDS = frozenset(
    {"preservation_id", "seed_ids", "required_failure_domains", "reason"}
)

_STATUS_ONLINE = "online"
_STATUS_OFFLINE = "offline"

_REPLICA_HEALTHY = "healthy"
_REPLICA_CORRUPT = "corrupt"
_REPLICA_UNAVAILABLE = "unavailable"

_ACTION_CREATE = "create"
_ACTION_REPLACE = "replace"

_PLANNED = "planned"
_BLOCKED = "blocked"

# blocked 结构化违规的三类稳定代码。
_VIOLATION_NO_SOURCE = "no_healthy_source"
_VIOLATION_INSUFFICIENT_DOMAINS = "insufficient_failure_domains"
_VIOLATION_SLOT_CAPACITY = "slot_capacity_infeasible"


class PreservationError(ValueError):
    """固化副本修复规划输入或业务边界不合法；整次调用失败，不返回部分结果。"""


def _exact_fields(value, expected, label):
    if not isinstance(value, dict):
        raise PreservationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise PreservationError(
            f"{label} has invalid fields; missing={missing}, extra={extra}"
        )
    return value


def _digest(value, label):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise PreservationError(f"{label} must be 64 lowercase hex characters")
    return value


def _parse_assets(assets, document_ids):
    """校验 assets 恰好覆盖全部文献，返回按 id 排序的规范 asset 列表与映射。"""
    array(assets, 1, MAX_ASSETS)
    by_id = {}
    for index, asset in enumerate(assets):
        label = f"assets[{index}]"
        _exact_fields(asset, _ASSET_FIELDS, label)
        document_id = ident(asset["id"])
        if document_id not in document_ids:
            raise PreservationError(
                f"{label} references unknown document {document_id!r}"
            )
        if document_id in by_id:
            raise PreservationError(
                f"duplicate asset for document {document_id!r}"
            )
        digest = _digest(asset["sha256"], f"{label}.sha256")
        size_bytes = integer(asset["size_bytes"], 1, MAX_SIZE_BYTES)
        by_id[document_id] = {
            "id": document_id,
            "sha256": digest,
            "size_bytes": size_bytes,
        }
    missing = sorted(document_ids - set(by_id))
    if missing or len(assets) != len(document_ids):
        raise PreservationError(
            f"assets must cover every document exactly once; missing={missing}"
        )
    ordered = [by_id[document_id] for document_id in sorted(by_id)]
    return ordered, by_id


def _parse_sites(sites):
    """校验站点列表，返回按 site_id 排序的规范站点与查找映射。"""
    array(sites, 1, MAX_SITES)
    by_id = {}
    for index, site in enumerate(sites):
        label = f"sites[{index}]"
        _exact_fields(site, _SITE_FIELDS, label)
        site_id = ident(site["site_id"])
        if site_id in by_id:
            raise PreservationError(f"duplicate site_id {site_id!r}")
        failure_domain = text(site["failure_domain"])
        status = site["status"]
        if status not in (_STATUS_ONLINE, _STATUS_OFFLINE):
            raise PreservationError(
                f"{label}.status must be 'online' or 'offline'"
            )
        write_slots = integer(site["write_slots"], 0, MAX_WRITE_SLOTS)
        write_cost = integer(site["write_cost"], 0, MAX_WRITE_COST)
        by_id[site_id] = {
            "site_id": site_id,
            "failure_domain": failure_domain,
            "status": status,
            "write_slots": write_slots,
            "write_cost": write_cost,
        }
    ordered = [by_id[site_id] for site_id in sorted(by_id)]
    return ordered, by_id


def _parse_replicas(replicas, document_ids, site_by_id, asset_by_id):
    """校验副本记录并判定每条记录的当前状态，返回按 replica_id 排序的列表。

    同一文献在同一站点至多一条记录；未知文献/站点、重复 replica_id、
    (document_id, site_id) 重复、非法摘要或 checked_at 都整次拒绝。
    """
    array(replicas, 0, MAX_REPLICAS)
    by_id = {}
    seen_doc_site = set()
    ordered = []
    for index, replica in enumerate(replicas):
        label = f"replicas[{index}]"
        _exact_fields(replica, _REPLICA_FIELDS, label)
        replica_id = ident(replica["replica_id"])
        if replica_id in by_id:
            raise PreservationError(f"duplicate replica_id {replica_id!r}")
        document_id = ident(replica["document_id"])
        if document_id not in document_ids:
            raise PreservationError(
                f"{label} references unknown document {document_id!r}"
            )
        site_id = ident(replica["site_id"])
        if site_id not in site_by_id:
            raise PreservationError(
                f"{label} references unknown site {site_id!r}"
            )
        doc_site = (document_id, site_id)
        if doc_site in seen_doc_site:
            raise PreservationError(
                f"duplicate replica for document {document_id!r} on site "
                f"{site_id!r}; at most one record per document/site pair"
            )
        seen_doc_site.add(doc_site)

        raw_digest = replica["observed_sha256"]
        if raw_digest is not None:
            observed = _digest(raw_digest, f"{label}.observed_sha256")
        else:
            observed = None
        checked_at = text(replica["checked_at"])

        site = site_by_id[site_id]
        if site["status"] != _STATUS_ONLINE or observed is None:
            state = _REPLICA_UNAVAILABLE
        elif observed == asset_by_id[document_id]["sha256"]:
            state = _REPLICA_HEALTHY
        else:
            state = _REPLICA_CORRUPT

        record = {
            "replica_id": replica_id,
            "document_id": document_id,
            "site_id": site_id,
            "failure_domain": site["failure_domain"],
            "site_status": site["status"],
            "observed_sha256": observed,
            "registered_sha256": asset_by_id[document_id]["sha256"],
            "checked_at": checked_at,
            "status": state,
        }
        by_id[replica_id] = record
        ordered.append(record)
    ordered.sort(key=lambda item: item["replica_id"])
    return ordered


def _parse_request(request, document_ids):
    _exact_fields(request, _REQUEST_FIELDS, "request")
    preservation_id = text(request["preservation_id"])
    raw_seeds = request["seed_ids"]
    if not isinstance(raw_seeds, list) or not 1 <= len(raw_seeds) <= MAX_SEEDS:
        raise PreservationError(
            f"seed_ids must contain 1..{MAX_SEEDS} unique ids"
        )
    seeds = []
    seen = set()
    for raw_seed in raw_seeds:
        seed_id = ident(raw_seed)
        if seed_id in seen:
            raise PreservationError("duplicate seed_id")
        if seed_id not in document_ids:
            raise PreservationError(f"unknown seed document {seed_id!r}")
        seen.add(seed_id)
        seeds.append(seed_id)
    required_domains = integer(
        request["required_failure_domains"], 1, MAX_REQUIRED_DOMAINS
    )
    reason = text(request["reason"])
    return {
        "preservation_id": preservation_id,
        "seed_ids": sorted(seeds),
        "required_failure_domains": required_domains,
        "reason": reason,
    }


def _index_replicas(replica_records):
    """按文献聚合副本记录，并建立 (文献, 站点) 查找。"""
    by_document = {}
    by_doc_site = {}
    for record in replica_records:
        by_document.setdefault(record["document_id"], []).append(record)
        by_doc_site[(record["document_id"], record["site_id"])] = record
    for records in by_document.values():
        records.sort(key=lambda item: item["site_id"])
    return by_document, by_doc_site


def _document_replica_view(document_id, records, asset):
    """构造单篇文献的完整副本审计视图（全部记录，按 site_id 排序）。"""
    entries = []
    healthy_sites = []
    healthy_domains = set()
    corrupt_sites = []
    unavailable_sites = []
    for record in records:
        entry = {
            "replica_id": record["replica_id"],
            "site_id": record["site_id"],
            "failure_domain": record["failure_domain"],
            "site_status": record["site_status"],
            "observed_sha256": record["observed_sha256"],
            "registered_sha256": record["registered_sha256"],
            "checked_at": record["checked_at"],
            "status": record["status"],
        }
        entries.append(entry)
        if record["status"] == _REPLICA_HEALTHY:
            healthy_sites.append(record["site_id"])
            healthy_domains.add(record["failure_domain"])
        elif record["status"] == _REPLICA_CORRUPT:
            corrupt_sites.append(record["site_id"])
        else:
            unavailable_sites.append(record["site_id"])
    entries.sort(key=lambda item: item["site_id"])
    return {
        "document_id": document_id,
        "registered_sha256": asset["sha256"],
        "size_bytes": asset["size_bytes"],
        "replicas": entries,
        "healthy_site_ids": sorted(healthy_sites),
        "healthy_failure_domains": sorted(healthy_domains),
        "corrupt_site_ids": sorted(corrupt_sites),
        "unavailable_site_ids": sorted(unavailable_sites),
        "healthy": bool(healthy_sites),
        "status": "recoverable" if healthy_sites else "unrecoverable",
    }

class _FrozenLex:
    """稀疏字典序向量的不可变、可比较包装。

    内部保存排序后的 ``(分量, 值)`` 元组；比较时按最小差异分量对齐（缺失分量
    视为 0），不能直接比较两个等长元组，因为它们的首项可能属于不同分量。
    缩减费用的字典序分量可能为负（残量反向边），因此对齐时缺失侧按 0 处理、
    不做非负假设。
    """

    __slots__ = ("items",)

    def __init__(self, lex_map):
        self.items = tuple(sorted(lex_map.items()))

    @staticmethod
    def _less(left_items, right_items):
        li = 0
        ri = 0
        left_len = len(left_items)
        right_len = len(right_items)
        while li < left_len and ri < right_len:
            left_component, left_value = left_items[li]
            right_component, right_value = right_items[ri]
            if left_component == right_component:
                if left_value != right_value:
                    return left_value < right_value
                li += 1
                ri += 1
            elif left_component < right_component:
                if left_value != 0:
                    return left_value < 0
                li += 1
            else:
                if right_value != 0:
                    return 0 < right_value
                ri += 1
        while li < left_len:
            if left_items[li][1] != 0:
                return left_items[li][1] < 0
            li += 1
        while ri < right_len:
            if right_items[ri][1] != 0:
                return 0 < right_items[ri][1]
            ri += 1
        return False

    def __lt__(self, other):
        return self._less(self.items, other.items)

    def __eq__(self, other):
        return self.items == other.items

    def __hash__(self):
        return hash(self.items)


class _VectorMinCostFlow:
    """稀疏向量费用的最小费用流：SSP + Dijkstra + Johnson 势能。

    每条边的费用由一个写入成本分量和至多一个字典序分量组成：
    ``(write_cost, lex_component, lex_value)``，非动作边的字典序分量编号取 0
    表示该分量不存在。到某节点的距离表示为 ``(write_cost, {分量: 值})`` 稀疏
    字典——一条增广路径只经过少量动作边，字典规模等于路径长度而非保护文献
    数，因此每次松弛/比较的开销与文献总数无关，避免宽元组的 O(N) 分量运算。

    字典序比较规则：先比 write_cost；相等时取两个稀疏字典中编号最小的差异
    分量（缺失视为 0），值更小者更优。这与“先最小化总 write_cost，再按文献
    ID 顺序逐篇最小化目标站点序列”的两级目标完全一致，且全程不产生随文献数
    膨胀的超大整数。

    前向边费用各分量非负，初始零势能可行；增广后用势能把残量反向边的负费用
    抵消为非负缩减费用，故每轮都用 Dijkstra，避免 SPFA/Bellman-Ford 的最坏
    复杂度。边对象为共享可变列表
    ``[to, 容量, write_cost, 字典序分量, 字典序值, 元数据, 反向边]``。
    """

    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]
        # 势能同样稀疏：一个 write_cost 数组 + 每节点一个字典序分量字典。
        self._pot_wc = [0] * size
        self._pot_lex = [dict() for _ in range(size)]

    def add_edge(self, source, target, capacity, write_cost, lex_component=0,
                 lex_value=0, meta=None):
        forward = [target, capacity, write_cost, lex_component, lex_value,
                   meta, None]
        backward = [source, 0, -write_cost, lex_component, -lex_value,
                    None, None]
        forward[6] = backward
        backward[6] = forward
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return forward

    def augment(self, source, sink, required_flow):
        """逐单位增广到 required_flow，返回实际流量（不可达时提前停止）。"""
        n = self.size
        pot_wc = self._pot_wc
        pot_lex = self._pot_lex
        flow = 0
        frozen_empty = _FrozenLex({})
        while flow < required_flow:
            dist_wc = [None] * n
            dist_lex = [None] * n
            dist_key = [None] * n
            previous_node = [-1] * n
            previous_edge = [None] * n
            dist_wc[source] = 0
            dist_lex[source] = {}
            dist_key[source] = frozen_empty
            queue = [(0, frozen_empty, source)]
            while queue:
                current_wc, current_key, node = heapq.heappop(queue)
                if current_key != dist_key[node] or \
                        current_wc != dist_wc[node]:
                    continue
                current_lex = dist_lex[node]
                node_pot_lex = pot_lex[node]
                for edge in self.graph[node]:
                    target, capacity, edge_wc, lex_comp, lex_val, _meta, \
                        _reverse = edge
                    if capacity <= 0:
                        continue
                    reduced_wc = current_wc + edge_wc + pot_wc[node] \
                        - pot_wc[target]
                    target_pot_lex = pot_lex[target]
                    existing_wc = dist_wc[target]
                    # 快速路径：该边与两端势能都不涉及任何字典序分量，缩减
                    # 字典序费用与当前节点完全相同，可直接复用，省去集合合并
                    # 与字典重建（DAG 前向上这是绝大多数松弛的情形）。
                    if not lex_comp and not node_pot_lex \
                            and not target_pot_lex:
                        if existing_wc is not None and \
                                reduced_wc >= existing_wc:
                            continue
                        candidate_key = current_key
                        reduced_lex = current_lex
                    else:
                        # 完整稀疏缩减费用 = 当前距离 + 边费用 + 势能[u]
                        # - 势能[v]。三个来源在该分量上的贡献都必须计入；即使
                        # 边本身不含该分量（lex_comp 为 0），两端势能差也可能
                        # 非零。
                        reduced_lex = dict(current_lex)
                        involved = set(node_pot_lex)
                        involved.update(target_pot_lex)
                        if lex_comp:
                            involved.add(lex_comp)
                        for component in involved:
                            value = reduced_lex.get(component, 0)
                            if lex_comp == component:
                                value += lex_val
                            value += node_pot_lex.get(component, 0)
                            value -= target_pot_lex.get(component, 0)
                            if value:
                                reduced_lex[component] = value
                            else:
                                reduced_lex.pop(component, None)
                        if existing_wc is not None and \
                                reduced_wc > existing_wc:
                            continue
                        candidate_key = _FrozenLex(reduced_lex)
                        if existing_wc is not None and \
                                reduced_wc == existing_wc and \
                                not candidate_key < dist_key[target]:
                            continue
                    dist_key[target] = candidate_key
                    dist_wc[target] = reduced_wc
                    dist_lex[target] = reduced_lex
                    previous_node[target] = node
                    previous_edge[target] = edge
                    heapq.heappush(
                        queue, (reduced_wc, candidate_key, target)
                    )
            if previous_edge[sink] is None:
                break
            for node in range(n):
                if dist_wc[node] is not None:
                    pot_wc[node] += dist_wc[node]
                    for component, value in dist_lex[node].items():
                        pot_lex[node][component] = \
                            pot_lex[node].get(component, 0) + value
            # 沿前驱路径求瓶颈容量：源→文献边容量为该文献缺口数、站点→汇边
            # 为站点 slot，均可能大于 1；只有动作边容量恒为 1。一次推送瓶颈
            # 容量可显著减少 SSP 迭代次数（同一最短路上的多单位流量合并）。
            path_edges = []
            node = sink
            bottleneck = required_flow - flow
            while node != source:
                prev = previous_node[node]
                edge = previous_edge[node]
                path_edges.append(edge)
                if edge[1] < bottleneck:
                    bottleneck = edge[1]
                node = prev
            for edge in path_edges:
                edge[1] -= bottleneck
                edge[6][1] += bottleneck
            flow += bottleneck
        return flow

    def saturated_action_edges(self):
        """返回全部容量耗尽且携带动作元数据的前向边（即被选中的动作）。"""
        return [
            edge
            for edges in self.graph
            for edge in edges
            if edge[5] is not None and edge[1] == 0
        ]


def _candidate_targets(document_id, audit, site_ordered, required_domains,
                       doc_site_index):
    """返回一篇文献的候选目标站点（按 site_id 排序）与按故障域分组结果。

    候选必须 online、write_slots>0、当前没有该文献的 healthy 副本，且其故障域
    尚未被该文献现有 healthy 副本覆盖。
    """
    covered_domains = set(audit["healthy_failure_domains"])
    candidates = []
    domains = {}
    for site in site_ordered:
        site_id = site["site_id"]
        if site["status"] != _STATUS_ONLINE or site["write_slots"] <= 0:
            continue
        if site["failure_domain"] in covered_domains:
            continue
        existing = doc_site_index.get((document_id, site_id))
        if existing is not None and \
                existing["status"] == _REPLICA_HEALTHY:
            continue
        candidates.append(site)
        domains.setdefault(site["failure_domain"], []).append(site)
    missing = max(0, required_domains - len(covered_domains))
    return candidates, domains, missing


def _action_meta(document_id, site, doc_site_index):
    """构造一条候选动作的确定性元数据（源在最终统一填充最小 healthy 源）。"""
    existing = doc_site_index.get((document_id, site["site_id"]))
    if existing is not None:
        return {
            "document_id": document_id,
            "target_site_id": site["site_id"],
            "action": _ACTION_REPLACE,
            "replaced_replica_id": existing["replica_id"],
            "failure_domain": site["failure_domain"],
            "write_cost": site["write_cost"],
        }
    return {
        "document_id": document_id,
        "target_site_id": site["site_id"],
        "action": _ACTION_CREATE,
        "replaced_replica_id": "",
        "failure_domain": site["failure_domain"],
        "write_cost": site["write_cost"],
    }


def _site_slot_summary(site_ordered, usage):
    """构造各站点 slot 使用/剩余摘要，按 site_id 排序。"""
    summary = []
    for site in site_ordered:
        site_id = site["site_id"]
        used = usage.get(site_id, 0)
        available = site["write_slots"] if site["status"] == _STATUS_ONLINE else 0
        summary.append({
            "site_id": site_id,
            "failure_domain": site["failure_domain"],
            "status": site["status"],
            "write_slots": site["write_slots"],
            "write_cost": site["write_cost"],
            "slots_used": used,
            "slots_remaining": site["write_slots"] - used,
            "slots_available": available,
        })
    return summary


def _candidate_site_summary(document_id, candidates, missing):
    """blocked 证据中可复核的候选目标摘要。"""
    return {
        "document_id": document_id,
        "missing_failure_domains": missing,
        "candidate_targets": [
            {
                "site_id": site["site_id"],
                "failure_domain": site["failure_domain"],
                "write_slots": site["write_slots"],
                "write_cost": site["write_cost"],
            }
            for site in candidates
        ],
    }


def plan_preservation(data, assets, sites, replicas, request):
    """为保护闭包共同求解固化副本修复计划。

    :param data: 与 :func:`citation_lineage.process` 相同的输入契约，不会被修改。
    :param assets: 恰好覆盖 ``data`` 全部文献的登记载体（``id`` / ``sha256`` /
        ``size_bytes``）。
    :param sites: 1..100 个唯一存储站点。
    :param replicas: 0..10000 条副本观测记录。
    :param request: 保护请求（``preservation_id`` / ``seed_ids`` /
        ``required_failure_domains`` / ``reason``）。
    :return: ``status`` 为 ``"planned"`` 或 ``"blocked"`` 的完整可 JSON 序列化
        结果；任一输入非法时抛 :class:`PreservationError`（``ValueError`` 子类），
        不返回部分结果，也不修改任何调用方输入。
    """
    # 1. 契约校验：先 process（同时完成 data 校验），再校验其余四类输入。
    lineage = process(data)
    document_ids = set(lineage["documents"])
    document_order = list(lineage["documents"])

    asset_ordered, asset_by_id = _parse_assets(assets, document_ids)
    site_ordered, site_by_id = _parse_sites(sites)
    replica_records = _parse_replicas(
        replicas, document_ids, site_by_id, asset_by_id
    )
    normalized_request = _parse_request(request, document_ids)

    required_domains = normalized_request["required_failure_domains"]
    seeds = normalized_request["seed_ids"]

    # 2. 保护闭包：直接复用 process 的 cited_ancestors，不为每个 seed 重扫图。
    ancestors = lineage["cited_ancestors"]
    closure_set = set(seeds)
    seed_contributions = {}
    for seed in seeds:
        contribution = sorted(set(ancestors[seed]))
        seed_contributions[seed] = contribution
        closure_set.update(contribution)
    closure_ids = sorted(closure_set)

    # 3. 全部文献的副本状态审计（闭包外文献同样保留审计摘要）。
    by_document, doc_site_index = _index_replicas(replica_records)
    audits = {}
    for document_id in document_order:
        audits[document_id] = _document_replica_view(
            document_id,
            by_document.get(document_id, []),
            asset_by_id[document_id],
        )

    # 4. 逐保护文献计算现状、缺口与候选目标。
    planning = {}
    for document_id in closure_ids:
        audit = audits[document_id]
        candidates, domains, missing = _candidate_targets(
            document_id, audit, site_ordered, required_domains,
            doc_site_index,
        )
        planning[document_id] = {
            "audit": audit,
            "candidates": candidates,
            "domains": domains,
            "missing": missing,
            "healthy_site_ids": list(audit["healthy_site_ids"]),
            "healthy_failure_domains": list(audit["healthy_failure_domains"]),
            "source_site_id": (
                min(audit["healthy_site_ids"])
                if audit["healthy_site_ids"] else None
            ),
        }

    slot_summary = _site_slot_summary(site_ordered, {})

    # 5. blocked 判定（一）：无 healthy 源；（二）单篇可用故障域不足。
    violations = []
    for document_id in closure_ids:
        plan = planning[document_id]
        audit = plan["audit"]
        if not audit["healthy"]:
            violations.append({
                "code": _VIOLATION_NO_SOURCE,
                "document_id": document_id,
                "healthy_site_ids": [],
                "healthy_failure_domains": [],
                "missing_failure_domains": plan["missing"],
                "candidate_targets": _candidate_site_summary(
                    document_id, plan["candidates"], plan["missing"]
                )["candidate_targets"],
                "site_slots": slot_summary,
            })
        elif len(plan["domains"]) < plan["missing"]:
            violations.append({
                "code": _VIOLATION_INSUFFICIENT_DOMAINS,
                "document_id": document_id,
                "healthy_site_ids": plan["healthy_site_ids"],
                "healthy_failure_domains": plan["healthy_failure_domains"],
                "missing_failure_domains": plan["missing"],
                "available_target_failure_domains": sorted(plan["domains"]),
                "available_target_domain_count": len(plan["domains"]),
                "candidate_targets": _candidate_site_summary(
                    document_id, plan["candidates"], plan["missing"]
                )["candidate_targets"],
                "site_slots": slot_summary,
            })

    total_actions = sum(
        planning[document_id]["missing"] for document_id in closure_ids
    )

    chosen_metas = []
    usage = {}
    if not violations and total_actions > 0:
        # 6. 全局向量费用最小费用流：站点 slot 为共享容量，杜绝逐文献贪心
        #    占用；一次求解同时最小化总 write_cost 与完整动作序列字典序。
        #    不能用逐文献贪心替代：即使贪心不超 slot，其局部最低成本选择也
        #    可能把后续文献唯一可用的低成本站点提前占满，使总成本高于全局解。
        chosen_metas, _minimum_write_cost = _solve_vector_flow(
            closure_ids, planning, site_ordered, doc_site_index,
            total_actions,
        )
        if chosen_metas is None:
            # （三）多文献争用站点 slot 的全局容量不可行。
            violations.append(_contention_violation(
                closure_ids, planning, site_ordered, slot_summary,
                total_actions,
            ))
            chosen_metas = []
        else:
            for meta in chosen_metas:
                usage[meta["target_site_id"]] = \
                    usage.get(meta["target_site_id"], 0) + 1

    if violations:
        return _blocked_result(
            normalized_request, asset_ordered, site_ordered, replica_records,
            lineage, closure_ids, seeds, seed_contributions, audits, planning,
            violations, slot_summary, total_actions, document_order,
        )

    # 7. 组装确定性动作：源固定为现有 healthy 源中 site_id 最小者；新动作
    #    不能作为其他动作的读取源（本实现从不把规划动作加入源集合）。
    actions = []
    for meta in sorted(
        chosen_metas,
        key=lambda item: (
            item["document_id"], item["target_site_id"],
            item["source_site_id"], item["action"],
            item["replaced_replica_id"],
        ),
    ):
        actions.append({
            "document_id": meta["document_id"],
            "source_site_id": meta["source_site_id"],
            "target_site_id": meta["target_site_id"],
            "failure_domain": meta["failure_domain"],
            "action": meta["action"],
            "replaced_replica_id": (
                meta["replaced_replica_id"] if meta["action"] == _ACTION_REPLACE
                else None
            ),
            "write_cost": meta["write_cost"],
        })

    total_write_cost = sum(action["write_cost"] for action in actions)
    final_slot_summary = _site_slot_summary(site_ordered, usage)

    planning_view = _planning_view(
        closure_ids, planning, actions, required_domains
    )
    projected_replicas = _projected_replicas(
        replica_records, actions, doc_site_index
    )

    result = {
        "status": _PLANNED,
        "preservation_id": normalized_request["preservation_id"],
        "request": dict(normalized_request),
        "assets": [dict(asset) for asset in asset_ordered],
        "sites": [dict(site) for site in site_ordered],
        "replicas": [
            {
                "replica_id": record["replica_id"],
                "document_id": record["document_id"],
                "site_id": record["site_id"],
                "observed_sha256": record["observed_sha256"],
                "checked_at": record["checked_at"],
            }
            for record in replica_records
        ],
        "process": lineage,
        "closure": {
            "document_ids": closure_ids,
            "document_count": len(closure_ids),
            "seed_ids": seeds,
            "seed_contributions": [
                {"seed_id": seed, "contributes": seed_contributions[seed]}
                for seed in seeds
            ],
        },
        "replica_audit": [
            _audit_public(audits[document_id])
            for document_id in document_order
        ],
        "planning": planning_view,
        "actions": actions,
        "action_count": len(actions),
        "total_write_cost": total_write_cost,
        "site_slot_usage": final_slot_summary,
        "planned_replica_view": projected_replicas,
        "violations": [],
        "summary": _summary(
            document_order, replica_records, closure_ids, audits, planning,
            actions, final_slot_summary,
        ),
    }
    return result


def _solve_vector_flow(closure_ids, planning, site_ordered, doc_site_index,
                       total_actions):
    """构造并求解向量费用最小费用流，一次完成成本最优与字典序决胜。

    网络：``source → 文献节点 → (文献×故障域节点) → 站点节点 → sink``。

    * 文献侧出边容量为该文献仍缺的故障域数；
    * 一个故障域含多个候选站点时，为每个 (文献, 故障域) 建立独立域节点：
      ``文献 → 域`` 容量 1 保证该文献在此域至多取一个站点；每个
      ``域 → 站点`` 只建一条携带费用与动作元数据的边，饱和边即唯一记录被
      选中的目标站点，残量反向边可被 SSP 用于改道；
    * 单站点故障域直接用 ``文献 → 站点`` 动作边；
    * 站点到 sink 的容量即该 online 站点本窗口 ``write_slots``，是所有文献
      动作共享的全局容量约束，杜绝逐文献贪心占用。

    每条动作边携带一个写入成本分量和一个字典序分量：写入成本即站点
    ``write_cost``；字典序分量编号即该文献在闭包中的 ID 序（从 1 起，每篇
    文献一个分量），取值为 ``(候选数+1)^局部位置``（候选按
    ``target_site_id`` 升序，局部位置越小位值越小）。稀疏向量按“最小差异
    分量编号”字典序比较，因此先比 write_cost，再按文献 ID 顺序逐篇决胜，每篇
    内部再按目标站点字典序决胜；同一 (文献, 目标) 的源站点、action 与
    replaced_replica_id 均唯一确定，故目标序列决胜即完整动作键决胜。分量值
    不超过 ``101^99``（约 660 位），每条边只携带一个分量，稀疏距离只含路径
    经过的分量，与文献总数解耦，不会产生超大整数或宽向量运算。

    返回 ``(chosen_metas, total_write_cost)``；流量不足时两项均为 ``None``，
    表示全局 slot 容量不可行。
    """
    n_docs = len(closure_ids)
    n_sites = len(site_ordered)
    site_index = {site["site_id"]: i for i, site in enumerate(site_ordered)}

    # 仅为含多个候选站点的 (文献, 故障域) 组合分配域节点。
    domain_pairs = sorted({
        (document_id, domain)
        for document_id in closure_ids
        for domain, member_sites in planning[document_id]["domains"].items()
        if len(member_sites) > 1
    })
    domain_node_index = {pair: i for i, pair in enumerate(domain_pairs)}

    base_doc = 1
    base_site = base_doc + n_docs
    base_domain = base_site + n_sites
    source = 0
    sink = base_domain + len(domain_pairs)
    flow = _VectorMinCostFlow(sink + 1)
    flow.source = source
    flow.sink = sink

    for doc_pos, document_id in enumerate(closure_ids):
        plan = planning[document_id]
        doc_node = base_doc + doc_pos
        flow.add_edge(source, doc_node, plan["missing"], 0)
        source_site = plan["source_site_id"]
        connected_domains = set()
        candidates = plan["candidates"]
        digit_base = len(candidates) + 1
        lex_component = doc_pos + 1
        for local, site in enumerate(candidates):
            site_node = base_site + site_index[site["site_id"]]
            domain = site["failure_domain"]
            pair = (document_id, domain)
            meta = _action_meta(document_id, site, doc_site_index)
            meta["source_site_id"] = source_site
            lex_value = digit_base ** local
            if pair in domain_node_index:
                domain_node = base_domain + domain_node_index[pair]
                if pair not in connected_domains:
                    # 每 (文献, 域) 一条容量 1 的边，强制域内至多一个站点。
                    flow.add_edge(doc_node, domain_node, 1, 0)
                    connected_domains.add(pair)
                flow.add_edge(
                    domain_node, site_node, 1, site["write_cost"],
                    lex_component, lex_value, meta,
                )
            else:
                flow.add_edge(
                    doc_node, site_node, 1, site["write_cost"],
                    lex_component, lex_value, meta,
                )

    for site_pos, site in enumerate(site_ordered):
        site_node = base_site + site_pos
        if site["status"] == _STATUS_ONLINE and site["write_slots"] > 0:
            # 共享 slot 容量：同一站点上所有文献动作共同受此边约束。
            flow.add_edge(site_node, sink, site["write_slots"], 0)

    achieved = flow.augment(source, sink, total_actions)
    if achieved < total_actions:
        return None, None

    chosen_metas = [
        edge[5] for edge in flow.saturated_action_edges()
    ]
    total_write_cost = sum(meta["write_cost"] for meta in chosen_metas)
    return chosen_metas, total_write_cost




def _contention_violation(closure_ids, planning, site_ordered, slot_summary,
                          total_actions):
    online_capacity = sum(
        site["write_slots"] for site in site_ordered
        if site["status"] == _STATUS_ONLINE
    )
    return {
        "code": _VIOLATION_SLOT_CAPACITY,
        "document_ids": list(closure_ids),
        "required_actions": total_actions,
        "online_slot_capacity": online_capacity,
        "documents": [
            {
                "document_id": document_id,
                "healthy_site_ids": planning[document_id]["healthy_site_ids"],
                "healthy_failure_domains":
                    planning[document_id]["healthy_failure_domains"],
                "missing_failure_domains": planning[document_id]["missing"],
                "candidate_targets": _candidate_site_summary(
                    document_id,
                    planning[document_id]["candidates"],
                    planning[document_id]["missing"],
                )["candidate_targets"],
            }
            for document_id in closure_ids
        ],
        "site_slots": slot_summary,
    }


def _blocked_result(normalized_request, asset_ordered, site_ordered,
                    replica_records, lineage, closure_ids, seeds,
                    seed_contributions, audits, planning, violations,
                    slot_summary, total_actions, document_order):
    return {
        "status": _BLOCKED,
        "preservation_id": normalized_request["preservation_id"],
        "request": dict(normalized_request),
        "assets": [dict(asset) for asset in asset_ordered],
        "sites": [dict(site) for site in site_ordered],
        "replicas": [
            {
                "replica_id": record["replica_id"],
                "document_id": record["document_id"],
                "site_id": record["site_id"],
                "observed_sha256": record["observed_sha256"],
                "checked_at": record["checked_at"],
            }
            for record in replica_records
        ],
        "process": lineage,
        "closure": {
            "document_ids": closure_ids,
            "document_count": len(closure_ids),
            "seed_ids": seeds,
            "seed_contributions": [
                {"seed_id": seed, "contributes": seed_contributions[seed]}
                for seed in seeds
            ],
        },
        "replica_audit": [
            _audit_public(audits[document_id])
            for document_id in document_order
        ],
        "planning": _planning_view(
            closure_ids, planning, [],
            normalized_request["required_failure_domains"],
        ),
        "actions": [],
        "action_count": 0,
        "total_write_cost": 0,
        "site_slot_usage": slot_summary,
        "planned_replica_view": [],
        "violations": violations,
        "summary": _summary(
            document_order, replica_records, closure_ids, audits, planning,
            [], slot_summary,
        ),
    }


def _audit_public(audit):
    return {
        "document_id": audit["document_id"],
        "registered_sha256": audit["registered_sha256"],
        "size_bytes": audit["size_bytes"],
        "replicas": [dict(entry) for entry in audit["replicas"]],
        "healthy_site_ids": list(audit["healthy_site_ids"]),
        "healthy_failure_domains": list(audit["healthy_failure_domains"]),
        "corrupt_site_ids": list(audit["corrupt_site_ids"]),
        "unavailable_site_ids": list(audit["unavailable_site_ids"]),
        "healthy": audit["healthy"],
        "status": audit["status"],
    }


def _planning_view(closure_ids, planning, actions, required_domains):
    """逐保护文献规划前后的 healthy 站点/故障域、达标状态与缺口视图。"""
    actions_by_document = {}
    for action in actions:
        actions_by_document.setdefault(action["document_id"], []).append(action)

    view = []
    for document_id in closure_ids:
        plan = planning[document_id]
        doc_actions = sorted(
            actions_by_document.get(document_id, []),
            key=lambda item: item["target_site_id"],
        )
        before_sites = set(plan["healthy_site_ids"])
        before_domains = set(plan["healthy_failure_domains"])
        after_sites = set(before_sites)
        after_domains = set(before_domains)
        for action in doc_actions:
            after_sites.add(action["target_site_id"])
            after_domains.add(action["failure_domain"])
        view.append({
            "document_id": document_id,
            "required_failure_domains": required_domains,
            "before": {
                "healthy_site_ids": sorted(before_sites),
                "healthy_failure_domains": sorted(before_domains),
                "domain_count": len(before_domains),
                "compliant": len(before_domains) >= required_domains,
                "missing_failure_domains": plan["missing"],
            },
            "planned_actions": [
                {
                    "target_site_id": action["target_site_id"],
                    "source_site_id": action["source_site_id"],
                    "failure_domain": action["failure_domain"],
                    "action": action["action"],
                    "replaced_replica_id": action["replaced_replica_id"],
                    "write_cost": action["write_cost"],
                }
                for action in doc_actions
            ],
            "after": {
                "healthy_site_ids": sorted(after_sites),
                "healthy_failure_domains": sorted(after_domains),
                "domain_count": len(after_domains),
                "compliant": len(after_domains) >= required_domains,
                "missing_failure_domains": max(
                    0, required_domains - len(after_domains)
                ),
            },
        })
    return view


def _projected_replicas(replica_records, actions, doc_site_index):
    """规划后副本视图：仅描述本次方案，不伪造 checked_at，也不回写输入。

    现有记录原样保留并标注其当前状态；被 replace 的记录额外标注
    ``projected_status="replaced"``；每个动作生成一条 ``planned`` 投影记录，
    其 ``checked_at=None`` 明确表示尚未发生真实校验。
    """
    replaced_keys = {
        (action["document_id"], action["target_site_id"])
        for action in actions if action["action"] == _ACTION_REPLACE
    }
    projected = []
    for record in replica_records:
        entry = {
            "replica_id": record["replica_id"],
            "document_id": record["document_id"],
            "site_id": record["site_id"],
            "failure_domain": record["failure_domain"],
            "observed_sha256": record["observed_sha256"],
            "registered_sha256": record["registered_sha256"],
            "checked_at": record["checked_at"],
            "current_status": record["status"],
            "planned": False,
            "projected_status": (
                "replaced"
                if (record["document_id"], record["site_id"]) in replaced_keys
                else record["status"]
            ),
        }
        projected.append(entry)
    for action in actions:
        projected.append({
            "replica_id": None,
            "document_id": action["document_id"],
            "site_id": action["target_site_id"],
            "failure_domain": action["failure_domain"],
            "observed_sha256": None,
            "registered_sha256": doc_site_index[
                (action["document_id"], action["source_site_id"])
            ]["registered_sha256"],
            "checked_at": None,
            "current_status": None,
            "planned": True,
            "projected_status": _REPLICA_HEALTHY,
            "action": action["action"],
            "replaced_replica_id": action["replaced_replica_id"],
            "source_site_id": action["source_site_id"],
        })
    projected.sort(key=lambda item: (
        item["document_id"], item["site_id"],
        item["replica_id"] or "",
        0 if not item["planned"] else 1,
    ))
    return projected


def _summary(document_order, replica_records, closure_ids, audits, planning,
             actions, slot_summary):
    status_counts = {
        _REPLICA_HEALTHY: 0,
        _REPLICA_CORRUPT: 0,
        _REPLICA_UNAVAILABLE: 0,
    }
    for record in replica_records:
        status_counts[record["status"]] += 1
    unrecoverable = sorted(
        document_id for document_id in document_order
        if not audits[document_id]["healthy"]
    )
    # 规划前即达标：保护文献缺口为 0（已有 healthy 故障域数已满足要求）。
    compliant_before = sorted(
        document_id for document_id in closure_ids
        if planning[document_id]["missing"] == 0
    )
    actions_per_document = {}
    for action in actions:
        actions_per_document[action["document_id"]] = \
            actions_per_document.get(action["document_id"], 0) + 1
    # 规划后达标：每个缺口都恰好由一个动作补齐。
    compliant_after = sorted(
        document_id for document_id in closure_ids
        if actions_per_document.get(document_id, 0)
        == planning[document_id]["missing"]
    )
    return {
        "documents": len(document_order),
        "protected_documents": len(closure_ids),
        "replicas": len(replica_records),
        "healthy_replicas": status_counts[_REPLICA_HEALTHY],
        "corrupt_replicas": status_counts[_REPLICA_CORRUPT],
        "unavailable_replicas": status_counts[_REPLICA_UNAVAILABLE],
        "unrecoverable_documents": unrecoverable,
        "unrecoverable_document_count": len(unrecoverable),
        "compliant_documents_before": compliant_before,
        "compliant_documents_after": compliant_after,
        "planned_actions": len(actions),
        "create_actions": sum(
            1 for action in actions if action["action"] == _ACTION_CREATE
        ),
        "replace_actions": sum(
            1 for action in actions if action["action"] == _ACTION_REPLACE
        ),
        "total_write_cost": sum(
            action["write_cost"] for action in actions
        ),
        "slots_used_total": sum(item["slots_used"] for item in slot_summary),
        "slots_remaining_total": sum(
            item["slots_remaining"] for item in slot_summary
        ),
    }


__all__ = [
    "plan_preservation",
    "PreservationError",
    "MAX_ASSETS",
    "MAX_SITES",
    "MAX_REPLICAS",
    "MAX_SEEDS",
    "MAX_REQUIRED_DOMAINS",
]

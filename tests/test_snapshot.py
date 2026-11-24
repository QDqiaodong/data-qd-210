"""`materialize` 双时间发布快照的合成档案数据测试。

全部数据为原创合成，不访问任何外部档案服务或网络。运行方式：

    python -I -m unittest discover -s tests -v
"""

import copy
import json
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import MaterializationError, materialize  # noqa: E402

T0 = "2020-01-01T00:00:00Z"
T1 = "2020-06-01T00:00:00Z"
T2 = "2021-01-01T00:00:00Z"
T3 = "2021-06-01T00:00:00Z"
T4 = "2022-01-01T00:00:00Z"
T5 = "2022-06-01T00:00:00Z"


def document(doc_id):
    return {"id": doc_id, "date": "1900-01-01", "title": f"档案 {doc_id}"}


def citation(source, target, page="p"):
    return {"from": source, "to": target, "page": page}


def doc_locator(doc_id):
    return doc_id


def cit_locator(source, target, page="p"):
    return {"from": source, "to": target, "page": page}


def assertion(assertion_id, kind, locator, state, valid_from, recorded_at,
              valid_to=None, reason=None, supersedes=None):
    return {
        "assertion_id": assertion_id,
        "kind": kind,
        "locator": locator,
        "state": state,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "recorded_at": recorded_at,
        "reason": reason if reason is not None else f"原因 {assertion_id}",
        "supersedes": [] if supersedes is None else list(supersedes),
    }


def doc_assertion(assertion_id, doc_id, state, valid_from, recorded_at,
                  valid_to=None, supersedes=None):
    return assertion(assertion_id, "document", doc_locator(doc_id), state,
                     valid_from, recorded_at, valid_to=valid_to,
                     supersedes=supersedes)


def cit_assertion(assertion_id, source, target, state, valid_from,
                  recorded_at, page="p", valid_to=None, supersedes=None):
    return assertion(assertion_id, "citation",
                     cit_locator(source, target, page), state, valid_from,
                     recorded_at, valid_to=valid_to, supersedes=supersedes)


def query(query_id, valid_at, known_at):
    return {"query_id": query_id, "valid_at": valid_at, "known_at": known_at}


def small_data():
    return {
        "documents": [document("d1"), document("d2"), document("d3")],
        "citations": [citation("d1", "d2"), citation("d2", "d3")],
    }


def snapshot_by_id(result):
    return {snapshot["query_id"]: snapshot for snapshot in result["snapshots"]}


def state_of(snapshot, entity_kind, locator):
    if entity_kind == "document":
        return snapshot["document_states"][locator]
    key = "|".join(tuple(locator))
    return snapshot["citation_states"][key]


def citation_bucket(snapshot, bucket, source, target, page="p"):
    for entry in snapshot[bucket]:
        if (entry["from"], entry["to"], entry["page"]) == \
                (source, target, page):
            return entry
    raise KeyError((bucket, source, target, page))


class FutureBackdatedWithdrawalTest(unittest.TestCase):
    """后来补录、业务生效时间更早的撤回不得污染旧知识截止快照。"""

    def setUp(self):
        self.data = small_data()
        self.assertions = [
            doc_assertion("a-avail", "d1", "available", T0, T1),
            # T4 才记录，但业务区间从 T0 起就生效。
            doc_assertion("a-backdated", "d1", "withdrawn", T0, T4,
                          supersedes=["a-avail"]),
            doc_assertion("a-d2", "d2", "available", T0, T1),
            doc_assertion("a-d3", "d3", "available", T0, T1),
            cit_assertion("c-d1d2", "d1", "d2", "available", T0, T1),
            cit_assertion("c-d2d3", "d2", "d3", "available", T0, T1),
        ]
        self.queries = [
            query("q-early", T2, T3),   # 尚未记录补录撤回
            query("q-late", T2, T5),    # 已记录补录撤回
        ]

    def test_early_snapshot_unaffected_by_future_assertion(self):
        result = materialize(self.data, self.assertions, self.queries)
        early = snapshot_by_id(result)["q-early"]
        self.assertEqual(state_of(early, "document", "d1")["status"],
                         "available")
        # 未来声明及其纠正关系都不得出现在旧快照中。
        self.assertEqual(
            sorted(early["visible_assertion_ids"]),
            ["a-avail", "a-d2", "a-d3", "c-d1d2", "c-d2d3"],
        )
        d1_evidence = state_of(early, "document", "d1")["evidence"]
        self.assertCountEqual(d1_evidence["frontier_assertion_ids"], ["a-avail"])
        self.assertCountEqual(d1_evidence["covering_assertion_ids"], ["a-avail"])
        self.assertIn(
            {"id": "d1", "date": "1900-01-01", "title": "档案 d1"},
            early["documents"],
        )

    def test_late_snapshot_reflects_backdated_withdrawal(self):
        result = materialize(self.data, self.assertions, self.queries)
        late = snapshot_by_id(result)["q-late"]
        self.assertEqual(state_of(late, "document", "d1")["status"],
                         "withdrawn")
        d1_evidence = state_of(late, "document", "d1")["evidence"]
        self.assertCountEqual(d1_evidence["frontier_assertion_ids"],
                         ["a-backdated"])
        self.assertCountEqual(d1_evidence["superseded_assertion_ids"], ["a-avail"])
        self.assertCountEqual(d1_evidence["covering_assertion_ids"],
                         ["a-backdated"])
        # d1 退出规范目录，但其状态证据仍完整保留。
        self.assertNotIn("d1", [d["id"] for d in late["documents"]])
        self.assertEqual(state_of(late, "document", "d1")["status"],
                         "withdrawn")

    def test_early_snapshot_replayable_byte_for_byte(self):
        result = materialize(self.data, self.assertions, self.queries)
        early = snapshot_by_id(result)["q-early"]
        again = materialize(
            self.data,
            # 即便调用方删掉未来声明后重放同一旧知识截止查询，结果一致。
            [a for a in self.assertions if a["assertion_id"] != "a-backdated"],
            [query("q-early", T2, T3)],
        )["snapshots"][0]
        again.pop("query_id")
        early_copy = copy.deepcopy(early)
        early_copy.pop("query_id")
        self.assertEqual(again, early_copy)


class OverlappingConflictTest(unittest.TestCase):
    """两个未互相纠正且区间重叠的相反声明形成冲突。"""

    def setUp(self):
        self.data = small_data()
        self.assertions = [
            doc_assertion("avail", "d2", "available", T0, T1),
            doc_assertion("withdraw", "d2", "withdrawn", T0, T2),
        ]
        self.queries = [query("q", T3, T3)]

    def test_conflict_when_both_states_cover(self):
        result = materialize(self.data, self.assertions, self.queries)
        snapshot = result["snapshots"][0]
        entry = state_of(snapshot, "document", "d2")
        self.assertEqual(entry["status"], "conflict")
        evidence = entry["evidence"]
        self.assertCountEqual(evidence["covering_assertion_ids"],
                         ["avail", "withdraw"])
        self.assertCountEqual(evidence["available_assertion_ids"], ["avail"])
        self.assertCountEqual(evidence["withdrawn_assertion_ids"], ["withdraw"])
        self.assertCountEqual(evidence["frontier_assertion_ids"],
                         ["avail", "withdraw"])
        self.assertNotIn("d2", [d["id"] for d in snapshot["documents"]])

    def test_no_tie_break_by_recorded_at_or_id(self):
        # 交换记录时间与 ID 形态后，只要两分支仍都在前沿且都覆盖，
        # 结论必须仍是 conflict，而不是偏向某条声明。
        shuffled_assertions = [
            doc_assertion("zzz", "d2", "withdrawn", T0, T1),
            doc_assertion("aaa", "d2", "available", T0, T2),
        ]
        result = materialize(
            self.data, shuffled_assertions, [query("q", T3, T3)])
        self.assertEqual(
            state_of(result["snapshots"][0], "document", "d2")["status"],
            "conflict",
        )


class SupersedeConvergenceTest(unittest.TestCase):
    """一条后续声明同时 supersede 两个冲突分支后，仅在相应 known_at 起收敛。"""

    def setUp(self):
        self.data = small_data()
        self.assertions = [
            doc_assertion("avail", "d1", "available", T0, T1),
            doc_assertion("withdraw", "d1", "withdrawn", T0, T2),
            # 同时纠正两个冲突分支；T4 才记录。
            doc_assertion("resolved", "d1", "available", T0, T4,
                          supersedes=["avail", "withdraw"]),
        ]
        self.queries = [
            query("q-before", T3, T3),
            query("q-after", T3, T5),
        ]

    def test_early_snapshot_keeps_conflict(self):
        snapshot = snapshot_by_id(
            materialize(self.data, self.assertions, self.queries)
        )["q-before"]
        self.assertEqual(state_of(snapshot, "document", "d1")["status"],
                         "conflict")
        visible = sorted(snapshot["visible_assertion_ids"])
        self.assertEqual(visible, ["avail", "withdraw"])

    def test_later_snapshot_converges_to_single_state(self):
        snapshot = snapshot_by_id(
            materialize(self.data, self.assertions, self.queries)
        )["q-after"]
        entry = state_of(snapshot, "document", "d1")
        self.assertEqual(entry["status"], "available")
        evidence = entry["evidence"]
        self.assertCountEqual(evidence["frontier_assertion_ids"], ["resolved"])
        self.assertEqual(sorted(evidence["superseded_assertion_ids"]),
                         ["avail", "withdraw"])
        self.assertCountEqual(evidence["covering_assertion_ids"], ["resolved"])

    def test_chain_supersede_is_transitive_in_frontier(self):
        assertions = self.assertions + [
            doc_assertion("final", "d1", "withdrawn", T0, T5,
                          supersedes=["resolved"]),
        ]
        snapshot = materialize(
            self.data, assertions, [query("q", T3, T5)]
        )["snapshots"][0]
        entry = state_of(snapshot, "document", "d1")
        self.assertEqual(entry["status"], "withdrawn")
        self.assertCountEqual(entry["evidence"]["frontier_assertion_ids"],
                         ["final"])
        # 间接被纠正的冲突分支同样退出前沿。
        self.assertEqual(
            sorted(entry["evidence"]["superseded_assertion_ids"]),
            ["avail", "resolved", "withdraw"],
        )


class IntervalBoundaryTest(unittest.TestCase):
    """同一声明在左闭右开区间边界前后状态不同。"""

    def setUp(self):
        self.data = small_data()
        self.assertions = [
            # avail 覆盖整个时间轴；window 在 T1 才记录、纠正 avail 且仅在
            # [T1, T3) 生效。窗口内为 withdrawn，窗口结束后 window 不再覆盖、
            # avail 已退出前沿，由 T3 后补录的 restored 重新给出 available。
            doc_assertion("avail", "d1", "available", T0, T0),
            doc_assertion("window", "d1", "withdrawn", T1, T1,
                          valid_to=T3, supersedes=["avail"]),
            doc_assertion("restored", "d1", "available", T3, T4,
                          supersedes=["window"]),
            doc_assertion("d2", "d2", "available", T0, T0),
            doc_assertion("d3", "d3", "available", T0, T0),
        ]
        self.queries = [
            query("before", T0, T0),
            query("at-start", T1, T2),
            query("inside", T2, T2),
            query("at-end", T3, T4),
            query("after", T4, T4),
        ]

    def test_half_open_semantics(self):
        result = materialize(self.data, self.assertions, self.queries)
        by_id = snapshot_by_id(result)
        before = state_of(by_id["before"], "document", "d1")
        self.assertEqual(before["status"], "available")
        self.assertCountEqual(before["evidence"]["covering_assertion_ids"],
                         ["avail"])

        at_start = state_of(by_id["at-start"], "document", "d1")
        # T1 属于 [T1, T3)：window 覆盖，avail 已被纠正退出前沿。
        self.assertEqual(at_start["status"], "withdrawn")
        self.assertCountEqual(at_start["evidence"]["covering_assertion_ids"],
                         ["window"])

        inside = state_of(by_id["inside"], "document", "d1")
        self.assertEqual(inside["status"], "withdrawn")

        at_end = state_of(by_id["at-end"], "document", "d1")
        # T3 不属于 [T1, T3)：window 不再覆盖；restored 自 T3 起覆盖。
        self.assertEqual(at_end["status"], "available")
        self.assertCountEqual(at_end["evidence"]["covering_assertion_ids"],
                         ["restored"])
        # window 已被 restored 纠正，属于可见但退出前沿的历史。
        self.assertCountEqual(
            at_end["evidence"]["superseded_assertion_ids"],
            ["avail", "window"],
        )

        after = state_of(by_id["after"], "document", "d1")
        self.assertEqual(after["status"], "available")

    def test_non_covering_frontier_kept_as_visible_history(self):
        # 独立场景：window 仍是前沿成员，但其 [T0, T1) 区间不再覆盖查询时点。
        assertions = [
            doc_assertion("window", "d2", "withdrawn", T0, T0,
                          valid_to=T1),
        ]
        result = materialize(
            self.data, assertions,
            [query("late", T3, T3), query("early", T0, T0)])
        late = snapshot_by_id(result)["late"]
        evidence = state_of(late, "document", "d2")["evidence"]
        # 不覆盖查询时点的前沿声明仍须作为可见历史保留。
        self.assertCountEqual(evidence["frontier_assertion_ids"], ["window"])
        self.assertCountEqual(evidence["visible_assertion_ids"], ["window"])
        self.assertCountEqual(evidence["covering_assertion_ids"], [])
        self.assertCountEqual(
            evidence["non_covering_frontier_assertion_ids"], ["window"])
        self.assertEqual(state_of(late, "document", "d2")["status"],
                         "unknown")

        early = snapshot_by_id(result)["early"]
        early_evidence = state_of(early, "document", "d2")["evidence"]
        self.assertCountEqual(early_evidence["covering_assertion_ids"], ["window"])


class DocumentWithdrawalBlocksCitationsTest(unittest.TestCase):
    """文献撤回阻断自身仍可发布的入射引用，并联动改变 process 图结果。"""

    def setUp(self):
        self.data = {
            "documents": [document("d1"), document("d2"),
                          document("d3"), document("d4")],
            "citations": [
                citation("d1", "d2"),
                citation("d2", "d3"),
                citation("d4", "d2"),
            ],
        }
        self.assertions = [
            doc_assertion("doc-d1", "d1", "available", T0, T0),
            doc_assertion("doc-d2", "d2", "available", T0, T0),
            doc_assertion("doc-d3", "d3", "available", T0, T0),
            doc_assertion("doc-d4", "d4", "available", T0, T0),
            # T2 记录、T0 即生效的撤回：纠正原 available 声明后 d2 被撤回。
            doc_assertion("doc-d2-off", "d2", "withdrawn", T0, T2,
                          supersedes=["doc-d2"]),
            cit_assertion("c-12", "d1", "d2", "available", T0, T0),
            cit_assertion("c-23", "d2", "d3", "available", T0, T0),
            cit_assertion("c-42", "d4", "d2", "available", T0, T0),
        ]
        self.queries = [query("before", T1, T1), query("after", T1, T3)]

    def test_graph_changes_when_document_withdrawn(self):
        result = materialize(self.data, self.assertions, self.queries)
        by_id = snapshot_by_id(result)
        before = by_id["before"]
        after = by_id["after"]

        self.assertEqual(before["process"]["roots"], ["d1", "d4"])
        self.assertEqual(before["process"]["summary"]["max_depth"], 2)
        self.assertEqual(
            before["process"]["cited_ancestors"]["d4"], ["d2", "d3"]
        )
        self.assertEqual(len(before["citations"]), 3)

        self.assertEqual(state_of(after, "document", "d2")["status"],
                         "withdrawn")
        # 引用自身前沿声明没有变化，仍然 available。
        for source, target in (("d1", "d2"), ("d2", "d3"), ("d4", "d2")):
            self.assertEqual(
                state_of(after, "citation", (source, target, "p"))["status"],
                "available",
            )
        # 但全部因端点问题退出规范图，进入 blocked_citations。
        self.assertEqual(after["citations"], [])
        self.assertEqual(len(after["blocked_citations"]), 3)
        self.assertEqual(after["withdrawn_citations"], [])

        block_12 = citation_bucket(after, "blocked_citations", "d1", "d2")
        self.assertEqual(block_12["blocked_endpoints"],
                         [{"endpoint": "to", "document_id": "d2",
                           "status": "withdrawn"}])
        block_23 = citation_bucket(after, "blocked_citations", "d2", "d3")
        self.assertEqual(block_23["blocked_endpoints"],
                         [{"endpoint": "from", "document_id": "d2",
                           "status": "withdrawn"}])
        block_42 = citation_bucket(after, "blocked_citations", "d4", "d2")
        self.assertEqual(block_42["blocked_endpoints"],
                         [{"endpoint": "to", "document_id": "d2",
                           "status": "withdrawn"}])

        # 规范图只剩 d1/d3/d4 三个孤立点：根、深度、祖先全部联动变化。
        self.assertEqual(after["process"]["summary"]["max_depth"], 0)
        self.assertEqual(sorted(after["process"]["roots"]),
                         ["d1", "d3", "d4"])
        self.assertEqual(after["process"]["cited_ancestors"]["d4"], [])
        self.assertNotEqual(before["process"]["roots"],
                            after["process"]["roots"])
        self.assertNotEqual(before["process"]["cited_ancestors"],
                            after["process"]["cited_ancestors"])

    def test_cycle_disappears_with_blocked_endpoint(self):
        data = {
            "documents": [document("d1"), document("d2"), document("d3")],
            "citations": [citation("d1", "d2"), citation("d2", "d3"),
                          citation("d3", "d1")],
        }
        assertions = [
            doc_assertion("d1", "d1", "available", T0, T0),
            doc_assertion("d2", "d2", "available", T0, T0),
            doc_assertion("d3", "d3", "available", T0, T0),
            doc_assertion("d3-off", "d3", "withdrawn", T0, T2,
                          supersedes=["d3"]),
            cit_assertion("c12", "d1", "d2", "available", T0, T0),
            cit_assertion("c23", "d2", "d3", "available", T0, T0),
            cit_assertion("c31", "d3", "d1", "available", T0, T0),
        ]
        result = materialize(
            data, assertions,
            [query("before", T1, T1), query("after", T1, T3)])
        by_id = snapshot_by_id(result)
        self.assertEqual(len(by_id["before"]["process"]["cycles"]), 1)
        self.assertIsNone(by_id["before"]["process"]["summary"]["max_depth"])
        # 撤回 d3 后两条关联边被阻断，剩余 d1->d2 无环。
        self.assertEqual(by_id["after"]["process"]["cycles"], [])
        self.assertEqual(by_id["after"]["process"]["summary"]["max_depth"], 1)
        self.assertEqual(len(by_id["after"]["blocked_citations"]), 2)


class CitationSelfStatusClassificationTest(unittest.TestCase):
    """引用自身撤回/缺证/冲突与端点阻断属于不同分类。"""

    def setUp(self):
        self.data = {
            "documents": [document("d1"), document("d2"),
                          document("d3"), document("d4")],
            "citations": [
                citation("d1", "d2"),
                citation("d2", "d3"),
                citation("d3", "d4"),
                citation("d4", "d1"),
            ],
        }
        for doc_id in ("d1", "d2", "d3", "d4"):
            pass
        self.assertions = [
            doc_assertion(f"doc-{d}", d, "available", T0, T0)
            for d in ("d1", "d2", "d3", "d4")
        ]
        # d1 在 T2 撤回（纠正原 available 声明）：d4->d1 自身可发布但被端点阻断。
        self.assertions.append(
            doc_assertion("doc-d1-off", "d1", "withdrawn", T0, T2,
                          supersedes=["doc-d1"]))
        # d1->d2 自身撤回。
        self.assertions.append(
            cit_assertion("c12-off", "d1", "d2", "withdrawn", T0, T1))
        # d2->d3 存在两条未互相纠正的相反声明：冲突。
        self.assertions.append(
            cit_assertion("c23-a", "d2", "d3", "available", T0, T1))
        self.assertions.append(
            cit_assertion("c23-w", "d2", "d3", "withdrawn", T0, T2))
        # d3->d4 没有任何声明：缺证（unknown）。
        # d4->d1 自身可发布。
        self.assertions.append(
            cit_assertion("c41", "d4", "d1", "available", T0, T1))
        self.queries = [query("q", T3, T3)]

    def test_distinct_buckets(self):
        result = materialize(self.data, self.assertions, self.queries)
        snapshot = result["snapshots"][0]

        withdrawn = citation_bucket(snapshot, "withdrawn_citations",
                                    "d1", "d2")
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertCountEqual(
            withdrawn["evidence"]["covering_assertion_ids"], ["c12-off"])

        conflicted = citation_bucket(snapshot, "conflicted_citations",
                                     "d2", "d3")
        self.assertEqual(conflicted["status"], "conflict")
        self.assertCountEqual(
            conflicted["evidence"]["covering_assertion_ids"],
            ["c23-a", "c23-w"])

        unevidenced = citation_bucket(snapshot, "unevidenced_citations",
                                      "d3", "d4")
        self.assertEqual(unevidenced["status"], "unknown")
        self.assertCountEqual(unevidenced["evidence"]["visible_assertion_ids"], [])
        self.assertCountEqual(unevidenced["evidence"]["frontier_assertion_ids"], [])

        blocked = citation_bucket(snapshot, "blocked_citations", "d4", "d1")
        self.assertEqual(blocked["status"], "available")
        self.assertEqual(blocked["blocked_endpoints"],
                         [{"endpoint": "to", "document_id": "d1",
                           "status": "withdrawn"}])

        # 四个分类互斥；没有一条引用进入规范图。
        all_buckets = (snapshot["blocked_citations"]
                       + snapshot["withdrawn_citations"]
                       + snapshot["unevidenced_citations"]
                       + snapshot["conflicted_citations"])
        self.assertEqual(len(all_buckets), 4)
        self.assertEqual(snapshot["citations"], [])
        # 摘要与各桶计数一致。
        self.assertEqual(snapshot["summary"]["blocked_citations"], 1)
        self.assertEqual(snapshot["summary"]["withdrawn_citations"], 1)
        self.assertEqual(snapshot["summary"]["unevidenced_citations"], 1)
        self.assertEqual(snapshot["summary"]["conflicted_citations"], 1)


class NoPublishableDocumentTest(unittest.TestCase):
    """无可发布文献时规范数组为空、process 为 null，但审计证据完整。"""

    def setUp(self):
        self.data = small_data()
        self.assertions = [
            doc_assertion("d1-off", "d1", "withdrawn", T0, T0),
            doc_assertion("d2-off", "d2", "withdrawn", T0, T0),
            doc_assertion("d3-off", "d3", "withdrawn", T0, T0),
            # 引用自身可发布，但没有可发布端点。
            cit_assertion("c12", "d1", "d2", "available", T0, T0),
        ]
        self.queries = [query("q", T1, T1)]

    def test_null_process_but_full_evidence(self):
        result = materialize(self.data, self.assertions, self.queries)
        snapshot = result["snapshots"][0]
        self.assertEqual(snapshot["documents"], [])
        self.assertEqual(snapshot["citations"], [])
        self.assertIsNone(snapshot["process"])

        # 文献状态与撤回证据完整。
        for doc_id in ("d1", "d2", "d3"):
            entry = state_of(snapshot, "document", doc_id)
            self.assertEqual(entry["status"], "withdrawn")
            self.assertTrue(entry["evidence"]["covering_assertion_ids"])

        # 引用证据按端点阻断保留，而不是被静默丢弃。
        blocked = citation_bucket(snapshot, "blocked_citations", "d1", "d2")
        self.assertEqual(
            sorted(e["endpoint"] for e in blocked["blocked_endpoints"]),
            ["from", "to"])
        self.assertEqual(len(snapshot["document_states"]), 3)
        self.assertEqual(len(snapshot["citation_states"]), 2)


class RegistryAndEndpointStatusTest(unittest.TestCase):
    """顶层共享注册表、conflict/unknown 端点阻断与 process 契约保持。"""

    def setUp(self):
        self.data = {
            "documents": [document("d1"), document("d2"), document("d3")],
            "citations": [citation("d1", "d2"), citation("d2", "d3")],
        }
        self.assertions = [
            # d1：两条未互相纠正的相反声明 -> conflict
            doc_assertion("d1-a", "d1", "available", T0, T1),
            doc_assertion("d1-w", "d1", "withdrawn", T0, T2),
            # d2：无任何声明 -> unknown
            doc_assertion("d3", "d3", "available", T0, T1),
            # 两条引用自身都可发布
            cit_assertion("c12", "d1", "d2", "available", T0, T1),
            cit_assertion("c23", "d2", "d3", "available", T0, T1),
        ]
        self.queries = [query("q", T3, T3)]

    def test_shared_registry_and_id_references(self):
        result = materialize(self.data, self.assertions, self.queries)
        registry = result["assertions"]
        # 注册表按 assertion_id 稳定排序且只构造一次。
        ids = [a["assertion_id"] for a in registry]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(registry), 5)
        # 注册表条目完整保留全部输入字段，且不泄漏 datetime。
        entry = next(a for a in registry if a["assertion_id"] == "d1-w")
        self.assertEqual(entry["state"], "withdrawn")
        self.assertEqual(entry["kind"], "document")
        self.assertEqual(entry["locator"], "d1")
        self.assertEqual(entry["valid_to"], None)
        self.assertEqual(entry["supersedes"], [])
        for key in ("valid_from", "recorded_at"):
            self.assertIsInstance(entry[key], str)
        self.assertIsNone(entry["valid_to"])
        citation_entry = next(a for a in registry if a["assertion_id"] == "c12")
        self.assertEqual(citation_entry["locator"],
                         {"from": "d1", "to": "d2", "page": "p"})

        snapshot = result["snapshots"][0]
        # 快照只持有 id，全部能在注册表中解析。
        registry_ids = set(ids)
        self.assertEqual(set(snapshot["visible_assertion_ids"]),
                         registry_ids)
        all_states = list(snapshot["document_states"].values()) \
            + list(snapshot["citation_states"].values())
        for state in all_states:
            for name, value in state["evidence"].items():
                if name.endswith("_assertion_ids"):
                    self.assertTrue(set(value) <= registry_ids)

    def test_conflict_and_unknown_endpoints_block_publishable_citation(self):
        result = materialize(self.data, self.assertions, self.queries)
        snapshot = result["snapshots"][0]

        self.assertEqual(
            state_of(snapshot, "document", "d1")["status"], "conflict")
        self.assertEqual(
            state_of(snapshot, "document", "d2")["status"], "unknown")
        self.assertEqual(
            state_of(snapshot, "document", "d3")["status"], "available")

        # d1->d2：自身 available，但 from 为 conflict、to 为 unknown。
        block_12 = citation_bucket(snapshot, "blocked_citations", "d1", "d2")
        self.assertEqual(
            sorted((e["endpoint"], e["status"])
                   for e in block_12["blocked_endpoints"]),
            [("from", "conflict"), ("to", "unknown")])
        # d2->d3：from 为 unknown。
        block_23 = citation_bucket(snapshot, "blocked_citations", "d2", "d3")
        self.assertEqual(block_23["blocked_endpoints"],
                         [{"endpoint": "from", "document_id": "d2",
                           "status": "unknown"}])

        # 唯一 available 的 d3 仍构成规范目录，process 正常计算（孤点）。
        self.assertEqual([d["id"] for d in snapshot["documents"]], ["d3"])
        self.assertEqual(snapshot["citations"], [])
        self.assertIsNotNone(snapshot["process"])
        self.assertEqual(snapshot["process"]["roots"], ["d3"])
        self.assertEqual(snapshot["process"]["summary"]["max_depth"], 0)


class ProcessContractPreservationTest(unittest.TestCase):
    def test_citation_state_keys_are_collision_safe_with_pipe_pages(self):
        # page 文本允许出现 '|'；文献 ID 经 ident 校验不含 '|'，
        # (from,to) 已唯一锚定实体，状态映射键不得发生碰撞。
        data = {
            "documents": [document("d1"), document("d2")],
            "citations": [
                {"from": "d1", "to": "d2", "page": "a|b"},
                {"from": "d1", "to": "d2", "page": "c"},
            ],
        }
        assertions = [
            doc_assertion("doc1", "d1", "available", T0, T0),
            doc_assertion("doc2", "d2", "available", T0, T0),
            cit_assertion("c-pipe", "d1", "d2", "available", T0, T0,
                          page="a|b"),
            cit_assertion("c-plain", "d1", "d2", "withdrawn", T0, T0,
                          page="c"),
        ]
        result = materialize(data, assertions, [query("q", T1, T1)])
        states = result["snapshots"][0]["citation_states"]
        self.assertEqual(len(states), 2)
        self.assertEqual(states["d1|d2|a|b"]["status"], "available")
        self.assertEqual(states["d1|d2|c"]["status"], "withdrawn")

    def test_canonical_catalog_keeps_process_field_contract(self):
        docs = [document(f"d{i}") for i in range(3)]
        data = {
            "documents": docs,
            "citations": [citation("d0", "d1"), citation("d1", "d2")],
        }
        assertions = [
            doc_assertion(f"doc-{i}", f"d{i}", "available", T0, T0)
            for i in range(3)
        ]
        assertions.append(
            cit_assertion("c01", "d0", "d1", "available", T0, T0))
        assertions.append(
            cit_assertion("c12", "d1", "d2", "available", T0, T0))
        result = materialize(data, assertions, [query("q", T1, T1)])
        snapshot = result["snapshots"][0]
        # 规范文档保留 process 契约的 date/title 字段。
        self.assertEqual(snapshot["documents"][0],
                         {"id": "d0", "date": "1900-01-01",
                          "title": "档案 d0"})
        lineage = snapshot["process"]
        self.assertEqual(lineage["summary"]["documents"], 3)
        self.assertEqual(lineage["summary"]["citations"], 2)
        self.assertEqual(lineage["roots"], ["d0"])
        self.assertEqual(lineage["summary"]["max_depth"], 2)

    def test_data_contract_violations_still_raise(self):
        bad_data = {
            "documents": [document("d1")],
            "citations": [{"from": "d1", "to": "ghost", "page": "p"}],
        }
        with self.assertRaises(ValueError):
            materialize(bad_data, [], [query("q", T0, T1)])


class EmptyInputTest(unittest.TestCase):
    def test_no_assertions_means_unknown_everywhere(self):
        result = materialize(small_data(), [], [query("q", T1, T1)])
        snapshot = result["snapshots"][0]
        self.assertEqual(snapshot["process"], None)
        for entry in snapshot["document_states"].values():
            self.assertEqual(entry["status"], "unknown")
            self.assertCountEqual(entry["evidence"]["visible_assertion_ids"], [])
        for entry in snapshot["citation_states"].values():
            self.assertEqual(entry["status"], "unknown")
        self.assertEqual(snapshot["blocked_citations"], [])
        self.assertEqual(snapshot["summary"]["visible_assertions"], 0)

    def test_no_queries_returns_empty_snapshot_list(self):
        result = materialize(small_data(), [], [])
        self.assertEqual(result, {"assertions": [], "snapshots": []})


class ValidationFailureTest(unittest.TestCase):
    """非法输入使整次调用失败且输入不变。"""

    def setUp(self):
        self.data = small_data()

    def _expect_failure(self, assertions=None, queries=None):
        assertions = assertions if assertions is not None else [
            doc_assertion("a", "d1", "available", T0, T0)]
        queries = queries if queries is not None else [query("q", T1, T1)]
        data_copy = copy.deepcopy(self.data)
        assertions_copy = copy.deepcopy(assertions)
        queries_copy = copy.deepcopy(queries)
        with self.assertRaises(ValueError):
            materialize(self.data, assertions, queries)
        # 失败后调用方输入原样保留。
        self.assertEqual(self.data, data_copy)
        self.assertEqual(assertions, assertions_copy)
        self.assertEqual(queries, queries_copy)

    def test_bad_timestamp_formats(self):
        for bad in ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00",
                    "2020-01-01 00:00:00Z", "2020-13-01T00:00:00Z",
                    "2020-02-30T00:00:00Z", "not-a-time"):
            self._expect_failure(assertions=[
                doc_assertion("a", "d1", "available", bad, T0)])
        self._expect_failure(queries=[query("q", "not-a-time", T1)])

    def test_valid_to_must_be_strictly_later(self):
        self._expect_failure(assertions=[
            doc_assertion("a", "d1", "available", T1, T0, valid_to=T1)])
        self._expect_failure(assertions=[
            doc_assertion("a", "d1", "available", T2, T0, valid_to=T1)])

    def test_entity_outside_data_universe(self):
        self._expect_failure(assertions=[
            doc_assertion("a", "ghost", "available", T0, T0)])
        self._expect_failure(assertions=[
            cit_assertion("a", "d1", "ghost", "available", T0, T0)])
        self._expect_failure(assertions=[
            cit_assertion("a", "d1", "d2", "available", T0, T0, page="q")])

    def test_dangling_cross_entity_and_order_inversion(self):
        base = doc_assertion("a", "d1", "available", T0, T1)
        dangling = doc_assertion("b", "d1", "withdrawn", T0, T2,
                                 supersedes=["nope"])
        cross = [
            base,
            doc_assertion("b", "d1", "withdrawn", T0, T2),
            cit_assertion("c", "d1", "d2", "available", T0, T1,
                          supersedes=["b"]),
        ]
        inversion = [
            doc_assertion("a", "d1", "available", T0, T2),
            doc_assertion("b", "d1", "withdrawn", T0, T1,
                          supersedes=["a"]),
        ]
        self._expect_failure(assertions=[base, dangling])
        self._expect_failure(assertions=cross)
        self._expect_failure(assertions=inversion)

    def test_duplicate_supersede_target_and_id(self):
        self._expect_failure(assertions=[
            doc_assertion("a", "d1", "available", T0, T1),
            doc_assertion("b", "d1", "withdrawn", T0, T2,
                          supersedes=["a", "a"]),
        ])
        self._expect_failure(assertions=[
            doc_assertion("a", "d1", "available", T0, T0),
            doc_assertion("a", "d2", "available", T0, T1),
        ])

    def test_explicit_supersede_cycle_rejected(self):
        # recorded_at 倒挂本身就必须拒绝。
        inversion_ring = [
            doc_assertion("a", "d1", "available", T0, T1),
            doc_assertion("b", "d1", "available", T0, T2,
                          supersedes=["a"]),
            doc_assertion("c", "d1", "available", T0, T1,
                          supersedes=["b"]),
        ]
        self._expect_failure(assertions=inversion_ring)

        # 自纠正也是环的一种。
        self_loop = [
            doc_assertion("a", "d1", "available", T0, T1,
                          supersedes=["a"]),
        ]
        self._expect_failure(assertions=self_loop)

    def test_supersede_edges_stay_acyclic_under_validation(self):
        # 即便所有目标存在、同实体且记录时间合法，环仍由独立环检测拦截；
        # 这里用 monkey patch 绕过 recorded_at 约束直接喂入环，验证环检测。
        from citation_lineage import snapshot as snapshot_module
        nodes = [
            {"index": 0, "assertion_id": "a", "recorded_at": 1,
             "supersedes_nodes": [2]},
            {"index": 1, "assertion_id": "b", "recorded_at": 2,
             "supersedes_nodes": [0]},
            {"index": 2, "assertion_id": "c", "recorded_at": 3,
             "supersedes_nodes": [1]},
        ]
        with self.assertRaises(MaterializationError):
            snapshot_module._reject_cycles(nodes)
        # 无环 DAG 不报错。
        dag = [
            {"index": 0, "assertion_id": "a", "recorded_at": 1,
             "supersedes_nodes": []},
            {"index": 1, "assertion_id": "b", "recorded_at": 2,
             "supersedes_nodes": [0]},
            {"index": 2, "assertion_id": "c", "recorded_at": 3,
             "supersedes_nodes": [0, 1]},
        ]
        self.assertIsNone(snapshot_module._reject_cycles(dag))

    def test_invalid_state_kind_and_fields(self):
        bad_state = doc_assertion("a", "d1", "published", T0, T0)
        self._expect_failure(assertions=[bad_state])
        bad_kind = assertion("a", "film", "d1", "available", T0, T0)
        self._expect_failure(assertions=[bad_kind])
        bad_locator = assertion("a", "citation", "d1", "available", T0, T0)
        self._expect_failure(assertions=[bad_locator])
        too_many = [
            doc_assertion("a", "d1", "available", T0, T0, supersedes=[])
        ]
        too_many[0]["supersedes"] = [f"x{i}" for i in range(21)]
        self._expect_failure(assertions=too_many)

    def test_duplicate_query_id_and_limit_violations(self):
        self._expect_failure(queries=[query("q", T0, T1),
                                      query("q", T0, T2)])
        with self.assertRaises(ValueError):
            materialize(self.data,
                        [doc_assertion(f"a{i}", "d1", "available", T0, T0)
                         for i in range(30001)],
                        [query("q", T0, T1)])
        with self.assertRaises(ValueError):
            materialize(self.data, [],
                        [query(f"q{i}", T0, T1) for i in range(1001)])


class DeterminismAndScaleTest(unittest.TestCase):
    def _large_input(self, doc_count=120, citation_count=400,
                     assertion_count=2000, query_count=25, seed=7):
        rng = random.Random(seed)
        docs = [document(f"d{i}") for i in range(doc_count)]
        citations = []
        seen = set()
        while len(citations) < citation_count:
            source = rng.randrange(doc_count)
            target = rng.randrange(doc_count)
            if source == target:
                continue
            edge = (f"d{source}", f"d{target}", f"p{rng.randrange(1, 4)}")
            if edge in seen:
                continue
            seen.add(edge)
            citations.append(citation(*edge))
        data = {"documents": docs, "citations": citations}

        assertions = []
        doc_ids = [d["id"] for d in docs]
        for i in range(assertion_count):
            if rng.random() < 0.5:
                kind = "document"
                locator = doc_locator(rng.choice(doc_ids))
            else:
                edge = rng.choice(citations)
                kind = "citation"
                locator = cit_locator(edge["from"], edge["to"], edge["page"])
            state = rng.choice(("available", "withdrawn"))
            month = rng.randrange(1, 12)
            recorded_at = f"2021-{month:02d}-15T00:00:00Z"
            assertions.append(assertion(
                f"a{i:06d}", kind, locator, state, T0, recorded_at,
                valid_to=None if rng.random() < 0.7 else
                "2024-01-01T00:00:00Z"))
        queries = [
            query(f"q{i:03d}",
                  f"2022-{((i % 11) + 1):02d}-01T00:00:00Z",
                  f"2021-{((i % 11) + 1):02d}-20T00:00:00Z")
            for i in range(query_count)
        ]
        return data, assertions, queries

    def test_shuffling_inputs_does_not_change_output(self):
        data, assertions, queries = self._large_input()
        reference = materialize(data, assertions, queries)

        rng = random.Random(99)
        shuffled_data = {
            "documents": data["documents"][:],
            "citations": data["citations"][:],
        }
        rng.shuffle(shuffled_data["documents"])
        rng.shuffle(shuffled_data["citations"])
        shuffled_assertions = assertions[:]
        rng.shuffle(shuffled_assertions)
        for item in shuffled_assertions:
            rng.shuffle(item["supersedes"])
        shuffled_queries = queries[:]
        rng.shuffle(shuffled_queries)

        shuffled_result = materialize(
            shuffled_data, shuffled_assertions, shuffled_queries)
        self.assertEqual(shuffled_result, reference)
        # 快照严格按 query_id 排序。
        ids = [s["query_id"] for s in shuffled_result["snapshots"]]
        self.assertEqual(ids, sorted(ids))

    def test_output_is_strict_json_serializable(self):
        data, assertions, queries = self._large_input()
        result = materialize(data, assertions, queries)
        # 严格 JSON 序列化：无 NaN/Infinity，往返后结构与内容一致。
        encoded = json.dumps(result, allow_nan=False, sort_keys=True)
        roundtrip = json.loads(encoded)
        self.assertEqual(roundtrip, json.loads(json.dumps(result,
                                                           allow_nan=False)))
        # 元组在 JSON 中序列化为数组；快照顶层键集合固定。
        expected_snapshot_keys = {
            "query_id", "valid_at", "known_at", "visible_assertion_ids",
            "document_states", "citation_states", "documents", "citations",
            "blocked_citations", "withdrawn_citations",
            "unevidenced_citations", "conflicted_citations", "process",
            "summary",
        }
        for snapshot in result["snapshots"]:
            self.assertEqual(set(snapshot), expected_snapshot_keys)
            self.assertIsInstance(
                snapshot["visible_assertion_ids"], (list, tuple))

    def test_large_scale_runs_and_keeps_bitemporal_difference(self):
        # 接近合法最大规模：30000 声明、数百查询；独立覆盖“能处理合法最大
        # 规模且输出确定、严格可序列化”的验收项，同时保持常规执行可承受。
        data, assertions, queries = self._large_input(
            doc_count=500, citation_count=2500,
            assertion_count=30000, query_count=300)
        result = materialize(data, assertions, queries)
        self.assertEqual(len(result["snapshots"]), 300)
        # 不同知识截止的可见声明数单调不减，且早期与晚期快照确实存在差异。
        visible_counts = [
            s["summary"]["visible_assertions"] for s in
            sorted(result["snapshots"],
                   key=lambda s: (s["known_at"], s["query_id"]))]
        self.assertEqual(visible_counts, sorted(visible_counts))
        self.assertLess(visible_counts[0], visible_counts[-1])
        # 全部快照均可严格 JSON 序列化（不保留往返副本以控制内存），
        # 且注册表完整覆盖被引用的声明。
        encoded = json.dumps(result, allow_nan=False)
        self.assertTrue(encoded.startswith("{"))
        registry_ids = {a["assertion_id"] for a in result["assertions"]}
        for snapshot in result["snapshots"]:
            for assertion_id in snapshot["visible_assertion_ids"]:
                self.assertIn(assertion_id, registry_ids)

    def test_inputs_are_not_mutated_on_success(self):
        data, assertions, queries = self._large_input(
            doc_count=20, citation_count=40, assertion_count=80,
            query_count=5)
        data_copy = copy.deepcopy(data)
        assertions_copy = copy.deepcopy(assertions)
        queries_copy = copy.deepcopy(queries)
        materialize(data, assertions, queries)
        self.assertEqual(data, data_copy)
        self.assertEqual(assertions, assertions_copy)
        self.assertEqual(queries, queries_copy)


if __name__ == "__main__":
    unittest.main()




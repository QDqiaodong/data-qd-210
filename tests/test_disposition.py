"""`assess_disposition` 处置评估的合成档案数据测试。

全部数据为原创合成，不访问任何外部档案服务或网络。运行方式：

    python -I -m unittest discover -s tests -v
"""

import copy
import json
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from citation_lineage import DispositionError, assess_disposition, process  # noqa: E402

AS_OF = "2024-06-01"
EXPIRED = "2020-01-01"
FUTURE = "2030-01-01"


def document(doc_id):
    return {"id": doc_id, "date": "1900-01-01", "title": f"档案 {doc_id}"}


def citation(source, target, page="p1"):
    return {"from": source, "to": target, "page": page}


def record(doc_id, until=EXPIRED, size_bytes=10, record_class="public-record"):
    return {
        "id": doc_id,
        "record_class": record_class,
        "retention_until": until,
        "size_bytes": size_bytes,
    }


def hold(hold_id, document_ids, status="active", reason="litigation"):
    return {
        "hold_id": hold_id,
        "document_ids": list(document_ids),
        "status": status,
        "reason": reason,
    }


def request(candidate_ids, as_of=AS_OF, disposition_id="disp-1",
            reason="scheduled destruction"):
    return {
        "disposition_id": disposition_id,
        "as_of": as_of,
        "candidate_ids": list(candidate_ids),
        "reason": reason,
    }


def data_of(documents, citations):
    return {
        "documents": [document(doc_id) for doc_id in documents],
        "citations": list(citations),
    }


def decisions_by_id(result):
    return {item["document_id"]: item for item in result["decisions"]}


class ExpiredButCitedTest(unittest.TestCase):
    def test_expired_candidate_cited_by_non_candidate_is_blocked(self):
        data = data_of(["anchor", "candidate"], [citation("anchor", "candidate")])
        records = [record("anchor", until=None), record("candidate")]
        result = assess_disposition(
            data, records, [], request(["candidate"])
        )

        decision = decisions_by_id(result)["candidate"]
        self.assertEqual(decision["decision"], "retain")
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["reasons"], ["required_as_citation_evidence"])
        self.assertEqual(result["allowed_document_ids"], [])
        self.assertEqual(result["blocked_document_ids"], ["candidate"])
        evidence = decision["citation_evidence"]
        self.assertEqual(evidence["anchor_id"], "anchor")
        self.assertEqual(evidence["document_ids"], ["anchor", "candidate"])
        self.assertEqual(
            evidence["locators"],
            [{"from": "anchor", "to": "candidate", "page": "p1"}],
        )


class MultiLevelChainTest(unittest.TestCase):
    def test_evidence_closure_blocks_two_level_chain(self):
        # anchor -> mid -> leaf；mid/leaf 都过期且在候选中，仍须保留。
        data = data_of(
            ["anchor", "mid", "leaf"],
            [citation("anchor", "mid"), citation("mid", "leaf")],
        )
        records = [
            record("anchor", until=None),
            record("mid"),
            record("leaf"),
        ]
        result = assess_disposition(
            data, records, [], request(["mid", "leaf"])
        )

        decisions = decisions_by_id(result)
        self.assertEqual(decisions["mid"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(decisions["leaf"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(result["allowed_document_ids"], [])
        leaf_evidence = decisions["leaf"]["citation_evidence"]
        self.assertEqual(leaf_evidence["anchor_id"], "anchor")
        self.assertEqual(leaf_evidence["document_ids"],
                         ["anchor", "mid", "leaf"])
        self.assertEqual(leaf_evidence["edge_count"], 2)
        self.assertEqual(
            leaf_evidence["locators"],
            [
                {"from": "anchor", "to": "mid", "page": "p1"},
                {"from": "mid", "to": "leaf", "page": "p1"},
            ],
        )

    def test_unreachable_expired_candidate_is_disposed(self):
        data = data_of(
            ["anchor", "keep", "cut"],
            [citation("anchor", "keep")],
        )
        records = [
            record("anchor", until=None),
            record("keep"),
            record("cut"),
        ]
        result = assess_disposition(data, records, [], request(["keep", "cut"]))

        decisions = decisions_by_id(result)
        self.assertEqual(decisions["keep"]["decision"], "retain")
        self.assertEqual(decisions["cut"]["decision"], "dispose")
        self.assertEqual(decisions["cut"]["reasons"], [])
        self.assertIsNone(decisions["cut"]["citation_evidence"])
        self.assertEqual(result["allowed_document_ids"], ["cut"])


class AnchorPropagationTest(unittest.TestCase):
    def test_candidate_anchor_by_hold_protects_downstream_candidate(self):
        # blocked (active hold) 自身保住，并保护它引用的 downstream。
        data = data_of(
            ["blocked", "downstream"],
            [citation("blocked", "downstream")],
        )
        records = [record("blocked"), record("downstream")]
        holds = [hold("h1", ["blocked"], status="active")]
        result = assess_disposition(
            data, records, holds, request(["blocked", "downstream"])
        )

        decisions = decisions_by_id(result)
        self.assertEqual(decisions["blocked"]["reasons"], ["active_hold"])
        self.assertEqual(decisions["downstream"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(decisions["downstream"]["citation_evidence"]["anchor_id"],
                         "blocked")
        self.assertEqual(result["allowed_document_ids"], [])

    def test_candidate_anchor_by_unexpired_retention_protects_downstream(self):
        data = data_of(
            ["recent", "downstream"],
            [citation("recent", "downstream")],
        )
        records = [
            record("recent", until=FUTURE),
            record("downstream"),
        ]
        result = assess_disposition(
            data, records, [], request(["recent", "downstream"])
        )

        decisions = decisions_by_id(result)
        self.assertEqual(decisions["recent"]["reasons"],
                         ["retention_not_elapsed"])
        self.assertEqual(decisions["downstream"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertIn("recent", result["anchor_document_ids"])


class CombinedReasonsTest(unittest.TestCase):
    def test_candidate_reports_retention_multiple_holds_and_evidence(self):
        data = data_of(
            ["root", "target"],
            [citation("root", "target")],
        )
        records = [record("root", until=None), record("target", until=FUTURE)]
        holds = [
            hold("h-a", ["target"], status="active"),
            hold("h-b", ["target", "root"], status="active"),
            hold("h-old", ["target"], status="released"),
        ]
        result = assess_disposition(
            data, records, holds, request(["target"])
        )

        decision = decisions_by_id(result)["target"]
        self.assertEqual(
            decision["reasons"],
            [
                "active_hold",
                "required_as_citation_evidence",
                "retention_not_elapsed",
            ],
        )
        self.assertEqual(decision["active_hold_ids"], ["h-a", "h-b"])
        self.assertEqual(decision["released_hold_ids"], ["h-old"])
        self.assertEqual(
            [(item["hold_id"], item["status"]) for item in decision["hold_evidence"]],
            [("h-a", "active"), ("h-b", "active"), ("h-old", "released")],
        )

    def test_permanent_retention_is_marked(self):
        data = data_of(["keep"], [])
        records = [record("keep", until=None)]
        result = assess_disposition(data, records, [], request(["keep"]))
        decision = decisions_by_id(result)["keep"]
        self.assertEqual(decision["reasons"], ["permanent_retention"])
        self.assertTrue(decision["retention"]["permanent"])
        self.assertFalse(decision["retention"]["expired"])


class CycleTest(unittest.TestCase):
    def test_cycle_all_expired_candidates_removed_together(self):
        data = data_of(
            ["a", "b", "c"],
            [citation("a", "b"), citation("b", "a")],
        )
        records = [record("a"), record("b"), record("c")]
        result = assess_disposition(data, records, [], request(["a", "b"]))

        self.assertEqual(result["allowed_document_ids"], ["a", "b"])
        self.assertEqual(result["blocked_document_ids"], [])
        for decision in result["decisions"]:
            self.assertEqual(decision["reasons"], [])
            self.assertIsNone(decision["citation_evidence"])

    def test_partial_cycle_protected_when_anchor_reaches_it(self):
        data = data_of(
            ["a", "b", "c"],
            [citation("a", "b"), citation("b", "a"), citation("c", "a")],
        )
        records = [record("a"), record("b"), record("c")]
        result = assess_disposition(data, records, [], request(["a", "b"]))

        decisions = decisions_by_id(result)
        self.assertEqual(decisions["a"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(decisions["b"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(result["allowed_document_ids"], [])
        # 见证仍是最少边路径：c -> a（而不是绕行 b）。
        self.assertEqual(decisions["a"]["citation_evidence"]["anchor_id"], "c")
        self.assertEqual(
            decisions["a"]["citation_evidence"]["locators"],
            [{"from": "c", "to": "a", "page": "p1"}],
        )

    def test_cycle_blocked_by_active_hold_member_protects_whole_cycle(self):
        data = data_of(
            ["a", "b", "c"],
            [citation("a", "b"), citation("b", "a")],
        )
        records = [record("a"), record("b"), record("c")]
        holds = [hold("h1", ["b"], status="active")]
        result = assess_disposition(data, records, holds, request(["a", "b"]))

        decisions = decisions_by_id(result)
        # a 仅因位于 b 的证据闭包内而保留；b 自身由 active hold 保护，不再重复
        # 标记引用证据原因（锚点自身不在自己的引用闭包内）。
        self.assertEqual(decisions["a"]["reasons"],
                         ["required_as_citation_evidence"])
        self.assertEqual(decisions["b"]["reasons"], ["active_hold"])
        self.assertEqual(result["allowed_document_ids"], [])


class WitnessSelectionTest(unittest.TestCase):
    def test_smallest_anchor_chosen_among_multiple_anchors(self):
        # 两个非候选锚点 zeta 与 alpha 都能一步到达 target；选字典序最小的 alpha。
        data = data_of(
            ["alpha", "zeta", "target"],
            [citation("alpha", "target"), citation("zeta", "target")],
        )
        records = [record("alpha", until=None), record("zeta", until=None),
                   record("target")]
        result = assess_disposition(data, records, [], request(["target"]))

        evidence = decisions_by_id(result)["target"]["citation_evidence"]
        self.assertEqual(evidence["anchor_id"], "alpha")
        self.assertEqual(result["anchor_document_ids"], ["alpha", "zeta"])
        self.assertEqual(evidence["locators"],
                         [{"from": "alpha", "to": "target", "page": "p1"}])

    def test_shortest_of_multiple_equal_and_unequal_paths(self):
        # 直接边比绕行更短，必须选择直接边。
        data = data_of(
            ["a", "m", "target"],
            [
                citation("a", "target", "direct"),
                citation("a", "m"),
                citation("m", "target"),
            ],
        )
        records = [record("a", until=None), record("m", until=None),
                   record("target")]
        result = assess_disposition(data, records, [], request(["target"]))
        evidence = decisions_by_id(result)["target"]["citation_evidence"]
        self.assertEqual(evidence["edge_count"], 1)
        self.assertEqual(evidence["locators"],
                         [{"from": "a", "to": "target", "page": "direct"}])

    def test_equal_length_paths_lexicographic_locator_sequence(self):
        # a->b->d 与 a->c->d 等长；比较完整定位序列，(a,b,...) < (a,c,...)。
        data = data_of(
            ["a", "b", "c", "d"],
            [
                citation("a", "b"),
                citation("a", "c"),
                citation("b", "d"),
                citation("c", "d"),
            ],
        )
        records = [record("a", until=None), record("b", until=None),
                   record("c", until=None), record("d")]
        result = assess_disposition(data, records, [], request(["d"]))
        evidence = decisions_by_id(result)["d"]["citation_evidence"]
        self.assertEqual(evidence["document_ids"], ["a", "b", "d"])
        self.assertEqual(
            evidence["locators"],
            [
                {"from": "a", "to": "b", "page": "p1"},
                {"from": "b", "to": "d", "page": "p1"},
            ],
        )

    def test_same_edge_multiple_pages_keeps_lexicographically_smallest(self):
        data = data_of(
            ["a", "d"],
            [citation("a", "d", "p9"), citation("a", "d", "p1")],
        )
        records = [record("a", until=None), record("d")]
        result = assess_disposition(data, records, [], request(["d"]))
        evidence = decisions_by_id(result)["d"]["citation_evidence"]
        self.assertEqual(evidence["locators"],
                         [{"from": "a", "to": "d", "page": "p1"}])
        # 同一对文献的不同 page 都仍在处置前目录中。
        self.assertEqual(len(result["process"]["citations"]), 2)

    def test_witness_anchor_is_lexicographically_smallest_reachable_anchor(self):
        # 两个锚点都能到达 target，但只有较小 ID 的锚点路径被展示。
        data = data_of(
            ["a1", "a2", "x", "target"],
            [
                citation("a1", "x"),
                citation("x", "target"),
                citation("a2", "target"),
            ],
        )
        records = [record("a1", until=None), record("a2", until=None),
                   record("x", until=None), record("target")]
        result = assess_disposition(data, records, [], request(["target"]))
        evidence = decisions_by_id(result)["target"]["citation_evidence"]
        self.assertEqual(evidence["anchor_id"], "a1")
        self.assertEqual(evidence["edge_count"], 2)


class PostDispositionTest(unittest.TestCase):
    def test_removed_citations_and_process_lineage_consistent(self):
        # keep（永久保留）是锚点并保护它引用的 shared；cut 自身过期、无 hold，
        # 且没有被任何锚点引用，因此可处置——它指向 keep/shared 的定位被移除。
        data = data_of(
            ["keep", "cut", "shared"],
            [
                citation("keep", "shared", "p1"),
                citation("cut", "shared", "p2"),
                citation("cut", "keep", "p5"),
                citation("shared", "keep", "p4"),
            ],
        )
        records = [
            record("keep", until=None),
            record("cut"),
            record("shared"),
        ]
        result = assess_disposition(data, records, [], request(["cut"]))

        self.assertEqual(result["allowed_document_ids"], ["cut"])
        self.assertEqual(result["blocked_document_ids"], [])
        self.assertEqual(
            result["removed_citations"],
            [
                {"from": "cut", "to": "keep", "page": "p5"},
                {"from": "cut", "to": "shared", "page": "p2"},
            ],
        )
        self.assertEqual(
            result["citations"],
            [
                {"from": "keep", "to": "shared", "page": "p1"},
                {"from": "shared", "to": "keep", "page": "p4"},
            ],
        )
        self.assertEqual([doc["id"] for doc in result["documents"]],
                         ["keep", "shared"])
        # 处置后的回顾谱系必须与直接对该目录运行 process 一致。
        expected = process({
            "documents": result["documents"],
            "citations": result["citations"],
        })
        self.assertEqual(result["process_after"], expected)
        self.assertIsNotNone(result["process_after"])

    def test_same_document_pair_pages_removed_independently(self):
        # 同一对文献的两条不同页码定位都因 from 端点被处置而移除，不合并去重。
        data = data_of(
            ["a", "b"],
            [citation("b", "a", "p1"), citation("b", "a", "p2")],
        )
        records = [record("a", until=None), record("b")]
        result = assess_disposition(data, records, [], request(["b"]))

        self.assertEqual(result["allowed_document_ids"], ["b"])
        self.assertEqual(
            result["removed_citations"],
            [
                {"from": "b", "to": "a", "page": "p1"},
                {"from": "b", "to": "a", "page": "p2"},
            ],
        )
        self.assertEqual(result["citations"], [])

    def test_all_disposed_process_null_but_evidence_complete(self):
        data = data_of(
            ["a", "b"],
            [citation("a", "b"), citation("b", "a", "back")],
        )
        records = [record("a", size_bytes=7), record("b", size_bytes=11)]
        result = assess_disposition(data, records, [], request(["a", "b"]))

        self.assertEqual(result["allowed_document_ids"], ["a", "b"])
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["citations"], [])
        self.assertIsNone(result["process_after"])
        self.assertEqual(result["total_allowed_bytes"], 18)
        self.assertEqual(len(result["decisions"]), 2)
        self.assertEqual(
            result["removed_citations"],
            [
                {"from": "a", "to": "b", "page": "p1"},
                {"from": "b", "to": "a", "page": "back"},
            ],
        )
        # 至少一篇文献限制不得放宽：处置前 process 仍完整。
        self.assertEqual(result["process"]["summary"]["documents"], 2)

    def test_document_date_is_not_used_to_infer_retention(self):
        # data.date 是 1900 年，但保管记录未到期：仍须保留。
        data = data_of(["only"], [])
        records = [record("only", until=FUTURE)]
        result = assess_disposition(data, records, [], request(["only"]))
        decision = decisions_by_id(result)["only"]
        self.assertEqual(decision["decision"], "retain")
        self.assertEqual(decision["reasons"], ["retention_not_elapsed"])

    def test_retention_boundary_is_inclusive(self):
        data = data_of(["only"], [])
        exact = assess_disposition(
            data, [record("only", until=AS_OF)], [], request(["only"])
        )
        after = assess_disposition(
            data, [record("only", until="2024-05-31")], [], request(["only"])
        )
        self.assertEqual(decisions_by_id(exact)["only"]["decision"], "retain")
        self.assertEqual(decisions_by_id(after)["only"]["decision"], "dispose")


class InvalidInputTest(unittest.TestCase):
    def setUp(self):
        self.data = data_of(["a", "b"], [citation("a", "b")])
        self.records = [record("a"), record("b")]
        self.holds = [hold("h1", ["a"])]
        self.request = request(["b"])

    def assert_rejected(self, data, records, holds, request_obj):
        snapshot = copy.deepcopy((data, records, holds, request_obj))
        with self.assertRaises(ValueError):
            assess_disposition(data, records, holds, request_obj)
        self.assertEqual((data, records, holds, request_obj), snapshot)

    def test_record_must_cover_every_document(self):
        self.assert_rejected(self.data, self.records[:1], self.holds,
                             self.request)

    def test_duplicate_record_rejected(self):
        self.assert_rejected(self.data, self.records + [record("a")],
                             self.holds, self.request)

    def test_unknown_record_document_rejected(self):
        self.assert_rejected(self.data, self.records + [record("zzz")],
                             self.holds, self.request)

    def test_invalid_calendar_date_rejected(self):
        bad = [record("a", until="2023-02-29"), record("b")]
        self.assert_rejected(self.data, bad, self.holds, self.request)

    def test_malformed_date_rejected(self):
        bad = [record("a", until="2020-1-1"), record("b")]
        self.assert_rejected(self.data, bad, self.holds, self.request)

    def test_malformed_as_of_rejected(self):
        bad_request = request(["b"], as_of="not-a-date")
        self.assert_rejected(self.data, self.records, self.holds, bad_request)

    def test_duplicate_hold_identity_rejected(self):
        bad = [hold("h1", ["a"]), hold("h1", ["b"])]
        self.assert_rejected(self.data, self.records, bad, self.request)

    def test_unknown_hold_document_rejected(self):
        self.assert_rejected(self.data, self.records, [hold("h1", ["zzz"])],
                             self.request)

    def test_duplicate_hold_document_rejected(self):
        self.assert_rejected(self.data, self.records, [hold("h1", ["a", "a"])],
                             self.request)

    def test_invalid_hold_status_rejected(self):
        self.assert_rejected(self.data, self.records,
                             [hold("h1", ["a"], status="pending")],
                             self.request)

    def test_unknown_candidate_rejected(self):
        self.assert_rejected(self.data, self.records, self.holds,
                             request(["zzz"]))

    def test_duplicate_candidate_rejected(self):
        self.assert_rejected(self.data, self.records, self.holds,
                             request(["b", "b"]))

    def test_size_bounds_rejected(self):
        self.assert_rejected(self.data,
                             [record("a", size_bytes=0), record("b")],
                             self.holds, self.request)
        self.assert_rejected(self.data,
                             [record("a", size_bytes=100_000_001), record("b")],
                             self.holds, self.request)

    def test_bool_size_rejected(self):
        self.assert_rejected(self.data,
                             [record("a", size_bytes=True), record("b")],
                             self.holds, self.request)

    def test_extra_fields_rejected(self):
        bad = [dict(record("a"), extra=1), record("b")]
        self.assert_rejected(self.data, bad, self.holds, self.request)

    def test_empty_documents_rejected(self):
        self.assert_rejected({"documents": [], "citations": []}, [],
                             [], request(["a"]))


class DeterminismTest(unittest.TestCase):
    def _scenario(self):
        data = data_of(
            ["a1", "a2", "b", "c", "d", "e"],
            [
                citation("a1", "b", "p2"),
                citation("a1", "b", "p1"),
                citation("a2", "b", "p9"),
                citation("b", "c"),
                citation("c", "d"),
                citation("d", "e"),
                citation("e", "c"),
                citation("b", "e", "q"),
            ],
        )
        records = [
            record("a1", until=None),
            record("a2", until=None),
            record("b", size_bytes=3),
            record("c", size_bytes=5),
            record("d", size_bytes=7),
            record("e", size_bytes=11),
        ]
        holds = [
            hold("h-zeta", ["b"], status="active"),
            hold("h-alpha", ["d"], status="released"),
        ]
        return data, records, holds, request(["b", "c", "d", "e"])

    def test_shuffling_nonsemantic_order_is_identity_stable(self):
        data, records, holds, req = self._scenario()
        baseline = assess_disposition(data, records, holds, req)
        expected_json = json.dumps(baseline, sort_keys=True, allow_nan=False)

        rng = random.Random(20240601)
        for _ in range(25):
            shuffled_data = {
                "documents": rng.sample(data["documents"],
                                        len(data["documents"])),
                "citations": rng.sample(data["citations"],
                                        len(data["citations"])),
            }
            shuffled_records = rng.sample(records, len(records))
            shuffled_holds = []
            for item in rng.sample(holds, len(holds)):
                detailed = dict(item)
                detailed["document_ids"] = rng.sample(
                    item["document_ids"], len(item["document_ids"])
                )
                shuffled_holds.append(detailed)
            shuffled_req = dict(req)
            shuffled_req["candidate_ids"] = rng.sample(
                req["candidate_ids"], len(req["candidate_ids"])
            )

            result = assess_disposition(
                shuffled_data, shuffled_records, shuffled_holds, shuffled_req
            )
            self.assertEqual(
                json.dumps(result, sort_keys=True, allow_nan=False),
                expected_json,
            )

    def test_witness_stable_across_input_shuffles(self):
        data, records, holds, req = self._scenario()
        baseline = assess_disposition(data, records, holds, req)
        reversed_data = {
            "documents": list(reversed(data["documents"])),
            "citations": list(reversed(data["citations"])),
        }
        reversed_result = assess_disposition(
            reversed_data, list(reversed(records)),
            [dict(item, document_ids=list(reversed(item["document_ids"])))
             for item in reversed(holds)],
            dict(req, candidate_ids=list(reversed(req["candidate_ids"]))),
        )
        baseline_b = decisions_by_id(baseline)["b"]["citation_evidence"]
        reversed_b = decisions_by_id(reversed_result)["b"]["citation_evidence"]
        self.assertEqual(baseline_b, reversed_b)

    def test_result_is_json_serialisable(self):
        data, records, holds, req = self._scenario()
        result = assess_disposition(data, records, holds, req)
        encoded = json.dumps(result, allow_nan=False)
        self.assertIn("disposition_id", encoded)


if __name__ == "__main__":
    unittest.main()

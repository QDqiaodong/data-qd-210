import copy
import hashlib
import json
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from citation_lineage import HandoffError, prepare_handoff


def sha_for(document_id):
    return hashlib.sha256(document_id.encode("utf-8")).hexdigest()


def make_payload(document_id, size_bytes=10):
    return {
        "document_id": document_id,
        "sha256": sha_for(document_id),
        "size_bytes": size_bytes,
    }


def make_data(documents, citations):
    return {
        "documents": [
            {"id": document_id, "date": "2024-01-01", "title": f"Title {document_id}"}
            for document_id in documents
        ],
        "citations": citations,
    }


def cite(source, target, page="p1"):
    return {"from": source, "to": target, "page": page}


def make_request(
    seeds,
    documents,
    *,
    sizes=None,
    max_documents=1000,
    max_bytes=1_000_000_000,
    handoff_id="handoff-1",
    payload_id_key="document_id",
):
    sizes = sizes or {}
    payloads = []
    for document_id in documents:
        payload = make_payload(document_id, sizes.get(document_id, 10))
        if payload_id_key == "id":
            payload = {
                "id": document_id,
                "sha256": payload["sha256"],
                "size_bytes": payload["size_bytes"],
            }
        payloads.append(payload)
    return {
        "handoff_id": handoff_id,
        "seed_ids": list(seeds),
        "payloads": payloads,
        "max_documents_per_batch": max_documents,
        "max_bytes_per_batch": max_bytes,
        "purpose": "offline archive transfer",
    }


def ack_for(result, batch_number, *, received_at=None, digest=None, handoff_id=None):
    batch = result["plan"]["batches"][batch_number - 1]
    return {
        "handoff_id": handoff_id or result["handoff_id"],
        "batch_number": batch_number,
        "manifest_sha256": digest or batch["manifest_sha256"],
        "received_at": received_at or f"2024-01-{batch_number:02d}T00:00:00Z",
    }


def assert_manifest_valid(testcase, batch):
    unsigned = {
        key: value
        for key, value in batch.items()
        if key != "manifest_sha256"
    }
    text = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    testcase.assertEqual(batch["manifest_sha256"], expected)


class HandoffPlanningTest(unittest.TestCase):
    def test_overlapping_seed_closure_delivers_document_once(self):
        documents = ["s1", "s2", "shared", "outside"]
        citations = [cite("s1", "shared"), cite("s2", "shared")]
        data = make_data(documents, citations)
        request = make_request(["s1", "s2"], documents)

        result = prepare_handoff(data, request, [])

        self.assertTrue(result["orderable"])
        self.assertEqual(
            result["plan"]["closure_document_ids"],
            ["s1", "s2", "shared"],
        )
        delivered = [
            document_id
            for batch in result["plan"]["batches"]
            for document_id in batch["document_ids"]
        ]
        self.assertEqual(sorted(delivered), ["s1", "s2", "shared"])
        self.assertEqual(len(delivered), len(set(delivered)))
        self.assertNotIn("outside", delivered)
        self.assertIn("cited_ancestors", result["plan"]["process"])
        json.dumps(result, allow_nan=False)

    def test_chain_is_batched_cited_end_first(self):
        documents = ["a", "b", "c"]
        citations = [cite("a", "b"), cite("b", "c")]
        data = make_data(documents, citations)
        request = make_request(["a"], documents)

        result = prepare_handoff(data, request, [])

        self.assertEqual(
            [batch["document_ids"] for batch in result["plan"]["batches"]],
            [["c"], ["b"], ["a"]],
        )
        for batch in result["plan"]["batches"]:
            self.assertLessEqual(batch["document_count"], 1000)
            self.assertLessEqual(batch["total_bytes"], 1_000_000_000)
            self.assertEqual(
                batch["documents"][0]["payload"]["document_id"],
                batch["document_ids"][0],
            )
            assert_manifest_valid(self, batch)

    def test_cycle_is_atomic_and_outgoing_dependency_precedes_it(self):
        documents = ["a", "b", "c"]
        citations = [
            cite("a", "b", "ab-1"),
            cite("b", "a", "ba-1"),
            cite("a", "c", "ac-1"),
        ]
        data = make_data(documents, citations)
        request = make_request(["a"], documents)

        result = prepare_handoff(data, request, [])

        self.assertEqual(
            [batch["document_ids"] for batch in result["plan"]["batches"]],
            [["c"], ["a", "b"]],
        )
        cycle_batch = result["plan"]["batches"][1]
        self.assertEqual(
            [(x["from"], x["to"], x["page"]) for x in cycle_batch["citations"]],
            [
                ("a", "b", "ab-1"),
                ("a", "c", "ac-1"),
                ("b", "a", "ba-1"),
            ],
        )
        cycle_unit = next(
            unit
            for unit in result["plan"]["atomic_units"]
            if unit["document_ids"] == ["a", "b"]
        )
        self.assertEqual(cycle_unit["dependency_document_ids"], ["c"])

        self.assertEqual(result["next_batch"]["batch_number"], 1)
        acknowledged = prepare_handoff(
            data, request, [ack_for(result, 1)]
        )
        self.assertEqual(acknowledged["next_batch"]["batch_number"], 2)
        self.assertEqual(
            acknowledged["next_batch"]["document_ids"], ["a", "b"]
        )

        with self.assertRaises(ValueError):
            prepare_handoff(data, request, [ack_for(result, 2)])

    def test_oversized_cycle_document_limit_is_unorderable(self):
        documents = ["a", "b", "c"]
        citations = [cite("a", "b"), cite("b", "a")]
        data = make_data(documents, citations)
        request = make_request(
            ["a"], documents, max_documents=1, max_bytes=100
        )

        result = prepare_handoff(data, request, [])

        self.assertFalse(result["orderable"])
        self.assertEqual(result["status"], "unorderable")
        self.assertIsNone(result["plan"])
        violation = result["violations"][0]
        self.assertEqual(violation["document_ids"], ["a", "b"])
        self.assertEqual(violation["document_count"], 2)
        self.assertIn("max_documents_per_batch", violation["triggered_limits"])
        self.assertEqual(result["confirmed_document_ids"], [])
        self.assertIsNone(result["next_batch"])
        json.dumps(result, allow_nan=False)

    def test_oversized_cycle_byte_limit_is_unorderable(self):
        documents = ["a", "b"]
        citations = [cite("a", "b"), cite("b", "a")]
        data = make_data(documents, citations)
        request = make_request(
            ["a"],
            documents,
            sizes={"a": 6, "b": 6},
            max_documents=10,
            max_bytes=10,
        )

        result = prepare_handoff(data, request, [])

        self.assertFalse(result["orderable"])
        violation = result["violations"][0]
        self.assertEqual(violation["total_bytes"], 12)
        self.assertIn("max_bytes_per_batch", violation["triggered_limits"])

    def test_ready_units_skip_nonfitting_unit_under_document_capacity(self):
        documents = ["a", "b1", "b2", "c"]
        citations = [cite("b1", "b2"), cite("b2", "b1")]
        data = make_data(documents, citations)
        request = make_request(
            documents, documents, max_documents=2, max_bytes=1000
        )

        result = prepare_handoff(data, request, [])

        self.assertEqual(
            [batch["document_ids"] for batch in result["plan"]["batches"]],
            [["a", "c"], ["b1", "b2"]],
        )

    def test_ready_units_skip_under_dual_capacity_limits(self):
        documents = ["a", "b1", "b2", "c", "d"]
        citations = [cite("b1", "b2"), cite("b2", "b1")]
        sizes = {"a": 5, "b1": 3, "b2": 3, "c": 4, "d": 1}
        data = make_data(documents, citations)
        request = make_request(
            documents,
            documents,
            sizes=sizes,
            max_documents=3,
            max_bytes=10,
        )

        result = prepare_handoff(data, request, [])

        first = result["plan"]["batches"][0]
        self.assertEqual(first["document_ids"], ["a", "c", "d"])
        self.assertEqual(first["document_count"], 3)
        self.assertEqual(first["total_bytes"], 10)
        self.assertEqual(
            [batch["document_ids"] for batch in result["plan"]["batches"]],
            [["a", "c", "d"], ["b1", "b2"]],
        )
        for batch in result["plan"]["batches"]:
            self.assertLessEqual(batch["document_count"], 3)
            self.assertLessEqual(batch["total_bytes"], 10)

    def test_multipage_citation_locators_are_preserved_with_from_document(self):
        documents = ["a", "b"]
        citations = [cite("a", "b", "page-2"), cite("a", "b", "page-1")]
        data = make_data(documents, list(reversed(citations)))
        request = make_request(["a"], documents)

        result = prepare_handoff(data, request, [])

        self.assertEqual(result["plan"]["batches"][0]["document_ids"], ["b"])
        self.assertEqual(result["plan"]["batches"][0]["citations"], [])
        second = result["plan"]["batches"][1]
        self.assertEqual(
            [c["page"] for c in second["citations"]],
            ["page-1", "page-2"],
        )
        self.assertEqual(len(second["citations"]), 2)

    def test_acknowledgement_prefix_changes_confirmed_and_next_batch(self):
        documents = ["a", "b", "c"]
        citations = [cite("a", "b", "ab"), cite("b", "c", "bc")]
        data = make_data(documents, citations)
        request = make_request(["a"], documents)
        planned = prepare_handoff(data, request, [])
        ack1 = ack_for(planned, 1)
        ack2 = ack_for(planned, 2)
        ack3 = ack_for(planned, 3)

        none_result = prepare_handoff(data, request, [])
        self.assertEqual(none_result["confirmed_batches"], [])
        self.assertEqual(none_result["pending_batches"], [1, 2, 3])
        self.assertEqual(none_result["confirmed_batch_numbers"], [])
        self.assertEqual(none_result["confirmed_document_ids"], [])
        self.assertEqual(none_result["confirmed_citations"], [])
        self.assertEqual(
            none_result["pending_document_ids"], ["a", "b", "c"]
        )
        self.assertEqual(len(none_result["pending_citations"]), 2)
        self.assertEqual(none_result["next_batch"]["batch_number"], 1)

        one_result = prepare_handoff(data, request, [ack1])
        self.assertEqual(one_result["confirmed_batches"], [1])
        self.assertEqual(one_result["pending_batches"], [2, 3])
        self.assertEqual(one_result["confirmed_batch_numbers"], [1])
        self.assertEqual(one_result["confirmed_document_ids"], ["c"])
        self.assertEqual(one_result["confirmed_citations"], [])
        self.assertEqual(
            one_result["pending_document_ids"], ["a", "b"]
        )
        self.assertEqual(one_result["next_batch"]["batch_number"], 2)

        two_result = prepare_handoff(data, request, [ack2, ack1])
        self.assertEqual(two_result["confirmed_batches"], [1, 2])
        self.assertEqual(two_result["pending_batches"], [3])
        self.assertEqual(two_result["confirmed_batch_numbers"], [1, 2])
        self.assertEqual(two_result["confirmed_document_ids"], ["b", "c"])
        self.assertEqual(
            [(c["from"], c["to"]) for c in two_result["confirmed_citations"]],
            [("b", "c")],
        )
        self.assertEqual(two_result["next_batch"]["batch_number"], 3)

        all_result = prepare_handoff(data, request, [ack3, ack2, ack1])
        self.assertEqual(all_result["confirmed_batches"], [1, 2, 3])
        self.assertEqual(all_result["pending_batches"], [])
        self.assertEqual(all_result["confirmed_batch_numbers"], [1, 2, 3])
        self.assertEqual(
            all_result["confirmed_document_ids"], ["a", "b", "c"]
        )
        self.assertEqual(len(all_result["confirmed_citations"]), 2)
        self.assertEqual(all_result["pending_document_ids"], [])
        self.assertEqual(all_result["pending_citations"], [])
        self.assertIsNone(all_result["next_batch"])
        self.assertTrue(all_result["all_confirmed"])

        # Replaying the exact same prefix is stable and does not alter manifest.
        replay = prepare_handoff(data, request, [ack1, ack2])
        self.assertEqual(replay, two_result)

    def test_invalid_acknowledgements_fail_without_mutating_inputs(self):
        documents = ["a", "b", "c"]
        citations = [cite("a", "b"), cite("b", "c")]
        data = make_data(documents, citations)
        request = make_request(["a"], documents)
        planned = prepare_handoff(data, request, [])
        ack1 = ack_for(planned, 1)
        ack2 = ack_for(planned, 2)
        ack3 = ack_for(planned, 3)

        bad_digest = copy.deepcopy(ack1)
        bad_digest["manifest_sha256"] = "0" * 64
        bad_handoff = copy.deepcopy(ack1)
        bad_handoff["handoff_id"] = "other"
        duplicate = copy.deepcopy(ack1)
        out_of_range = {
            "handoff_id": "handoff-1",
            "batch_number": 99,
            "manifest_sha256": "0" * 64,
            "received_at": "2024-99-99",
        }
        cases = [
            [bad_digest],
            [bad_handoff],
            [ack1, ack3],
            [ack2],
            [ack1, duplicate],
            [out_of_range],
        ]
        for acknowledgements in cases:
            data_before = copy.deepcopy(data)
            request_before = copy.deepcopy(request)
            acks_before = copy.deepcopy(acknowledgements)
            with self.assertRaises(ValueError):
                prepare_handoff(data, request, acknowledgements)
            self.assertEqual(data, data_before)
            self.assertEqual(request, request_before)
            self.assertEqual(acknowledgements, acks_before)

    def test_semantic_input_shuffling_produces_identical_result(self):
        documents = ["z", "a", "m", "b", "c"]
        citations = [
            cite("z", "a", "z-a-2"),
            cite("z", "a", "z-a-1"),
            cite("a", "m"),
            cite("m", "b"),
            cite("b", "m"),
            cite("z", "c"),
        ]
        data = make_data(documents, citations)
        request = make_request(["z", "c"], documents)
        base = prepare_handoff(data, request, [])
        ack1 = ack_for(base, 1)
        ack2 = ack_for(base, 2)

        rng = random.Random(210)
        shuffled_data = copy.deepcopy(data)
        rng.shuffle(shuffled_data["documents"])
        rng.shuffle(shuffled_data["citations"])

        shuffled_request = copy.deepcopy(request)
        rng.shuffle(shuffled_request["seed_ids"])
        rng.shuffle(shuffled_request["payloads"])
        shuffled_acks = [ack2, ack1]

        result = prepare_handoff(
            shuffled_data, shuffled_request, shuffled_acks
        )
        expected = prepare_handoff(data, request, [ack1, ack2])
        self.assertEqual(result, expected)
        self.assertEqual(
            [batch["batch_number"] for batch in result["plan"]["batches"]],
            list(range(1, len(result["plan"]["batches"]) + 1)),
        )
        json.dumps(result, allow_nan=False)

    def test_accepts_id_payload_alias_and_validates_coverage(self):
        documents = ["a", "b"]
        data = make_data(documents, [])
        request = make_request(["a"], documents, payload_id_key="id")
        result = prepare_handoff(data, request, [])
        self.assertEqual(
            result["plan"]["batches"][0]["payloads"][0]["document_id"], "a"
        )

        bad_cases = []
        missing = make_request(["a"], documents)
        missing["payloads"].pop()
        bad_cases.append(missing)

        unknown = make_request(["a"], documents)
        unknown["payloads"][0]["document_id"] = "ghost"
        bad_cases.append(unknown)

        duplicate = make_request(["a"], documents)
        duplicate["payloads"][1] = copy.deepcopy(duplicate["payloads"][0])
        bad_cases.append(duplicate)

        bad_hash = make_request(["a"], documents)
        bad_hash["payloads"][0]["sha256"] = "A" * 64
        bad_cases.append(bad_hash)

        bad_size = make_request(["a"], documents)
        bad_size["payloads"][0]["size_bytes"] = 0
        bad_cases.append(bad_size)

        for bad_request in bad_cases:
            with self.assertRaises(ValueError):
                prepare_handoff(data, bad_request, [])

    def test_invalid_seed_and_limits_are_rejected(self):
        data = make_data(["a"], [])
        request = make_request(["missing"], ["a"])
        with self.assertRaises(ValueError):
            prepare_handoff(data, request, [])

        request = make_request(["a", "a"], ["a"])
        with self.assertRaises(ValueError):
            prepare_handoff(data, request, [])

        for field, value in [
            ("max_documents_per_batch", 0),
            ("max_documents_per_batch", 1001),
            ("max_bytes_per_batch", 0),
            ("max_bytes_per_batch", 1_000_000_001),
            ("purpose", ""),
            ("handoff_id", ""),
        ]:
            bad = make_request(["a"], ["a"])
            bad[field] = value
            with self.assertRaises(ValueError):
                prepare_handoff(data, bad, [])

    def test_thousand_document_chain_is_iterative_and_serializable(self):
        n = 1000
        documents = [f"d{i:04d}" for i in range(n)]
        citations = [
            cite(documents[i], documents[i + 1]) for i in range(n - 1)
        ]
        data = make_data(documents, citations)
        request = make_request(
            [documents[0]], documents, max_documents=1000
        )

        result = prepare_handoff(data, request, [])

        self.assertEqual(len(result["plan"]["batches"]), n)
        self.assertEqual(
            result["plan"]["batches"][0]["document_ids"], [documents[-1]]
        )
        self.assertEqual(
            result["plan"]["batches"][-1]["document_ids"], [documents[0]]
        )
        encoded = json.dumps(result, allow_nan=False)
        self.assertIn("manifest_sha256", encoded)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from citation_lineage import consolidate, process


def loc(source_id, local_id):
    return {"source_id": source_id, "local_id": local_id}


def claim(claim_id, left, right, relation="same", evidence=None):
    return {
        "claim_id": claim_id,
        "left": left if isinstance(left, dict) else loc(*left),
        "right": right if isinstance(right, dict) else loc(*right),
        "relation": relation,
        "evidence": evidence if evidence is not None else f"evidence-{claim_id}",
    }


def document(local_id, date="2000-01-01", title="Common title"):
    return {"local_id": local_id, "date": date, "title": title}


def catalog(source_id, documents=None, citations=None, rank=0):
    return {
        "source_id": source_id,
        "authority_rank": rank,
        "documents": list(documents or []),
        "citations": list(citations or []),
    }


def citation(source, target, page="p.1"):
    return {"from": source, "to": target, "page": page}


def dumps(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


class ConsolidationBehaviorTest(unittest.TestCase):
    def test_transitive_same_chain_overridden_by_different_quarantines_all(self):
        catalogs = [
            catalog("s1", [document("A")]),
            catalog("s2", [document("B")]),
            catalog("s3", [document("C")]),
        ]
        claims = [
            claim("c1", ("s1", "A"), ("s2", "B")),
            claim("c2", ("s2", "B"), ("s3", "C")),
            claim("c3", ("s1", "A"), ("s3", "C"), "different", "sealed catalogue"),
        ]

        result = consolidate(catalogs, claims)

        self.assertEqual(result["documents"], [])
        self.assertEqual(result["citations"], [])
        self.assertIsNone(result["process"])
        self.assertEqual(len(result["quarantined_components"]), 1)
        quarantined = result["quarantined_components"][0]
        self.assertEqual(
            quarantined["members"], ["s1:A", "s2:B", "s3:C"]
        )
        self.assertIn("identity", quarantined["conflict_types"])
        self.assertTrue(quarantined["has_identity_conflict"])
        self.assertEqual(
            [item["claim_id"] for item in quarantined["different_claims"]],
            ["c3"],
        )
        self.assertEqual(
            [item["claim_id"] for item in quarantined["same_claims"]],
            ["c1", "c2"],
        )
        # The complete result is JSON safe.
        json.dumps(result, allow_nan=False)

    def test_strongest_rank_field_conflict_and_weaker_variants(self):
        catalogs = [
            catalog("a", [document("X", "2000", "Same title")], rank=0),
            catalog("b", [document("Y", "2001", "Same title")], rank=0),
            catalog("c", [document("Z", "1899", "Weaker title")], rank=2),
        ]
        claims = [
            claim("same-1", ("a", "X"), ("b", "Y")),
            claim("same-2", ("b", "Y"), ("c", "Z")),
        ]

        result = consolidate(catalogs, claims)

        self.assertEqual(len(result["quarantined_components"]), 1)
        component = result["quarantined_components"][0]
        self.assertEqual(component["conflict_fields"], ["date"])
        date_evidence = component["field_evidence"]["date"]
        self.assertEqual(date_evidence["status"], "conflict")
        self.assertEqual(date_evidence["authority_rank"], 0)
        self.assertEqual(date_evidence["distinct_values"], ["2000", "2001"])
        records_by_value = {
            item["value"]: item["records"]
            for item in date_evidence["conflicting_values"]
        }
        self.assertEqual(records_by_value["2000"], ["a:X"])
        self.assertEqual(records_by_value["2001"], ["b:Y"])
        observed_dates = {
            item["id"]: item["value"]
            for item in date_evidence["observations"]
        }
        self.assertEqual(
            observed_dates, {"a:X": "2000", "b:Y": "2001", "c:Z": "1899"}
        )
        # Title agrees at rank 0, so the weaker title variant is evidence only
        # and does not create a second field conflict.
        title_evidence = component["field_evidence"]["title"]
        self.assertEqual(title_evidence["status"], "selected")
        self.assertEqual(title_evidence["selected_value"], "Same title")
        self.assertIn("Weaker title", title_evidence["weaker_distinct_values"])

    def test_agreement_at_strongest_rank_publishes_despite_weaker_conflict(self):
        catalogs = [
            catalog("a", [document("X", "2000", "Title")], rank=0),
            catalog("b", [document("Y", "2000", "Title")], rank=0),
            catalog("c", [document("Z", "1899", "Variant")], rank=5),
        ]
        claims = [claim("same", ("a", "X"), ("c", "Z"))]
        claims.append(claim("same-2", ("a", "X"), ("b", "Y")))

        result = consolidate(catalogs, claims)

        self.assertEqual(result["quarantined_components"], [])
        self.assertEqual(result["documents"], [
            {"id": "a:X", "date": "2000", "title": "Title"}
        ])
        evidence = result["document_evidence"][0]
        self.assertEqual(set(evidence["aliases"]), {"b:Y", "c:Z"})
        self.assertEqual(
            evidence["field_evidence"]["title"]["weaker_distinct_values"],
            ["Variant"],
        )

    def test_claim_change_moves_citation_from_canonical_edge_to_internal(self):
        catalogs = [
            catalog("s1", [document("A"), document("B")], [citation("A", "B")]),
        ]

        separate = consolidate(catalogs, [])
        self.assertEqual(
            separate["citations"],
            [{"from": "s1:A", "to": "s1:B", "page": "p.1"}],
        )
        self.assertEqual(separate["internal_citation_evidence"], [])

        merged = consolidate(
            catalogs, [claim("merge", ("s1", "A"), ("s1", "B"))]
        )
        self.assertEqual(merged["citations"], [])
        self.assertEqual(len(merged["internal_citation_evidence"]), 1)
        internal = merged["internal_citation_evidence"][0]
        self.assertEqual(internal["canonical_id"], "s1:A")
        self.assertEqual(internal["from"], "A")
        self.assertEqual(internal["to"], "B")
        self.assertEqual(merged["process"]["summary"]["documents"], 1)
        self.assertEqual(merged["process"]["summary"]["citations"], 0)

    def test_conflicting_identity_claim_blocks_citation(self):
        catalogs = [
            catalog(
                "s1",
                [document("A"), document("B"), document("C")],
                [citation("A", "B", "p.9")],
            ),
        ]
        claims = [
            claim("ab", ("s1", "A"), ("s1", "B")),
            claim("bc", ("s1", "B"), ("s1", "C")),
            claim("ac-diff", ("s1", "A"), ("s1", "C"), "different"),
        ]

        result = consolidate(catalogs, claims)

        self.assertEqual(result["citations"], [])
        self.assertEqual(result["internal_citation_evidence"], [])
        self.assertEqual(len(result["blocked_citations"]), 1)
        blocked = result["blocked_citations"][0]
        self.assertEqual(blocked["source_id"], "s1")
        self.assertEqual(blocked["blocked_endpoints"], ["from", "to"])
        self.assertIn("identity", blocked["conflict_types"])
        self.assertEqual(
            blocked["endpoint_conflicts"][0]["component_id"], "s1:A"
        )
        self.assertIsNone(result["process"])

    def test_multi_source_locator_aggregates_but_keeps_all_origins(self):
        catalogs = [
            catalog(
                "s1",
                [document("A"), document("Z")],
                [citation("A", "Z", "same-page"), citation("A", "Z", "page-2")],
                rank=0,
            ),
            catalog(
                "s2",
                [document("B"), document("Y")],
                [citation("B", "Y", "same-page")],
                rank=1,
            ),
        ]
        claims = [
            claim("a-b", ("s1", "A"), ("s2", "B")),
            claim("z-y", ("s1", "Z"), ("s2", "Y")),
        ]

        result = consolidate(catalogs, claims)

        self.assertEqual(
            result["citations"],
            [
                {"from": "s1:A", "to": "s1:Z", "page": "page-2"},
                {"from": "s1:A", "to": "s1:Z", "page": "same-page"},
            ],
        )
        evidence_by_page = {
            item["page"]: item for item in result["citation_evidence"]
        }
        same_page = evidence_by_page["same-page"]
        self.assertEqual(same_page["sources"], ["s1", "s2"])
        self.assertEqual(same_page["raw_citation_count"], 2)
        self.assertEqual(
            same_page["source_locations"],
            [
                {"source_id": "s1", "from": "A", "to": "Z", "page": "same-page"},
                {"source_id": "s2", "from": "B", "to": "Y", "page": "same-page"},
            ],
        )
        # Different pages remain different process locators.
        self.assertEqual(len(result["citation_evidence"]), 2)
        self.assertEqual(
            result["process"], process(result["canonical_catalog"])
        )

    def test_component_reports_identity_and_field_conflicts_together(self):
        catalogs = [
            catalog("s1", [document("A", "d1")]),
            catalog("s2", [document("B", "d2")]),
            catalog("s3", [document("C", "d3")]),
        ]
        claims = [
            claim("ab", ("s1", "A"), ("s2", "B")),
            claim("bc", ("s2", "B"), ("s3", "C")),
            claim("diff", ("s1", "A"), ("s3", "C"), "different"),
        ]

        result = consolidate(catalogs, claims)
        component = result["quarantined_components"][0]

        self.assertIn("identity", component["conflict_types"])
        self.assertIn("field:date", component["conflict_types"])
        self.assertNotIn("field:title", component["conflict_types"])
        self.assertEqual(component["conflict_fields"], ["date"])
        self.assertEqual(
            {item["type"] for item in component["conflicts"]},
            {"identity", "field"},
        )
        self.assertTrue(component["has_identity_conflict"])
        self.assertTrue(component["has_field_conflict"])

    def test_all_quarantined_returns_complete_reference_evidence(self):
        catalogs = [
            catalog(
                "s1",
                [document("A", "d1"), document("B", "d1")],
                [citation("A", "B")],
            ),
            catalog(
                "s2",
                [document("C", "d1"), document("D", "d2")],
                [citation("C", "D")],
            ),
        ]
        claims = [
            claim("same-ab", ("s1", "A"), ("s1", "B")),
            claim("diff-ab", ("s1", "A"), ("s1", "B"), "different"),
            claim("same-cd", ("s2", "C"), ("s2", "D")),
        ]

        result = consolidate(catalogs, claims)

        self.assertEqual(len(result["quarantined_components"]), 2)
        self.assertEqual(len(result["blocked_citations"]), 2)
        self.assertEqual(result["internal_citation_evidence"], [])
        self.assertEqual(result["documents"], [])
        self.assertIsNone(result["process"])
        blocked_sources = {
            item["source_id"]: item for item in result["blocked_citations"]
        }
        self.assertEqual(blocked_sources["s1"]["blocked_endpoints"], ["from", "to"])
        self.assertEqual(blocked_sources["s2"]["blocked_endpoints"], ["from", "to"])
        self.assertTrue(all(
            json.dumps(item, allow_nan=False)
            for item in result["quarantined_components"]
        ))

    def test_invalid_claims_and_inputs_raise_without_mutating_input(self):
        valid_catalogs = [
            catalog("s1", [document("A"), document("B")]),
            catalog("s2", [document("C")]),
        ]
        left = ("s1", "A")
        right = ("s2", "C")
        invalid_claims = [
            ("missing source", [claim("x", ("nope", "A"), right)]),
            ("missing document", [claim("x", ("s1", "nope"), right)]),
            ("duplicate claim id", [
                claim("x", left, right),
                claim("x", ("s1", "B"), right),
            ]),
            ("duplicate same orientation", [
                claim("x1", left, right, "same"),
                claim("x2", left, right, "same"),
            ]),
            ("duplicate same reversed", [
                claim("x1", left, right, "same"),
                claim("x2", right, left, "same"),
            ]),
            ("duplicate different", [
                claim("x1", left, right, "different"),
                claim("x2", left, right, "different"),
            ]),
            ("self different", [
                claim("x", left, left, "different"),
            ]),
            ("bad relation", [{
                "claim_id": "x", "left": loc(*left), "right": loc(*right),
                "relation": "maybe", "evidence": "e",
            }]),
            ("empty evidence", [
                claim("x", left, right, evidence=""),
            ]),
            ("unknown claim field", [{
                "claim_id": "x", "left": loc(*left), "right": loc(*right),
                "relation": "same", "evidence": "e", "extra": 1,
            }]),
        ]

        for name, bad_claims in invalid_claims:
            with self.subTest(name=name):
                catalog_copy = copy.deepcopy(valid_catalogs)
                claims_copy = copy.deepcopy(bad_claims)
                with self.assertRaises(ValueError):
                    consolidate(catalog_copy, claims_copy)
                self.assertEqual(catalog_copy, valid_catalogs)
                self.assertEqual(claims_copy, bad_claims)

        invalid_catalogs = [
            [],
            [catalog(f"s{i}") for i in range(51)],
            [{"source_id": "s1", "authority_rank": -1,
              "documents": [], "citations": []}],
            [{"source_id": "s1", "authority_rank": True,
              "documents": [], "citations": []}],
            [{**catalog("s1"), "extra": True}],
            [catalog("s1", [document("A"), document("A")])],
            [catalog("s1", [{"local_id": "A", "date": "", "title": "T"}])],
            [catalog("s1", [document("A")], [citation("A", "missing")])],
            [catalog("s1", [document("A")], [citation("missing", "A")])],
            [catalog("x" * 60, [document("y" * 5)])],
        ]
        for index, bad_catalogs in enumerate(invalid_catalogs):
            with self.subTest(catalog_index=index):
                with self.assertRaises(ValueError):
                    consolidate(copy.deepcopy(bad_catalogs), [])

    def test_tuple_locators_and_integer_claim_ids_are_supported(self):
        catalogs = [catalog("s1", [document("A"), document("B")])]
        claims = [{
            "claim_id": 7,
            "left": ("s1", "A"),
            "right": ["s1", "B"],
            "relation": "same",
            "evidence": "tuple and list locators",
        }]

        result = consolidate(catalogs, claims)

        self.assertEqual(result["documents"][0]["id"], "s1:A")
        self.assertEqual(result["identity_claim_evidence"][0]["claim_id"], 7)

    def test_identical_metadata_without_claim_does_not_merge(self):
        catalogs = [
            catalog("s1", [document("A", "2000", "Same")]),
            catalog("s2", [document("B", "2000", "Same")]),
        ]

        result = consolidate(catalogs, [])

        self.assertEqual(
            sorted(item["id"] for item in result["documents"]),
            ["s1:A", "s2:B"],
        )
        self.assertEqual(len(result["components"]), 2)

    def test_canonical_id_uses_rank_then_source_and_local_tuple_order(self):
        catalogs = [
            catalog("s10", [document("A")], rank=0),
            catalog("s1", [document("Z")], rank=0),
            catalog("s2", [document("M")], rank=1),
        ]
        claims = [
            claim("c1", ("s10", "A"), ("s1", "Z")),
            claim("c2", ("s1", "Z"), ("s2", "M")),
        ]

        result = consolidate(catalogs, claims)

        # Tuple source_id order makes "s1" precede "s10", even though the
        # joined strings compare with ':' differently.
        self.assertEqual(result["documents"][0]["id"], "s1:Z")
        self.assertEqual(result["document_evidence"][0]["aliases"],
                         ["s10:A", "s2:M"])

    def test_count_boundaries_are_enforced(self):
        with self.assertRaises(ValueError):
            consolidate([catalog("s1", [document(f"d{i}") for i in range(5001)])], [])

        many_citations = [citation("A", "A") for _ in range(25001)]
        with self.assertRaises(ValueError):
            consolidate([catalog("s1", [document("A")], many_citations)], [])

        with self.assertRaises(ValueError):
            consolidate([catalog("s1")], [{} for _ in range(10001)])

    def test_caller_input_is_not_mutated_on_success(self):
        catalogs = [
            catalog("s1", [document("A"), document("B")], [citation("A", "B")]),
            catalog("s2", [document("C")]),
        ]
        claims = [claim("c1", ("s1", "A"), ("s2", "C"))]
        catalog_snapshot = copy.deepcopy(catalogs)
        claims_snapshot = copy.deepcopy(claims)

        consolidate(catalogs, claims)

        self.assertEqual(catalogs, catalog_snapshot)
        self.assertEqual(claims, claims_snapshot)

    def test_shuffling_all_semantic_input_orders_is_deterministic(self):
        catalogs = self._rich_catalogs()
        claims = self._rich_claims()
        expected = dumps(consolidate(copy.deepcopy(catalogs), copy.deepcopy(claims)))

        rng = random.Random(210)
        shuffled_catalogs = copy.deepcopy(catalogs)
        rng.shuffle(shuffled_catalogs)
        for source in shuffled_catalogs:
            rng.shuffle(source["documents"])
            rng.shuffle(source["citations"])
        shuffled_claims = copy.deepcopy(claims)
        rng.shuffle(shuffled_claims)

        actual_result = consolidate(shuffled_catalogs, shuffled_claims)
        self.assertEqual(dumps(actual_result), expected)
        self.assertEqual(
            actual_result["process"],
            process(actual_result["canonical_catalog"]),
        )

    def test_business_process_limits_are_explicit_errors(self):
        too_many_docs = [
            catalog("s1", [document(f"d{i:04d}") for i in range(1001)])
        ]
        with self.assertRaises(ValueError) as context:
            consolidate(too_many_docs, [])
        self.assertIn("1000", str(context.exception))

        # 1000 published documents and 5001 distinct canonical locators.
        # Build a compact star-like set with pages supplying distinct locators.
        docs = [document(f"d{i:04d}") for i in range(1000)]
        citations = []
        for i in range(5001):
            citations.append(citation("d0000", f"d{(i % 999) + 1:04d}", f"page-{i}"))
        with self.assertRaises(ValueError) as context:
            consolidate([catalog("s1", docs, citations)], [])
        self.assertIn("5000", str(context.exception))

    def _rich_catalogs(self):
        return [
            catalog(
                "zeta",
                [document("Z1", "2001", "Z title"),
                 document("Z2", "2002", "Z other")],
                [citation("Z1", "Z2", "pA"), citation("Z1", "Z2", "pA")],
                rank=2,
            ),
            catalog(
                "alpha",
                [document("A1", "2000", "A title"),
                 document("A2", "2002", "Best title"),
                 document("A3", "2003", "Conflict")],
                [citation("A1", "A2", "pA"), citation("A2", "A3", "pB")],
                rank=0,
            ),
            catalog(
                "mid",
                [document("M1", "2000", "A title"),
                 document("M2", "2002", "Best title")],
                [citation("M1", "M2", "pA")],
                rank=1,
            ),
        ]

    def _rich_claims(self):
        return [
            claim("z-a1", ("zeta", "Z1"), ("alpha", "A1")),
            claim("a1-m1", ("alpha", "A1"), ("mid", "M1")),
            claim("a2-m2", ("alpha", "A2"), ("mid", "M2")),
            claim("conflict-same", ("alpha", "A3"), ("zeta", "Z2")),
            claim("conflict-diff", ("alpha", "A2"), ("alpha", "A3"), "different"),
        ]


class ConsolidationScaleTest(unittest.TestCase):
    def test_legal_maximum_sparse_graph_is_deterministic_and_lineage_matches(self):
        catalogs = []
        for source_index in range(50):
            source_id = f"s{source_index:02d}"
            documents = [
                document(
                    f"d{doc_index:03d}",
                    f"date-block-{doc_index // 10}",
                    f"Title block {doc_index // 10}",
                )
                for doc_index in range(100)
            ]
            citations = []
            for doc_index in range(100):
                for offset in range(1, 6):
                    target = (doc_index + offset) % 100
                    citations.append(
                        citation(f"d{doc_index:03d}", f"d{target:03d}",
                                 f"p{offset}")
                    )
            self.assertEqual(len(citations), 500)
            catalogs.append(catalog(source_id, documents, citations,
                                    rank=source_index % 7))

        claims = []
        claim_number = 0
        # 500 components of ten local records each, linked sparsely by chains.
        for component in range(500):
            source_index = component // 10
            block = component % 10
            first = block * 10
            source_id = f"s{source_index:02d}"
            for offset in range(9):
                claims.append(claim(
                    f"same-{claim_number:05d}",
                    (source_id, f"d{first + offset:03d}"),
                    (source_id, f"d{first + offset + 1:03d}"),
                ))
                claim_number += 1
        self.assertEqual(claim_number, 4500)

        # Fill to the 10,000 claim maximum with unique cross-component
        # different declarations; these do not create conflicts.
        different_count = 0
        for left_component in range(500):
            for right_component in range(left_component + 1, 500):
                if different_count == 5500:
                    break
                left_source = f"s{left_component // 10:02d}"
                right_source = f"s{right_component // 10:02d}"
                left_local = f"d{(left_component % 10) * 10:03d}"
                right_local = f"d{(right_component % 10) * 10:03d}"
                claims.append(claim(
                    f"diff-{different_count:05d}",
                    (left_source, left_local),
                    (right_source, right_local),
                    "different",
                ))
                different_count += 1
            if different_count == 5500:
                break
        self.assertEqual(len(claims), 10000)

        first = consolidate(catalogs, claims)
        encoded = dumps(first)
        self.assertTrue(encoded)
        self.assertEqual(first["summary"]["published_documents"], 500)
        self.assertLessEqual(len(first["citations"]), 5000)
        self.assertEqual(first["process"], process(first["canonical_catalog"]))

        rng = random.Random(99)
        shuffled_catalogs = copy.deepcopy(catalogs)
        rng.shuffle(shuffled_catalogs)
        for source in shuffled_catalogs:
            rng.shuffle(source["documents"])
            rng.shuffle(source["citations"])
        shuffled_claims = copy.deepcopy(claims)
        rng.shuffle(shuffled_claims)
        second = consolidate(shuffled_catalogs, shuffled_claims)

        self.assertEqual(dumps(second), encoded)
        self.assertEqual(second["process"], first["process"])


if __name__ == "__main__":
    unittest.main()

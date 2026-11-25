"""`plan_preservation` 固化副本修复规划的合成档案数据测试。

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

from citation_lineage import PreservationError, plan_preservation, process  # noqa: E402


def digest(value):
    """合成用 64 位小写十六进制摘要。"""
    return f"{value:064x}"


def document(doc_id):
    return {"id": doc_id, "date": "1900-01-01", "title": f"档案 {doc_id}"}


def citation(source, target, page="p1"):
    return {"from": source, "to": target, "page": page}


def asset(doc_id, number, size_bytes=4096):
    return {"id": doc_id, "sha256": digest(number), "size_bytes": size_bytes}


def site(site_id, domain, status="online", slots=10, cost=5):
    return {
        "site_id": site_id,
        "failure_domain": domain,
        "status": status,
        "write_slots": slots,
        "write_cost": cost,
    }


def replica(replica_id, doc_id, site_id, observed, checked_at="2026-01-01T00:00:00Z"):
    return {
        "replica_id": replica_id,
        "document_id": doc_id,
        "site_id": site_id,
        "observed_sha256": observed,
        "checked_at": checked_at,
    }


def request(seeds, required=3, preservation_id="pres-1",
            reason="重点文献固化"):
    return {
        "preservation_id": preservation_id,
        "seed_ids": list(seeds),
        "required_failure_domains": required,
        "reason": reason,
    }


def data_of(documents, citations):
    return {
        "documents": [document(doc_id) for doc_id in documents],
        "citations": list(citations),
    }


# 四篇文献的稳定登记摘要。
DIGEST = {f"d{i}": digest(i) for i in range(1, 5)}
WRONG = "0" * 64


def build_inputs(sites, replicas, docs=("d1", "d2", "d3", "d4")):
    data = data_of(docs, [citation("d1", "d2"), citation("d2", "d3")])
    assets = [asset(doc_id, i + 1) for i, doc_id in enumerate(docs)]
    return data, assets, sites, replicas


class HealthySourceButCitedEvidenceUnrecoverableTest(unittest.TestCase):
    def test_seed_healthy_but_cited_document_unrecoverable_blocks(self):
        # d1（seed）与 d2 自身有 healthy 副本；被引证据 d3 完全没有 healthy 源。
        sites = [
            site("s1", "A", slots=10),
            site("s2", "B", slots=10),
            site("s3", "C", slots=10),
        ]
        replicas = [
            replica("r0", "d2", "s1", DIGEST["d2"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d3", "s2", WRONG),       # corrupt
            replica("r3", "d3", "s3", None),         # 不可读
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["actions"], [])
        codes = {item["code"] for item in result["violations"]}
        self.assertIn("no_healthy_source", codes)
        violation = next(
            item for item in result["violations"]
            if item["code"] == "no_healthy_source"
        )
        self.assertEqual(violation["document_id"], "d3")
        # 闭包包含 d1、d2、d3（d4 在闭包外但仍有审计摘要）。
        self.assertEqual(result["closure"]["document_ids"], ["d1", "d2", "d3"])
        audit_d3 = next(
            item for item in result["replica_audit"]
            if item["document_id"] == "d3"
        )
        self.assertFalse(audit_d3["healthy"])
        self.assertEqual(audit_d3["status"], "unrecoverable")
        self.assertEqual(
            sorted(entry["status"] for entry in audit_d3["replicas"]),
            ["corrupt", "unavailable"],
        )
        # 闭包外文献 d4 的审计摘要仍然保留。
        self.assertTrue(
            any(item["document_id"] == "d4" for item in result["replica_audit"])
        )


class MultipleHealthyInOneFailureDomainTest(unittest.TestCase):
    def test_healthy_replicas_in_same_domain_count_as_one(self):
        # d1 在故障域 A 的两个站点都 healthy，但只贡献一个故障域。
        sites = [
            site("s1", "A", slots=10),
            site("s2", "A", slots=10),
            site("s3", "B", slots=10),
            site("s4", "C", slots=10),
        ]
        replicas = [
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d1", "s2", DIGEST["d1"]),
            replica("r3", "d2", "s3", DIGEST["d2"]),
            replica("r4", "d3", "s3", DIGEST["d3"]),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "planned")
        plan = next(
            item for item in result["planning"]
            if item["document_id"] == "d1"
        )
        self.assertEqual(plan["before"]["healthy_failure_domains"], ["A"])
        self.assertEqual(plan["before"]["missing_failure_domains"], 2)
        target_domains = {
            action["failure_domain"] for action in result["actions"]
            if action["document_id"] == "d1"
        }
        self.assertEqual(target_domains, {"B", "C"})
        self.assertTrue(plan["after"]["compliant"])


class CorruptAndUnavailableAsReplaceTargetsTest(unittest.TestCase):
    def test_corrupt_and_unavailable_records_are_replace_targets(self):
        sites = [
            site("s1", "A", slots=10),
            site("s2", "B", slots=10),
            site("s3", "C", slots=10),
            site("s4", "D", slots=10),
        ]
        replicas = [
            replica("r0", "d2", "s1", DIGEST["d2"]),
            replica("r00", "d3", "s1", DIGEST["d3"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),     # healthy 源
            replica("r2", "d1", "s2", WRONG),             # corrupt
            replica("r3", "d1", "s3", None),              # unavailable
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=4)
        )
        self.assertEqual(result["status"], "planned")
        actions = {
            action["target_site_id"]: action
            for action in result["actions"]
            if action["document_id"] == "d1"
        }
        self.assertEqual(actions["s2"]["action"], "replace")
        self.assertEqual(actions["s2"]["replaced_replica_id"], "r2")
        self.assertEqual(actions["s3"]["action"], "replace")
        self.assertEqual(actions["s3"]["replaced_replica_id"], "r3")
        # 没有现存记录的 s4 是 create。
        self.assertEqual(actions["s4"]["action"], "create")
        self.assertIsNone(actions["s4"]["replaced_replica_id"])
        # corrupt/unavailable 副本绝不能作为源。
        for action in result["actions"]:
            self.assertEqual(action["source_site_id"], "s1")


class SlotContentionGlobalPlanningTest(unittest.TestCase):
    def test_per_document_greedy_would_fail_but_global_plan_succeeds(self):
        # 两个文献都缺 1 个故障域；低成本站点 s2 只有 1 个 slot，且是 d1 的
        # 唯一可行目标，d2 还可使用稍贵的 s3。逐文献贪心会让 d1 占掉 s2 后
        # d2 无槽可用，全局规划必须让 d1 用 s2、d2 用 s3（或等价可行分配）。
        sites = [
            site("s1", "A", slots=10, cost=1),
            site("s2", "B", slots=1, cost=1),
            site("s3", "C", slots=10, cost=9),
        ]
        replicas = [
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d2", "s1", DIGEST["d2"]),
            # d1 在 s3 已有 healthy 会覆盖 C 域，故令其为 corrupt，保证 s2
            # 是 d1 补 B 域的唯一低成本选择。
            replica("r3", "d1", "s3", WRONG),
        ]
        data, assets, sites, replicas = build_inputs(
            sites, replicas, docs=("d1", "d2")
        )
        data["citations"] = [citation("d1", "d2")]
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=2)
        )
        self.assertEqual(result["status"], "planned")
        for plan in result["planning"]:
            self.assertTrue(plan["after"]["compliant"])
        for summary in result["site_slot_usage"]:
            self.assertGreaterEqual(summary["slots_remaining"], 0)
            self.assertLessEqual(
                summary["slots_used"], summary["write_slots"]
            )

    def test_global_slot_shortage_returns_blocked_without_partial_actions(self):
        # 全部可行目标站点的总 slot 不足以补齐闭包缺口。
        sites = [
            site("s1", "A", slots=10, cost=1),
            site("s2", "B", slots=0, cost=1),   # 零 slot 不可写入
            site("s3", "C", slots=0, cost=1),
        ]
        replicas = [
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d2", "s1", DIGEST["d2"]),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["actions"], [])
        self.assertTrue(
            any(item["code"] == "insufficient_failure_domains"
                for item in result["violations"])
        )


class EqualWriteCostTieBreakTest(unittest.TestCase):
    def test_equal_cost_uses_action_sequence_lexicographic_order(self):
        # 多个站点 write_cost 相同，验证字典序决胜稳定且可复算。
        sites = [
            site("s1", "A", slots=10, cost=5),
            site("s2", "B", slots=10, cost=5),
            site("s3", "C", slots=10, cost=5),
            site("s4", "D", slots=10, cost=5),
        ]
        replicas = [
            replica("r0", "d2", "s1", DIGEST["d2"]),
            replica("r00", "d3", "s1", DIGEST["d3"]),
            replica("r1", "d1", "s1", DIGEST["d1"])]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "planned")
        targets = [
            action["target_site_id"]
            for action in result["actions"]
            if action["document_id"] == "d1"
        ]
        # 同成本下取字典序最小的两个目标站点 s2、s3。
        self.assertEqual(targets, ["s2", "s3"])
        # 再次运行结果完全一致。
        again = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(
            [(a["document_id"], a["target_site_id"], a["source_site_id"],
              a["action"], a["replaced_replica_id"]) for a in result["actions"]],
            [(a["document_id"], a["target_site_id"], a["source_site_id"],
              a["action"], a["replaced_replica_id"]) for a in again["actions"]],
        )


class ExistingHealthyDomainsUseNoSlotsTest(unittest.TestCase):
    def test_already_compliant_document_needs_no_action(self):
        sites = [
            site("s1", "A", slots=10),
            site("s2", "B", slots=10),
            site("s3", "C", slots=10),
        ]
        replicas = [
            replica("r0", "d2", "s1", DIGEST["d2"]),
            replica("r00", "d3", "s1", DIGEST["d3"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d1", "s2", DIGEST["d1"]),
            replica("r3", "d1", "s3", DIGEST["d1"]),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "planned")
        self.assertEqual(
            [a for a in result["actions"] if a["document_id"] == "d1"], []
        )
        plan = next(p for p in result["planning"] if p["document_id"] == "d1")
        self.assertTrue(plan["before"]["compliant"])
        self.assertEqual(plan["planned_actions"], [])
        # d1 不消耗任何 slot；闭包内 d2/d3 的动作不影响该断言的 d1 部分。
        d1_usage = sum(
            1 for action in result["actions"]
            if action["document_id"] == "d1"
        )
        self.assertEqual(d1_usage, 0)


class OfflineSiteAffectsReadAndWriteTest(unittest.TestCase):
    def test_offline_site_is_neither_source_nor_target(self):
        sites = [
            site("s1", "A", status="offline", slots=10),
            site("s2", "B", status="online", slots=10),
            site("s3", "C", status="online", slots=10),
            site("s4", "D", status="online", slots=10),
        ]
        replicas = [
            # 离线站点上的健康摘要也只能判为 unavailable。
            replica("r0", "d2", "s2", DIGEST["d2"]),
            replica("r00", "d3", "s2", DIGEST["d3"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d1", "s2", DIGEST["d1"]),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3)
        )
        self.assertEqual(result["status"], "planned")
        audit = next(a for a in result["replica_audit"]
                     if a["document_id"] == "d1")
        offline_entry = next(e for e in audit["replicas"] if e["site_id"] == "s1")
        self.assertEqual(offline_entry["status"], "unavailable")
        # 任何动作都不能读取或写入离线 s1。
        for action in result["actions"]:
            self.assertNotEqual(action["source_site_id"], "s1")
            self.assertNotEqual(action["target_site_id"], "s1")
        slot_s1 = next(s for s in result["site_slot_usage"]
                       if s["site_id"] == "s1")
        self.assertEqual(slot_s1["slots_available"], 0)
        self.assertEqual(slot_s1["slots_used"], 0)


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.sites = [
            site("s1", "A", slots=10), site("s2", "B", slots=10),
            site("s3", "C", slots=10),
        ]
        self.replicas = [
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d2", "s1", DIGEST["d2"]),
            replica("r3", "d3", "s1", DIGEST["d3"]),
        ]
        self.data, self.assets, self.sites, self.replicas = build_inputs(
            self.sites, self.replicas
        )
        self.request = request(["d1"], required=2)

    def assert_rejected(self, **kwargs):
        with self.assertRaises(ValueError):
            plan_preservation(**kwargs)

    def test_asset_coverage_must_be_exact(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        # 缺一个 asset。
        self.assert_rejected(
            data=self.data, assets=assets[:-1], sites=sites,
            replicas=replicas, request=self.request)
        # 未知文献的 asset。
        bad_assets = assets + [asset("unknown", 99)]
        self.assert_rejected(
            data=self.data, assets=bad_assets, sites=sites,
            replicas=replicas, request=self.request)
        # 重复 asset。
        dup_assets = assets + [assets[0]]
        self.assert_rejected(
            data=self.data, assets=dup_assets, sites=sites,
            replicas=replicas, request=self.request)

    def test_bad_digest_rejected(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        bad_assets = [dict(a) for a in assets]
        bad_assets[0]["sha256"] = "ABCDEF"
        self.assert_rejected(
            data=self.data, assets=bad_assets, sites=sites,
            replicas=replicas, request=self.request)

    def test_duplicate_replica_identity_and_doc_site_rejected(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        # 重复 replica_id。
        dup_id = replicas + [replica("r1", "d2", "s2", DIGEST["d2"])]
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=dup_id, request=self.request)
        # 同一文献同一站点第二条记录。
        dup_pair = replicas + [replica("r9", "d1", "s1", DIGEST["d1"])]
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=dup_pair, request=self.request)

    def test_unknown_document_and_site_rejected(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=replicas + [replica("r9", "ghost", "s1", DIGEST["d1"])],
            request=self.request)
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=replicas + [replica("r9", "d2", "ghost", DIGEST["d2"])],
            request=self.request)

    def test_illegal_fields_and_counts_rejected(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        bad_site = dict(sites[0])
        bad_site["extra"] = 1
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites + [bad_site],
            replicas=replicas, request=self.request)
        bad_request = dict(self.request)
        bad_request["required_failure_domains"] = 11
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=replicas, request=bad_request)
        bad_request2 = dict(self.request)
        bad_request2["seed_ids"] = ["d1", "d1"]
        self.assert_rejected(
            data=self.data, assets=assets, sites=sites,
            replicas=replicas, request=bad_request2)

    def test_inputs_are_not_mutated(self):
        _, assets, sites, replicas = build_inputs(
            self.sites, self.replicas)
        snapshots = copy.deepcopy(
            (self.data, assets, sites, replicas, self.request))
        plan_preservation(
            self.data, assets, sites, replicas, self.request)
        self.assertEqual(
            (self.data, assets, sites, replicas, self.request), snapshots)


class OrderIndependenceTest(unittest.TestCase):
    def test_shuffling_all_input_orders_gives_identical_result(self):
        sites = [
            site("s1", "A", slots=10, cost=3),
            site("s2", "B", slots=10, cost=1),
            site("s3", "C", slots=10, cost=2),
            site("s4", "D", slots=10, cost=1),
        ]
        replicas = [
            replica("r0", "d3", "s1", DIGEST["d3"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r2", "d2", "s1", DIGEST["d2"]),
            replica("r3", "d1", "s2", WRONG),
            replica("r4", "d2", "s3", None),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        req = request(["d1", "d2"], required=3)
        baseline = plan_preservation(
            data, assets, sites, replicas, req)

        rng = random.Random(99)
        for _ in range(8):
            shuffled_sites = sites[:]
            shuffled_replicas = replicas[:]
            shuffled_assets = assets[:]
            shuffled_seeds = req["seed_ids"][:]
            rng.shuffle(shuffled_sites)
            rng.shuffle(shuffled_replicas)
            rng.shuffle(shuffled_assets)
            rng.shuffle(shuffled_seeds)
            shuffled_req = dict(req)
            shuffled_req["seed_ids"] = shuffled_seeds
            result = plan_preservation(
                data, shuffled_assets, shuffled_sites,
                shuffled_replicas, shuffled_req)
            self.assertEqual(
                json.dumps(result, sort_keys=True, allow_nan=False),
                json.dumps(baseline, sort_keys=True, allow_nan=False),
            )


class PlannedInvariantsTest(unittest.TestCase):
    def test_planned_result_satisfies_all_contract_invariants(self):
        sites = [
            site("s1", "A", slots=10, cost=3),
            site("s2", "B", slots=2, cost=1),
            site("s3", "C", slots=10, cost=2),
            site("s4", "D", slots=10, cost=4),
            site("s5", "E", slots=10, cost=5),
        ]
        replicas = [
            replica("r0", "d2", "s1", DIGEST["d2"]),
            replica("r1", "d1", "s1", DIGEST["d1"]),
            replica("r3", "d1", "s2", WRONG),
            replica("r4", "d3", "s4", DIGEST["d3"]),
        ]
        data, assets, sites, replicas = build_inputs(sites, replicas)
        result = plan_preservation(
            data, assets, sites, replicas, request(["d1"], required=3))
        self.assertEqual(result["status"], "planned")
        # 可 JSON 序列化且无 NaN。
        json.dumps(result, allow_nan=False)
        # 动作排序键符合契约。
        keys = [
            (a["document_id"], a["target_site_id"], a["source_site_id"],
             a["action"], a["replaced_replica_id"] or "")
            for a in result["actions"]
        ]
        self.assertEqual(keys, sorted(keys))
        # 每篇保护文献规划后达标，且站点不持有同一文献的多个 healthy 副本。
        for plan in result["planning"]:
            self.assertTrue(plan["after"]["compliant"])
        healthy_pairs = set()
        for view in result["planned_replica_view"]:
            if view["projected_status"] == "healthy":
                pair = (view["document_id"], view["site_id"])
                self.assertNotIn(pair, healthy_pairs)
                healthy_pairs.add(pair)
        # 规划后视图不伪造校验时间：新动作 checked_at 为 None。
        for view in result["planned_replica_view"]:
            if view.get("planned"):
                self.assertIsNone(view["checked_at"])
        # 动作数等于各文献缺口之和。
        expected_actions = sum(
            p["before"]["missing_failure_domains"]
            for p in result["planning"]
        )
        self.assertEqual(result["action_count"], expected_actions)
        # 总 write_cost 与逐动作求和一致。
        self.assertEqual(
            result["total_write_cost"],
            sum(a["write_cost"] for a in result["actions"]),
        )
        # 完整 process 结果被保留。
        self.assertEqual(
            result["process"]["summary"]["documents"],
            len(data["documents"]),
        )


if __name__ == "__main__":
    unittest.main()

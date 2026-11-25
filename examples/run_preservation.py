"""固化副本修复规划合成示例（不访问网络或外部档案服务）。

运行：python -I examples/run_preservation.py

场景：4 篇文献，policy 引用 handbook、handbook 引用 case-note。6 个站点分布在
4 个故障域，其中 s5 离线。seed=policy 的保护闭包为 policy、handbook、case-note。
case-note 在低成本站点上只有损坏/不可读记录，需要 replace；多个文献共享站点
slot，规划在满足每篇 3 个故障域的同时最小化总 write_cost，并按动作键字典序决胜。
"""

import json
import sys

sys.path.insert(0, str(__import__('pathlib').Path(
    __file__).resolve().parents[1] / 'src'))

from citation_lineage import plan_preservation  # noqa: E402


def digest(value):
    return f"{value:064x}"


def document(doc_id):
    return {'id': doc_id, 'date': '1900-01-01', 'title': f'档案 {doc_id}'}


data = {
    'documents': [
        document('policy'),
        document('handbook'),
        document('case-note'),
        document('draft'),
    ],
    'citations': [
        {'from': 'policy', 'to': 'handbook', 'page': 'p1'},
        {'from': 'handbook', 'to': 'case-note', 'page': 'p9'},
    ],
}

_DIGEST = {
    'policy': digest(1),
    'handbook': digest(2),
    'case-note': digest(3),
    'draft': digest(4),
}
WRONG = '0' * 64

assets = [
    {'id': doc_id, 'sha256': _DIGEST[doc_id], 'size_bytes': 4096}
    for doc_id in ('policy', 'handbook', 'case-note', 'draft')
]

sites = [
    {'site_id': 's1', 'failure_domain': 'A', 'status': 'online',
     'write_slots': 10, 'write_cost': 1},
    {'site_id': 's2', 'failure_domain': 'B', 'status': 'online',
     'write_slots': 2, 'write_cost': 2},
    {'site_id': 's3', 'failure_domain': 'C', 'status': 'online',
     'write_slots': 10, 'write_cost': 3},
    {'site_id': 's4', 'failure_domain': 'D', 'status': 'online',
     'write_slots': 10, 'write_cost': 4},
    # 同属故障域 B 的高成本站点，展示同域多站点与成本决胜。
    {'site_id': 's6', 'failure_domain': 'B', 'status': 'online',
     'write_slots': 10, 'write_cost': 9},
    # 离线站点：既不能读也不能写。
    {'site_id': 's5', 'failure_domain': 'A', 'status': 'offline',
     'write_slots': 10, 'write_cost': 1},
]


def replica(replica_id, doc_id, site_id, observed):
    return {
        'replica_id': replica_id,
        'document_id': doc_id,
        'site_id': site_id,
        'observed_sha256': observed,
        'checked_at': '2026-01-15T08:00:00Z',
    }


replicas = [
    # policy 在 A 域有 healthy 源，B 域有一条损坏记录待 replace。
    replica('r1', 'policy', 's1', _DIGEST['policy']),
    replica('r2', 'policy', 's2', WRONG),
    # handbook 仅在 A 域 healthy。
    replica('r3', 'handbook', 's1', _DIGEST['handbook']),
    # case-note 在 D 域有 healthy 源，C 域记录不可读、B 域损坏。
    replica('r4', 'case-note', 's4', _DIGEST['case-note']),
    replica('r5', 'case-note', 's3', None),
    replica('r6', 'case-note', 's2', WRONG),
    # 离线 s5 上的记录一律 unavailable。
    replica('r7', 'draft', 's5', _DIGEST['draft']),
]

request = {
    'preservation_id': 'pres-2026-q1',
    'seed_ids': ['policy'],
    'required_failure_domains': 3,
    'reason': '重点政策文献及其引用证据固化',
}

result = plan_preservation(data, assets, sites, replicas, request)
print(json.dumps({
    'status': result['status'],
    'closure': result['closure']['document_ids'],
    'actions': result['actions'],
    'action_count': result['action_count'],
    'total_write_cost': result['total_write_cost'],
    'compliant_after': [
        (item['document_id'], item['after']['healthy_failure_domains'])
        for item in result['planning']
    ],
    'slot_usage': [
        (item['site_id'], item['slots_used'], item['slots_remaining'])
        for item in result['site_slot_usage']
    ],
    'summary': result['summary'],
}, ensure_ascii=False, indent=2))

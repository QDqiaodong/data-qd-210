"""处置评估合成示例（不访问网络或外部档案服务）。

运行：python -I examples/run_disposition.py

场景：馆藏包含 5 篇文献。`policy` 为永久保留的保管政策，引用 `handbook`；
`handbook` 引用 `case-note`；`case-note` 与 `draft` 自身保管期已过，且都被列为
拟销毁候选。因为引用证据闭包必须保留，`handbook` 与 `case-note` 被阻止处置；
`draft` 过期且无人引用，可以处置，其引用定位随之移除。`case-note` 同时受一条
active hold 保护并有另一条已解除的 hold，展示多类原因并存与 hold 审计证据。
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import assess_disposition  # noqa: E402


def document(doc_id):
    return {'id': doc_id, 'date': '1900-01-01', 'title': f'档案 {doc_id}'}


def citation(source, target, page='p1'):
    return {'from': source, 'to': target, 'page': page}


data = {
    'documents': [
        document('policy'),
        document('handbook'),
        document('case-note'),
        document('draft'),
    ],
    'citations': [
        citation('policy', 'handbook'),
        citation('handbook', 'case-note'),
        citation('draft', 'case-note', 'p7'),
        citation('draft', 'handbook', 'p2'),
    ],
}

records = [
    {
        'id': 'policy',
        'record_class': 'policy',
        'retention_until': None,
        'size_bytes': 4096,
    },
    {
        'id': 'handbook',
        'record_class': 'manual',
        'retention_until': '2020-01-01',
        'size_bytes': 8192,
    },
    {
        'id': 'case-note',
        'record_class': 'case-file',
        'retention_until': '2019-06-30',
        'size_bytes': 2048,
    },
    {
        'id': 'draft',
        'record_class': 'working-paper',
        'retention_until': '2021-12-31',
        'size_bytes': 512,
    },
]

holds = [
    {
        'hold_id': 'hold-litigation',
        'document_ids': ['case-note'],
        'status': 'active',
        'reason': '未结诉讼，保留作为证据',
    },
    {
        'hold_id': 'hold-closed',
        'document_ids': ['case-note'],
        'status': 'released',
        'reason': '已结案，解除保全',
    },
]

request = {
    'disposition_id': 'disp-2024-q3',
    'as_of': '2024-09-30',
    'candidate_ids': ['handbook', 'case-note', 'draft'],
    'reason': '2024 年第三季度到期文献清理',
}

result = assess_disposition(data, records, holds, request)
print(json.dumps({
    'allowed': result['allowed_document_ids'],
    'blocked': result['blocked_document_ids'],
    'total_allowed_bytes': result['total_allowed_bytes'],
    'decisions': [
        {
            'document_id': decision['document_id'],
            'decision': decision['decision'],
            'reasons': decision['reasons'],
            'active_hold_ids': decision['active_hold_ids'],
            'released_hold_ids': decision['released_hold_ids'],
            'witness': (
                decision['citation_evidence']
                and decision['citation_evidence']['locators']
            ),
        }
        for decision in result['decisions']
    ],
    'removed_citations': result['removed_citations'],
    'remaining_documents': [doc['id'] for doc in result['documents']],
    'process_after_summary': (
        result['process_after'] and result['process_after']['summary']
    ),
}, ensure_ascii=False, indent=2))

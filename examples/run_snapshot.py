"""双时间发布快照合成示例（不访问网络或外部档案服务）。

运行：python -I examples/run_snapshot.py

场景：档案 d1 最初登记为可发布；其后机构在更晚的知识时间补录了一条业务上
更早生效的撤回声明（并纠正原声明）。同一业务时点、不同知识截止的两个审计
查询因此得到不同结论：旧快照看不到补录撤回，新快照看到撤回并收敛。
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import materialize  # noqa: E402


def document(doc_id):
    return {'id': doc_id, 'date': '1900-01-01', 'title': f'档案 {doc_id}'}


def citation(source, target, page='p'):
    return {'from': source, 'to': target, 'page': page}


data = {
    'documents': [document('d1'), document('d2')],
    'citations': [citation('d1', 'd2')],
}

assertions = [
    {
        'assertion_id': 'd1-available',
        'kind': 'document',
        'locator': 'd1',
        'state': 'available',
        'valid_from': '2020-01-01T00:00:00Z',
        'valid_to': None,
        'recorded_at': '2020-06-01T00:00:00Z',
        'reason': '初次登记可发布',
        'supersedes': [],
    },
    # 2022 年才记录，但业务上自 2020 年起即撤回，并纠正原 available 声明。
    {
        'assertion_id': 'd1-backdated-withdrawal',
        'kind': 'document',
        'locator': 'd1',
        'state': 'withdrawn',
        'valid_from': '2020-01-01T00:00:00Z',
        'valid_to': None,
        'recorded_at': '2022-01-01T00:00:00Z',
        'reason': '事后补录的早期撤回',
        'supersedes': ['d1-available'],
    },
    {
        'assertion_id': 'd2-available',
        'kind': 'document',
        'locator': 'd2',
        'state': 'available',
        'valid_from': '2020-01-01T00:00:00Z',
        'valid_to': None,
        'recorded_at': '2020-06-01T00:00:00Z',
        'reason': '初次登记可发布',
        'supersedes': [],
    },
    {
        'assertion_id': 'edge-available',
        'kind': 'citation',
        'locator': {'from': 'd1', 'to': 'd2', 'page': 'p'},
        'state': 'available',
        'valid_from': '2020-01-01T00:00:00Z',
        'valid_to': None,
        'recorded_at': '2020-06-01T00:00:00Z',
        'reason': '初次登记可发布',
        'supersedes': [],
    },
]

queries = [
    # 同一业务时点 2021 年中；知识截止分别在补录撤回之前与之后。
    {'query_id': 'audit-2021',
     'valid_at': '2021-06-01T00:00:00Z',
     'known_at': '2021-01-01T00:00:00Z'},
    {'query_id': 'audit-2022',
     'valid_at': '2021-06-01T00:00:00Z',
     'known_at': '2022-06-01T00:00:00Z'},
]

result = materialize(data, assertions, queries)
print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False,
                 sort_keys=True))

for snapshot in result['snapshots']:
    d1 = snapshot['document_states']['d1']
    print(f"{snapshot['query_id']}: d1={d1['status']}, "
          f"published_docs={[d['id'] for d in snapshot['documents']]}, "
          f"blocked={len(snapshot['blocked_citations'])}, "
          f"process_is_null={snapshot['process'] is None}")

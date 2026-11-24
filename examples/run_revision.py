"""修订流水线合成示例（不访问网络或外部档案服务）。

运行：python -I examples/run_revision.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import RevisionError, revise  # noqa: E402


def document(doc_id):
    return {'id': doc_id, 'date': '1900-01-01', 'title': f'档案 {doc_id}'}


def citation(source, target, page):
    return {'from': source, 'to': target, 'page': page}


def change(source, target, page, action):
    return {'from': source, 'to': target, 'page': page, 'action': action}


data = {
    'documents': [document('d1'), document('d2'), document('d3'),
                  document('d4')],
    # 初始谱系：d3 -> d2 -> d1
    'citations': [citation('d2', 'd1', 'p10'),
                  citation('d3', 'd2', 'p4')],
}

batches = [
    # 第一批：补登 d4 -> d1
    {'revision_id': 'rev-1', 'base_revision': None, 'reason': '补登遗漏定位',
     'changes': [change('d4', 'd1', 'p2', 'add')]},
    # 第二批：撤销错误定位并新增 d4 -> d3，形成 d4 -> d3 -> d2 -> d1
    {'revision_id': 'rev-2', 'base_revision': 'rev-1',
     'reason': '更正 d4 的引用方向',
     'changes': [change('d4', 'd1', 'p2', 'remove'),
                 change('d4', 'd3', 'p7', 'add')]},
    # 第三批（故意构造为非法）：撤销不存在的定位
    {'revision_id': 'rev-3', 'base_revision': 'rev-2',
     'reason': '演示原子拒绝',
     'changes': [change('d1', 'd4', 'p9', 'remove')]},
]

try:
    outcome = revise(data, batches)
except RevisionError as exc:
    outcome = exc.result
    print(f'批次 {exc.index} ({exc.revision_id}) 被拒绝：{exc.reason}')

print(json.dumps(
    {
        'status': outcome['status'],
        'head_revision_id': outcome['head_revision_id'],
        'accepted_revision_ids': outcome['accepted_revision_ids'],
        'versions': [
            {'revision_id': version['revision_id'],
             'roots': version['process']['roots'],
             'cycles': version['process']['cycles'],
             'max_depth': version['process']['summary']['max_depth'],
             'document_depths': version['document_depths'],
             'applied': version['applied'],
             'impact': version['impact']}
            for version in outcome['versions']
        ],
        'failure': outcome['failure'],
        'unprocessed_batches': outcome['unprocessed_batches'],
    },
    ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))

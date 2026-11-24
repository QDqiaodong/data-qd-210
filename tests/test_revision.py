"""`revise` 修订流水线的合成档案数据测试。

全部数据为原创合成，不访问任何外部档案服务或网络。运行方式：

    python -I -m unittest discover -s tests -v
"""

import copy
import json
import pathlib
import random
import sys
import unittest

# 允许在未安装（仅 src 布局）时以隔离模式运行测试
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import RevisionError, process, revise  # noqa: E402


def document(doc_id):
    return {'id': doc_id, 'date': '1900-01-01', 'title': f'档案 {doc_id}'}


def citation(source, target, page='p'):
    return {'from': source, 'to': target, 'page': page}


def change(source, target, page, action):
    return {'from': source, 'to': target, 'page': page, 'action': action}


def add(source, target, page='p'):
    return change(source, target, page, 'add')


def remove(source, target, page='p'):
    return change(source, target, page, 'remove')


def batch(revision_id, base_revision, reason, changes):
    return {'revision_id': revision_id, 'base_revision': base_revision,
            'reason': reason, 'changes': changes}


def make_data(ids, citations):
    return {'documents': [document(doc_id) for doc_id in ids],
            'citations': [dict(item) for item in citations]}


def revise_expecting_failure(data, batches):
    """运行必失败的修订流水线，返回异常携带的结构化结果并校验异常身份。"""
    try:
        revise(data, batches)
    except RevisionError as exc:
        assert exc.result is not None
        assert exc.result['failure']['revision_id'] == exc.revision_id
        return exc.result
    raise AssertionError('revision pipeline was expected to reject a batch')


class RevisionBasicsTest(unittest.TestCase):
    """新增/撤销对 roots、cycles、cited_ancestors、max_depth 的联动。"""

    def setUp(self):
        # 初始链 c -> b -> a；d 为孤立文献
        self.data = make_data(
            ['a', 'b', 'c', 'd'],
            [citation('b', 'a'), citation('c', 'b')])

    def test_initial_version_matches_process(self):
        result = revise(self.data, [])
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['initial_revision_id'])
        self.assertIsNone(result['head_revision_id'])
        self.assertEqual(result['accepted_revision_ids'], [])
        self.assertEqual(result['failure'], None)
        self.assertEqual(result['unprocessed_batches'], [])

        initial = result['versions'][0]
        self.assertIsNone(initial['revision_id'])
        self.assertIsNone(initial['base_revision'])
        self.assertIsNone(initial['reason'])
        self.assertEqual(initial['process'], process(self.data))
        self.assertEqual(initial['document_depths'],
                         {'a': 2, 'b': 1, 'c': 0, 'd': 0})
        self.assertEqual(initial['applied'],
                         {'added': [], 'removed': []})
        self.assertEqual(initial['impact'],
                         {'roots': [], 'cycle_members': [],
                          'cited_ancestors': [], 'document_depths': []})

    def test_add_edge_changes_roots_depth_and_ancestry(self):
        batches = [batch('r1', None, '补登 d 对 a 的引用',
                         [add('d', 'a')])]
        result = revise(self.data, batches)
        self.assertEqual(result['status'], 'completed')
        r1 = result['versions'][1]

        self.assertEqual(r1['revision_id'], 'r1')
        self.assertIsNone(r1['base_revision'])
        self.assertEqual(r1['reason'], '补登 d 对 a 的引用')
        self.assertEqual(r1['applied']['added'], [citation('d', 'a')])
        self.assertEqual(r1['applied']['removed'], [])
        # d 原本就是根，新增出边不改变根身份；根集合保持 [c, d]
        self.assertEqual(r1['process']['roots'], ['c', 'd'])
        self.assertEqual(r1['impact']['roots'], [])
        # a 的最长路径仍为 max(c-b-a, d-a) = 2；d 深度仍为 0
        self.assertEqual(r1['process']['summary']['max_depth'], 2)
        self.assertEqual(r1['document_depths'],
                         {'a': 2, 'b': 1, 'c': 0, 'd': 0})
        self.assertEqual(r1['impact']['document_depths'], [])
        self.assertEqual(r1['impact']['cycle_members'], [])
        # d 的可达被引集合从空变为 [a]
        self.assertEqual(r1['process']['cited_ancestors']['d'], ['a'])
        self.assertEqual(r1['impact']['cited_ancestors'], ['d'])

    def test_longer_chain_updates_max_depth_and_depth_impact(self):
        batches = [batch('r1', None, '延长引用链',
                         [add('d', 'c')])]
        r1 = revise(self.data, batches)['versions'][1]
        # d -> c -> b -> a：深度 d=0 c=1 b=2 a=3；c 失去根身份
        self.assertEqual(r1['process']['roots'], ['d'])
        self.assertEqual(r1['process']['summary']['max_depth'], 3)
        self.assertEqual(r1['document_depths'],
                         {'a': 3, 'b': 2, 'c': 1, 'd': 0})
        self.assertEqual(r1['impact']['roots'], ['c'])
        self.assertEqual(r1['impact']['document_depths'], ['a', 'b', 'c'])
        self.assertEqual(r1['impact']['cited_ancestors'], ['d'])

    def test_remove_edge_restores_roots_and_depth(self):
        batches = [
            batch('r1', None, '撤销 c->b', [remove('c', 'b')]),
        ]
        r1 = revise(self.data, batches)['versions'][1]
        # 剩余 b -> a；b 因失去入边成为新根，c 本来无根边变化保持根身份
        self.assertEqual(r1['process']['roots'], ['b', 'c', 'd'])
        self.assertEqual(r1['process']['summary']['max_depth'], 1)
        self.assertEqual(r1['document_depths'],
                         {'a': 1, 'b': 0, 'c': 0, 'd': 0})
        self.assertEqual(r1['impact']['roots'], ['b'])
        self.assertEqual(r1['impact']['document_depths'], ['a', 'b'])
        self.assertEqual(r1['applied']['removed'], [citation('c', 'b')])
        # c 的可达集合被清空
        self.assertEqual(r1['process']['cited_ancestors']['c'], [])
        self.assertEqual(r1['impact']['cited_ancestors'], ['c'])

    def test_remove_last_edge_of_document_leaves_empty_ancestry(self):
        result = revise(make_data(['a', 'b'], [citation('b', 'a')]),
                        [batch('r1', None, '全部撤销',
                               [remove('b', 'a')])])
        r1 = result['versions'][1]
        self.assertEqual(r1['process']['citations'], [])
        self.assertEqual(r1['process']['roots'], ['a', 'b'])
        self.assertEqual(r1['process']['summary']['max_depth'], 0)
        self.assertEqual(r1['document_depths'], {'a': 0, 'b': 0})
        # a 失去入边成为新根（b 原本就是根）
        self.assertEqual(r1['impact']['roots'], ['a'])
        self.assertEqual(r1['impact']['document_depths'], ['a'])

    def test_cycle_creation_and_break_linkage(self):
        # c -> b -> a，新增 a -> c 形成三文献环
        data = make_data(['a', 'b', 'c'],
                         [citation('b', 'a'), citation('c', 'b')])
        result = revise(data, [
            batch('r1', None, '补录形成循环', [add('a', 'c')]),
            batch('r2', 'r1', '撤销错误补录', [remove('a', 'c')]),
        ])
        self.assertEqual(result['head_revision_id'], 'r2')
        initial, r1, r2 = result['versions']

        self.assertEqual(r1['process']['cycles'], [['a', 'b', 'c']])
        self.assertIsNone(r1['process']['summary']['max_depth'])
        self.assertEqual(r1['process']['roots'], [])
        self.assertEqual(r1['impact']['roots'], ['c'])
        self.assertEqual(r1['impact']['cycle_members'], ['a', 'b', 'c'])
        # 环内全部 null
        self.assertEqual(r1['document_depths'],
                         {'a': None, 'b': None, 'c': None})
        self.assertEqual(r1['impact']['document_depths'], ['a', 'b', 'c'])
        # 含环时全局 max_depth 仍为 null（process 契约不变）
        self.assertIsNone(r1['process']['summary']['max_depth'])

        # 破环后完整恢复初始结果，影响集合对称标记
        self.assertEqual(r2['process'], initial['process'])
        self.assertEqual(r2['document_depths'], initial['document_depths'])
        self.assertEqual(r2['impact']['roots'], ['c'])
        self.assertEqual(r2['impact']['cycle_members'], ['a', 'b', 'c'])
        self.assertEqual(r2['impact']['document_depths'], ['a', 'b', 'c'])
        self.assertEqual(r2['process']['summary']['max_depth'], 2)


class CycleBoundaryDepthTest(unittest.TestCase):
    """跨循环边界时 document_depths 与影响集合的精确语义。"""

    def setUp(self):
        # 主链 c -> b -> a；另有环相关测试用的独立链 g -> f -> e；
        # h 为孤立文献。d 用于扮演环下游。
        self.ids = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
        self.base_citations = [
            citation('b', 'a'), citation('c', 'b'),
            citation('f', 'e'), citation('g', 'f'),
        ]
        self.data = make_data(self.ids, self.base_citations)

    def test_upstream_and_disconnected_components_stay_finite(self):
        # 新增 a -> c 使 a,b,c 成环；再新增 a -> d 让 d 处于环下游
        result = revise(self.data, [
            batch('r1', None, '成环并延伸下游',
                  [add('a', 'c'), add('a', 'd')]),
        ])
        r1 = result['versions'][1]
        self.assertEqual(r1['process']['cycles'], [['a', 'b', 'c']])
        depths = r1['document_depths']
        # 环成员及其下游 d 为 null
        self.assertIsNone(depths['a'])
        self.assertIsNone(depths['b'])
        self.assertIsNone(depths['c'])
        self.assertIsNone(depths['d'])
        # 与环不连通的无环链 g -> f -> e 保持有限深度，孤立 h 为 0
        self.assertEqual(depths['e'], 2)
        self.assertEqual(depths['f'], 1)
        self.assertEqual(depths['g'], 0)
        self.assertEqual(depths['h'], 0)
        # 全局 max_depth 仍遵循 process 契约为 null
        self.assertIsNone(r1['process']['summary']['max_depth'])
        self.assertEqual(
            r1['impact']['document_depths'], ['a', 'b', 'c', 'd'])
        self.assertEqual(
            r1['impact']['cycle_members'], ['a', 'b', 'c'])

    def test_breaking_cycle_restores_every_marked_document(self):
        result = revise(self.data, [
            batch('r1', None, '成环并延伸下游',
                  [add('a', 'c'), add('a', 'd')]),
            batch('r2', 'r1', '破环', [remove('a', 'c')]),
        ])
        initial, r1, r2 = result['versions']
        # r2 中 a,b,c,d 都不再被环污染
        self.assertEqual(r2['process']['cycles'], [])
        self.assertEqual(r2['document_depths'], {
            'a': 2, 'b': 1, 'c': 0, 'd': 3,
            'e': 2, 'f': 1, 'g': 0, 'h': 0})
        # 与 r1 相比，恰好是四个被污染过的文献状态翻转
        self.assertEqual(
            r2['impact']['document_depths'], ['a', 'b', 'c', 'd'])
        self.assertEqual(
            r2['impact']['cycle_members'], ['a', 'b', 'c'])
        # 无环部分文献不受任何版本影响
        self.assertNotIn('e', r1['impact']['document_depths'])
        self.assertNotIn('h', r2['impact']['document_depths'])
        # 初始与最终过程结果结构一致（仅边 a->d 为新增，故不相等，
        # 但独立链部分完全一致）
        self.assertEqual(
            r2['process']['cited_ancestors']['g'],
            initial['process']['cited_ancestors']['g'])

    def test_finite_depth_is_longest_path_not_shortest(self):
        # 菱形：r -> x -> t, r -> t；t 的最长深度为 2
        data = make_data(['r', 'x', 't'],
                         [citation('r', 'x'), citation('x', 't'),
                          citation('r', 't')])
        v = revise(data, [])['versions'][0]
        self.assertEqual(v['document_depths'],
                         {'r': 0, 't': 2, 'x': 1})
        self.assertEqual(v['process']['summary']['max_depth'], 2)

    def test_two_node_scc_with_upstream_downstream_and_isolates(self):
        # 环 a<->b；u 在环上游（u->a）；v 在环下游（a->v）；
        # w 孤立；另有与环不连通的链 z->y
        data = make_data(
            ['a', 'b', 'u', 'v', 'w', 'y', 'z'],
            [citation('a', 'b'), citation('b', 'a'),
             citation('u', 'a'), citation('a', 'v'),
             citation('z', 'y')])
        v = revise(data, [])['versions'][0]
        self.assertEqual(v['process']['cycles'], [['a', 'b']])
        self.assertEqual(v['document_depths'], {
            'a': None, 'b': None, 'u': 0, 'v': None,
            'w': 0, 'y': 1, 'z': 0})
        # 环上游 u 与不连通部分 w/z/y 都不能被全局 null 化
        self.assertIsNone(v['process']['summary']['max_depth'])

    def test_node_reachable_from_cycle_and_from_finite_root_is_tainted(self):
        # q 同时有有限路径 x->q 与环可达路径 a->q；只要可从环到达即为 null
        data = make_data(['a', 'b', 'x', 'q'],
                         [citation('a', 'b'), citation('b', 'a'),
                          citation('x', 'q'), citation('a', 'q')])
        v = revise(data, [])['versions'][0]
        self.assertEqual(v['document_depths'],
                         {'a': None, 'b': None, 'q': None, 'x': 0})


class AtomicBatchTest(unittest.TestCase):
    """先检查后原子提交；第二步冲突不污染第一步。"""

    def setUp(self):
        self.data = make_data(
            ['a', 'b', 'c'], [citation('b', 'a')])

    def test_invalid_batch_changes_nothing_and_reports_identity(self):
        original = process(self.data)
        snapshot = copy.deepcopy(self.data)
        # 同批：一个合法新增 + 一个对不存在定位的撤销
        bad = batch('r-bad', None, '混合有效与无效变更',
                    [add('c', 'a'), remove('c', 'b')])
        with self.assertRaises(RevisionError) as caught:
            revise(self.data, [bad])
        self.assertEqual(caught.exception.revision_id, 'r-bad')
        self.assertEqual(caught.exception.index, 0)
        self.assertIn('does not exist', caught.exception.reason)

        result = caught.exception.result
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['head_revision_id'])
        self.assertEqual(len(result['versions']), 1)
        self.assertEqual(result['versions'][0]['process'], original)
        self.assertEqual(result['failure']['revision_id'], 'r-bad')
        self.assertEqual(result['failure']['index'], 0)
        self.assertEqual(result['failed_batch'], result['failure'])
        # 失败后无后续批次
        self.assertEqual(result['unprocessed_batches'], [])
        # 输入对象不被修改
        self.assertEqual(self.data, snapshot)

    def test_second_batch_failure_does_not_pollute_first(self):
        good = batch('r1', None, '合法新增', [add('c', 'a')])
        bad = batch('r2', 'r1', '重复新增', [add('c', 'a')])
        later = batch('r3', 'r2', '不应被处理', [add('b', 'c')])
        with self.assertRaises(RevisionError):
            revise(self.data, [good, bad, later])
        result = revise_expecting_failure(
            self.data, [good, bad, later])

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['accepted_revision_ids'], ['r1'])
        self.assertEqual(result['head_revision_id'], 'r1')
        # 第一步完整保留
        r1 = result['versions'][1]
        self.assertEqual(r1['applied']['added'], [citation('c', 'a')])
        self.assertEqual(r1['process'], process(
            make_data(['a', 'b', 'c'],
                      [citation('b', 'a'), citation('c', 'a')])))
        # 失败批次与未处理批次状态明确
        self.assertEqual(result['failure']['revision_id'], 'r2')
        self.assertEqual(result['failure']['index'], 1)
        self.assertIn('already exists', result['failure']['reason'])
        self.assertEqual(result['unprocessed_batches'],
                         [{'index': 2, 'revision_id': 'r3'}])

    def test_all_checks_run_against_batch_start_state(self):
        # 合法混合批次：撤销开始状态存在的 b->a，新增开始状态不存在的 b->c；
        # 整体原子提交。
        good = batch('r-good', None, '同批撤销与新增不同定位',
                     [remove('b', 'a'), add('b', 'c')])
        result = revise(self.data, [good])
        self.assertEqual(result['status'], 'completed')
        r1 = result['versions'][1]
        self.assertEqual(r1['applied']['removed'], [citation('b', 'a')])
        self.assertEqual(r1['applied']['added'], [citation('b', 'c')])
        self.assertEqual(r1['process']['citations'], [citation('b', 'c')])

        # 语义校验必须基于批次开始状态，而不是顺序逐条应用：
        # 同批新增一个开始状态已存在的定位（另一条变更合法），整批拒绝。
        bad_add = batch('r-bad1', None, '混入对已存在定位的新增',
                        [add('b', 'c'), add('b', 'a')])
        result = revise_expecting_failure(self.data, [bad_add])
        self.assertIn('already exists', result['failure']['reason'])
        self.assertEqual(len(result['versions']), 1)

        # 同批撤销一个开始状态不存在的定位（另一条撤销合法），整批拒绝。
        bad_remove = batch('r-bad2', None, '混入对不存在定位的撤销',
                           [remove('b', 'a'), remove('c', 'a')])
        result = revise_expecting_failure(self.data, [bad_remove])
        self.assertIn('does not exist', result['failure']['reason'])
        self.assertEqual(len(result['versions']), 1)

    def test_same_locator_add_and_remove_in_one_batch_rejected(self):
        # 即使动作互补，同一三元组在一批内也只能出现一次
        bad = batch('r-bad', None, '先新增再撤销同一新定位',
                    [add('c', 'a'), remove('c', 'a')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('more than once', result['failure']['reason'])
        self.assertEqual(len(result['versions']), 1)

    def test_duplicate_locator_within_batch_rejected(self):
        for changes in (
                [add('c', 'a'), add('c', 'a')],
                [add('c', 'a'), remove('c', 'a')]):
            with self.subTest(changes=changes):
                bad = batch('r-dup', None, '批次内重复定位', changes)
                result = revise_expecting_failure(self.data, [bad])
                self.assertIn('more than once',
                              result['failure']['reason'])


class ReplayAndConflictTest(unittest.TestCase):
    """同 ID 重放与身份冲突。"""

    def setUp(self):
        self.data = make_data(['a', 'b'], [citation('b', 'a')])
        self.r1 = batch('r1', None, '新增', [add('b', 'a', 'p2')])

    def test_identical_replay_returns_original_version(self):
        replay = copy.deepcopy(self.r1)
        result = revise(self.data, [self.r1, replay])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['accepted_revision_ids'], ['r1'])
        self.assertEqual(result['head_revision_id'], 'r1')
        self.assertEqual(len(result['versions']), 2)
        only = result['versions'][1]
        self.assertEqual(only['applied']['added'],
                         [citation('b', 'a', 'p2')])
        # 重放不重复应用：定位只存在一条
        self.assertEqual(
            only['process']['citations'],
            sorted([citation('b', 'a'), citation('b', 'a', 'p2')],
                   key=lambda c: (c['from'], c['to'], c['page'])))

    def test_replay_then_next_revision_chains_from_real_head(self):
        r2 = batch('r2', 'r1', '继续修订', [add('a', 'b')])
        result = revise(self.data, [self.r1, copy.deepcopy(self.r1), r2])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['accepted_revision_ids'], ['r1', 'r2'])
        self.assertEqual(result['head_revision_id'], 'r2')

    def test_same_id_different_changes_is_identity_conflict(self):
        # 同一提交内：预扫描在任何应用前判定身份冲突，不接受任何批次
        evil = batch('r1', None, '新增', [add('a', 'b')])
        result = revise_expecting_failure(self.data, [self.r1, evil])
        self.assertEqual(result['failure']['index'], 1)
        self.assertIn('identity conflict', result['failure']['reason'])
        self.assertEqual(result['accepted_revision_ids'], [])

    def test_accepted_then_conflicting_replay_in_one_submission(self):
        # 同一次调用内先合法接受 r1，再用冲突内容重放 r1：
        # 预扫描在任何应用前判定身份冲突，不接受任何批次、不使版本倒退
        evil_same_call = batch('r1', None, '新增', [add('a', 'b')])
        result = revise_expecting_failure(
            self.data, [self.r1, evil_same_call])
        self.assertIn('identity conflict', result['failure']['reason'])
        self.assertEqual(result['accepted_revision_ids'], [])
        self.assertEqual(len(result['versions']), 1)

    def test_replay_is_scoped_to_one_submission(self):
        # 每次 revise 调用从同一初始 data 独立开始；不同调用之间无共享状态，
        # 同一批次在两个独立调用中分别被接受是合法的
        first = revise(self.data, [self.r1])
        second = revise(copy.deepcopy(self.data),
                        [copy.deepcopy(self.r1)])
        self.assertEqual(first['accepted_revision_ids'], ['r1'])
        self.assertEqual(second['accepted_revision_ids'], ['r1'])
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_same_id_different_reason_or_base_is_conflict(self):
        different_reason = batch('r1', None, '不同原因',
                                 [add('b', 'a', 'p2')])
        different_base = batch('r1', 'r-other', '新增',
                               [add('b', 'a', 'p2')])
        for evil in (different_reason, different_base):
            with self.subTest(evil=evil):
                result = revise_expecting_failure(self.data, [self.r1, evil])
                self.assertIn('identity conflict',
                              result['failure']['reason'])

    def test_replay_must_not_bypass_ordering(self):
        # r1 重放后，后续批次仍须指向真实的紧邻已接受版本 r1；
        # 这里构造跳过 r1 直接以 null 为父的冲突批次
        orphan = batch('r2', None, '错误父版本', [add('a', 'b')])
        result = revise_expecting_failure(
            self.data, [self.r1, copy.deepcopy(self.r1), orphan])
        self.assertIn('base_revision', result['failure']['reason'])
        self.assertEqual(result['failure']['revision_id'], 'r2')

    def test_duplicate_new_id_within_one_submission_rejected(self):
        # 两个从未接受过的同 ID 批次不能伪装成重放；内容不同即身份冲突
        first = batch('rx', None, '首次', [add('a', 'b')])
        second = batch('rx', 'rx', '重复身份', [add('b', 'a', 'p2')])
        result = revise_expecting_failure(self.data, [first, second])
        self.assertEqual(result['failure']['index'], 1)
        self.assertIn('identity conflict',
                      result['failure']['reason'])
        # 预扫描先于应用：没有任何批次被接受
        self.assertEqual(len(result['versions']), 1)
        self.assertEqual(result['accepted_revision_ids'], [])


class OrderingAndStructureTest(unittest.TestCase):
    """父版本顺序、文献集合封闭性、输入契约复用。"""

    def setUp(self):
        self.data = make_data(['a', 'b', 'c'], [citation('b', 'a')])

    def test_first_batch_requires_null_base(self):
        bad = batch('r1', 'ghost', '首批父版本非 null', [add('c', 'a')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('immediately preceding accepted revision',
                      result['failure']['reason'])

    def test_wrong_parent_revision_rejected(self):
        good = batch('r1', None, '一', [add('c', 'a')])
        bad = batch('r2', 'r0', '错误父版本', [add('b', 'c')])
        result = revise_expecting_failure(self.data, [good, bad])
        self.assertEqual(result['failure']['revision_id'], 'r2')
        self.assertIn('r1', result['failure']['message'])
        self.assertEqual(result['unprocessed_batches'], [])

    def test_remove_nonexistent_locator_rejected(self):
        bad = batch('r1', None, '撤销不存在定位', [remove('c', 'a')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('does not exist', result['failure']['reason'])

    def test_add_existing_locator_rejected(self):
        bad = batch('r1', None, '新增已存在定位', [add('b', 'a')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('already exists', result['failure']['reason'])

    def test_change_referencing_unknown_document_rejected(self):
        bad = batch('r1', None, '引用集合外文献', [add('c', 'ghost')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('outside the fixed initial document collection',
                      result['failure']['reason'])
        self.assertEqual(len(result['versions']), 1)

    def test_self_citation_change_rejected(self):
        bad = batch('r1', None, '自引变更', [add('a', 'a')])
        result = revise_expecting_failure(self.data, [bad])
        self.assertIn('self-citation', result['failure']['reason'])

    def test_unknown_action_and_bad_shape_rejected(self):
        bad_action = batch('r1', None, '坏动作',
                           [change('c', 'a', 'p', 'delete')])
        result = revise_expecting_failure(self.data, [bad_action])
        self.assertIn('action must be one of', result['failure']['reason'])

        malformed = {'revision_id': 'r9'}
        result2 = revise_expecting_failure(self.data, [malformed])
        self.assertIn('batch fields', result2['failure']['reason'])
        self.assertEqual(result2['failure']['revision_id'], 'r9')

    def test_contract_value_errors_normalized_with_batch_identity(self):
        # 空 changes、非字符串 id、坏身份、缺字段都必须归为 RevisionError，
        # 且结果中保留失败前已接受版本（此处仅初始版本）。
        cases = [
            [{'revision_id': 'r1', 'base_revision': None, 'reason': 'x',
              'changes': []}],
            [{'revision_id': 123, 'base_revision': None, 'reason': 'x',
              'changes': [add('a', 'b')]}],
            [{'revision_id': 'r1', 'base_revision': None, 'reason': 'x',
              'changes': [change('a/bad', 'b', 'p', 'add')]}],
            [['not', 'a', 'dict']],
        ]
        for batches in cases:
            with self.subTest(batches=batches):
                result = revise_expecting_failure(self.data, batches)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(len(result['versions']), 1)
                self.assertEqual(result['failure']['index'], 0)

    def test_batches_must_be_a_list(self):
        with self.assertRaises(RevisionError):
            revise(self.data, {'revision_id': 'r1'})

    def test_process_contract_still_enforced_on_initial_data(self):
        broken = make_data(['a', 'b'], [citation('a', 'a')])
        with self.assertRaises(ValueError):
            revise(broken, [])

    def test_input_objects_are_not_mutated(self):
        data = make_data(['a', 'b'], [citation('b', 'a')])
        data_before = copy.deepcopy(data)
        batches = [batch('r1', None, '新增', [add('a', 'b')]),
                   batch('r2', 'r1', '撤销', [remove('b', 'a')])]
        batches_before = copy.deepcopy(batches)
        revise(data, batches)
        self.assertEqual(data, data_before)
        self.assertEqual(batches, batches_before)


class DeterminismTest(unittest.TestCase):
    """打乱无语义顺序不得改变确定性输出。"""

    def _build(self):
        data = make_data(
            ['a', 'b', 'c', 'd', 'e'],
            [citation('b', 'a', 'p1'), citation('c', 'b', 'p2'),
             citation('d', 'a', 'p3')])
        batches = [
            batch('r1', None, '第一批',
                  [add('e', 'd'), add('c', 'a', 'p9')]),
            batch('r2', 'r1', '第二批',
                  [remove('d', 'a', 'p3'), add('e', 'c')]),
            batch('r3', 'r2', '第三批',
                  [remove('c', 'b', 'p2'), add('b', 'd')]),
        ]
        return data, batches

    def test_shuffled_documents_citations_and_changes_are_identical(self):
        data, batches = self._build()
        baseline = revise(copy.deepcopy(data), copy.deepcopy(batches))

        rng = random.Random(20261124)
        shuffled_data = copy.deepcopy(data)
        rng.shuffle(shuffled_data['documents'])
        rng.shuffle(shuffled_data['citations'])
        shuffled_batches = copy.deepcopy(batches)
        for item in shuffled_batches:
            rng.shuffle(item['changes'])
        rng.shuffle(shuffled_batches)  # 批次顺序本身有语义，重排后恢复
        shuffled_batches = sorted(shuffled_batches,
                                  key=lambda b: b['revision_id'])

        rerun = revise(shuffled_data, shuffled_batches)
        self.assertEqual(
            json.dumps(rerun, sort_keys=True, ensure_ascii=False),
            json.dumps(baseline, sort_keys=True, ensure_ascii=False))

    def test_replay_recognized_despite_change_order_shuffle(self):
        data, batches = self._build()
        replay = copy.deepcopy(batches[0])
        rng = random.Random(7)
        rng.shuffle(replay['changes'])
        result = revise(data, [batches[0], replay])
        self.assertEqual(result['accepted_revision_ids'], ['r1'])
        self.assertEqual(len(result['versions']), 2)

    def test_impact_sets_are_sorted(self):
        data, batches = self._build()
        result = revise(data, batches)
        for version in result['versions']:
            for values in version['impact'].values():
                self.assertEqual(values, sorted(values))
        self.assertEqual(result['accepted_revision_ids'],
                         ['r1', 'r2', 'r3'])
        # 全部批次接受后结果可 JSON 序列化（含 null）
        encoded = json.dumps(result, ensure_ascii=False,
                             sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded['status'], 'completed')

    def test_each_version_process_result_independent(self):
        data, batches = self._build()
        result = revise(data, batches)
        # 每个版本的 process 都是该版本定位集合的独立完整计算
        for version in result['versions']:
            expected = process({
                'documents': data['documents'],
                'citations': version['process']['citations'],
            })
            self.assertEqual(version['process'], expected)
        # 版本之间不共享可变容器
        first = result['versions'][1]['process']['citations']
        first.append({'from': 'zzz', 'to': 'zzz', 'page': 'p'})
        self.assertNotIn(
            {'from': 'zzz', 'to': 'zzz', 'page': 'p'},
            result['versions'][2]['process']['citations'])

    def test_long_chain_revisions_use_iterative_traversal(self):
        # 接近契约上限的长链上连续修订，不能触发 Python 递归上限，
        # 且每个版本都完整重算 process 与逐文献深度
        size = 900
        ids = [f'd{i}' for i in range(size)]
        data = make_data(
            ids, [citation(f'd{i}', f'd{i + 1}') for i in range(size - 1)])
        batches = [
            batch('r1', None, '新增回边成环',
                  [add(f'd{size - 1}', 'd0')]),
            batch('r2', 'r1', '撤销回边破环',
                  [remove(f'd{size - 1}', 'd0')]),
        ]
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(200)
        try:
            result = revise(data, batches)
        finally:
            sys.setrecursionlimit(old_limit)
        initial, r1, r2 = result['versions']
        self.assertEqual(initial['process']['summary']['max_depth'],
                         size - 1)
        self.assertEqual(len(r1['process']['cycles']), 1)
        self.assertIsNone(r1['process']['summary']['max_depth'])
        self.assertTrue(all(value is None
                            for value in r1['document_depths'].values()))
        self.assertEqual(r2['process']['summary']['max_depth'], size - 1)
        self.assertEqual(r2['document_depths'], initial['document_depths'])


if __name__ == '__main__':
    unittest.main()

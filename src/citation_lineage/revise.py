"""档案引用修订流水线。

在已发布的引用关系之上按顺序接受修订批次，每个批次原子生效，并为每个已
接受版本保留完整、可复核的谱系结果（直接交给 :func:`citation_lineage.service.process`
计算），同时记录逐文献深度与相对前一版本的影响集合。

设计约束见 ``docs/revision.md``。本模块只读取调用方传入的 ``data`` 与 ``batches``，
不会修改这些输入对象，也不会让失败批次污染此前已接受的版本。
"""

from collections import deque

from .checks import array, fields, ident
from .service import process, text

# 动作字面量；新增 / 撤销
_ACTION_ADD = 'add'
_ACTION_REMOVE = 'remove'
_ACTIONS = (_ACTION_ADD, _ACTION_REMOVE)

# 初始版本的 revision_id / 父版本 / 原因均为空
_INITIAL = None


class RevisionError(ValueError):
    """修订批次被拒绝。

    携带批次身份（``revision_id`` 与批次下标 ``index``）、可定位的业务原因
    ``reason``，以及截至失败时的完整结构化结果 ``result``（含此前已接受版本、
    失败批次与未处理批次状态），便于档案整理人员复核。
    """

    def __init__(self, reason, revision_id=_INITIAL, index=None, result=None):
        self.reason = reason
        self.revision_id = revision_id
        self.index = index
        self.result = result
        location = []
        if index is not None:
            location.append(f'batch index {index}')
        if revision_id is not None:
            location.append(f'revision_id={revision_id!r}')
        prefix = '[' + ', '.join(location) + '] ' if location else ''
        super().__init__(prefix + reason)


def _locator(change, index, revision_id):
    """校验并返回一条 change 的标准化定位三元组与动作。"""
    fields(change, ('from', 'to', 'page', 'action'))
    action = change['action']
    if action not in _ACTIONS:
        raise RevisionError(
            f"change action must be one of {list(_ACTIONS)}, got {action!r}",
            revision_id, index)
    source = ident(change['from'])
    target = ident(change['to'])
    page = text(change['page'])
    return action, (source, target, page)


def _validate_batch_shape(batch, index):
    """校验批次外壳，返回 (revision_id, base_revision, reason, 原始 changes)。"""
    if not isinstance(batch, dict):
        raise RevisionError('revision batch must be an object', index=index)
    required = ('revision_id', 'base_revision', 'reason', 'changes')
    if set(batch) != set(required):
        raise RevisionError(
            f'batch fields must be exactly {list(required)}',
            batch.get('revision_id') if isinstance(batch, dict) else _INITIAL,
            index)
    revision_id = text(batch['revision_id'])
    reason = text(batch['reason'])
    base_revision = batch['base_revision']
    if base_revision is not None:
        base_revision = text(base_revision)
    changes = array(batch['changes'], 1, 5000)
    return revision_id, base_revision, reason, changes


def _normalize_changes(changes, revision_id, index, doc_ids):
    """结构校验所有 change，返回按定位排序的 (action, locator) 列表。

    仅做结构性校验（字段、身份、文献集合封闭性、批次内重复定位）；新增/撤销
    的存在性语义在 :func:`revise` 中基于批次开始时的状态统一检查。
    """
    normalized = []
    seen = set()
    duplicated = None
    for change in changes:
        action, locator = _locator(change, index, revision_id)
        source, target, _page = locator
        if source not in doc_ids or target not in doc_ids:
            raise RevisionError(
                f'change locator {list(locator)} references a document outside '
                'the fixed initial document collection',
                revision_id, index)
        if source == target:
            raise RevisionError(
                f'change locator {list(locator)} is a self-citation, which is '
                'not allowed by the citation contract',
                revision_id, index)
        if locator in seen and duplicated is None:
            duplicated = locator
        seen.add(locator)
        normalized.append((action, locator))
    if duplicated is not None:
        raise RevisionError(
            f'locator {list(duplicated)} is operated on more than once within '
            'the same batch; each locator may appear at most once per batch',
            revision_id, index)
    # 排序后输出，保证打乱批次内变更顺序不影响任何确定性结果
    return sorted(normalized, key=lambda item: item[1])


def _document_depths(result, doc_ids):
    """计算逐文献深度。

    无环版本：沿引用方向从任一根文献到该文献的最长路径边数，根与孤点为 0。
    含环版本：大小大于 1 的强连通分量成员，以及沿引用方向从任一环成员可达
    的文献记为 ``null``；其余文献在剔除这些环分量后的无环子图上按根到文献
    最长路径取值。因此环上游、与环不连通的无环部分仍可取有限值。
    """
    adjacency = {doc_id: set() for doc_id in doc_ids}
    for citation in result['citations']:
        adjacency[citation['from']].add(citation['to'])

    tainted = set()
    for component in result['cycles']:
        tainted.update(component)
    pending = list(tainted)
    while pending:  # 环成员沿出边可达的全部文献
        node = pending.pop()
        for child in adjacency[node]:
            if child not in tainted:
                tainted.add(child)
                pending.append(child)

    # 在有限（非 null）文献构成的子图上做最长路径；该子图必然无环，
    # 因为任何大小 > 1 的强连通分量都已整体进入 tainted。
    finite = [doc_id for doc_id in doc_ids if doc_id not in tainted]
    sub_adjacency = {doc_id: set() for doc_id in finite}
    indegree = {doc_id: 0 for doc_id in finite}
    for node in finite:
        for child in adjacency[node]:
            if child not in tainted:
                sub_adjacency[node].add(child)
                indegree[child] += 1

    depth = {doc_id: 0 for doc_id in finite}
    queue = deque(doc_id for doc_id in sorted(finite) if indegree[doc_id] == 0)
    while queue:
        node = queue.popleft()
        for child in sorted(sub_adjacency[node]):
            depth[child] = max(depth[child], depth[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    return {doc_id: None if doc_id in tainted else depth[doc_id]
            for doc_id in sorted(doc_ids)}


def _empty_impact():
    return {'roots': [], 'cycle_members': [],
            'cited_ancestors': [], 'document_depths': []}


def _cycle_members(result):
    members = set()
    for component in result['cycles']:
        members.update(component)
    return members


def _impact_between(previous, current):
    """逐项比较两个相邻版本，返回按文献 ID 排序的四类影响集合。"""
    previous_roots = set(previous['process']['roots'])
    current_roots = set(current['process']['roots'])
    previous_cycles = _cycle_members(previous['process'])
    current_cycles = _cycle_members(current['process'])

    previous_ancestry = previous['process']['cited_ancestors']
    current_ancestry = current['process']['cited_ancestors']
    ancestry_changed = sorted(
        doc_id for doc_id in current_ancestry
        if previous_ancestry.get(doc_id) != current_ancestry[doc_id])

    previous_depths = previous['document_depths']
    current_depths = current['document_depths']
    depths_changed = sorted(
        doc_id for doc_id in current_depths
        # 整数与 null 之间的变化也必须逐项捕获
        if previous_depths.get(doc_id) != current_depths[doc_id])

    return {
        'roots': sorted(previous_roots ^ current_roots),
        'cycle_members': sorted(previous_cycles ^ current_cycles),
        'cited_ancestors': ancestry_changed,
        'document_depths': depths_changed,
    }


def _snapshot(documents, edges):
    """由当前定位集合构造交给 process 的完整输入（全新对象）。"""
    return {
        'documents': [dict(document) for document in documents],
        'citations': [{'from': source, 'to': target, 'page': page}
                      for source, target, page in sorted(edges)],
    }


def _version(revision_id, base_revision, reason, result, depths,
             added, removed, impact):
    return {
        'revision_id': revision_id,
        'base_revision': base_revision,
        'reason': reason,
        'process': result,
        'document_depths': depths,
        'applied': {
            'added': [{'from': s, 'to': t, 'page': p}
                      for s, t, p in sorted(added)],
            'removed': [{'from': s, 'to': t, 'page': p}
                        for s, t, p in sorted(removed)],
        },
        'impact': impact,
    }


def _batch_payload_signature(batch):
    """静态提取批次归一化签名；结构非法时返回 None（交由主循环报错）。"""
    if not isinstance(batch, dict) or \
            set(batch) != {'revision_id', 'base_revision', 'reason',
                           'changes'}:
        return None
    if not isinstance(batch['revision_id'], str) or \
            not isinstance(batch['reason'], str) or \
            not isinstance(batch['changes'], list):
        return None
    base_revision = batch['base_revision']
    if base_revision is not None and not isinstance(base_revision, str):
        return None
    locators = []
    for item in batch['changes']:
        if not isinstance(item, dict) or \
                set(item) != {'from', 'to', 'page', 'action'}:
            return None
        if not all(isinstance(item[key], str)
                   for key in ('from', 'to', 'page', 'action')):
            return None
        locators.append((item['action'],
                         (item['from'], item['to'], item['page'])))
    return (base_revision, batch['reason'], tuple(sorted(locators,
                                                          key=lambda x: x[1])))


def _same_batch_payload(first, second):
    """两个同 ID 批次是否为完全相同的重放（容忍变更书写顺序差异）。"""
    first_signature = _batch_payload_signature(first)
    second_signature = _batch_payload_signature(second)
    if first_signature is None or second_signature is None:
        return False
    return first_signature == second_signature


def _envelope(versions, head, failure, unprocessed):
    """构造统一的可 JSON 序列化顶层结果。"""
    return {
        'status': 'failed' if failure is not None else 'completed',
        'initial_revision_id': _INITIAL,
        'head_revision_id': head,
        'accepted_revision_ids': [v['revision_id'] for v in versions[1:]],
        'versions': versions,
        'failure': failure,
        'failed_batch': failure,
        'unprocessed_batches': unprocessed,
    }


def revise(data, batches):
    """按顺序提交引用修订批次，返回包含完整版本谱系的可 JSON 序列化结果。

    :param data: 与 :func:`citation_lineage.process` 相同的初始输入契约；
        文献集合在整个修订过程中固定为其中声明的文献。
    :param batches: 按顺序提交的修订批次列表，每批含 ``revision_id``、
        ``base_revision``、``reason``、``changes``。
    :return: 顶层结果字典，含初始版本与每个已接受修订版本；全部批次接受时
        ``status`` 为 ``"completed"``。
    :raises RevisionError: 任一批次无效时抛出。异常携带批次身份与可定位业务
        原因，其 ``result`` 属性给出与成功路径同构的结构化结果：保留失败前
        已接受版本，并在 ``failure`` / ``failed_batch`` 与
        ``unprocessed_batches`` 中标记失败批次与未处理批次；绝不会部分应用。
    """
    if not isinstance(batches, list):
        raise RevisionError('batches must be a list')

    # 初始版本：直接由现有 process 计算，输入契约与限制完全沿用。
    initial_result = process(data)
    doc_ids = sorted(initial_result['documents'])
    initial_depths = _document_depths(initial_result, doc_ids)
    initial_version = _version(
        _INITIAL, _INITIAL, _INITIAL, initial_result, initial_depths,
        added=set(), removed=set(), impact=_empty_impact())

    # 预扫描：同一提交中重复出现的 revision_id 必须能静态判定为重放
    # （与首次出现的批次完全同内容）；否则在任何应用之前直接拒绝，
    # 保证两个"新"同 ID 批次不会被伪装成重放，也不产生部分修订。
    first_occurrence = {}
    for pre_index, pre_batch in enumerate(batches):
        if not isinstance(pre_batch, dict) or \
                not isinstance(pre_batch.get('revision_id'), str):
            continue
        pre_id = pre_batch['revision_id']
        if pre_id not in first_occurrence:
            first_occurrence[pre_id] = (pre_index, pre_batch)
            continue
        first_index, first_batch = first_occurrence[pre_id]
        if not _same_batch_payload(first_batch, pre_batch):
            exc = RevisionError(
                f'revision_id {pre_id!r} appears more than once in the '
                f'submission (batch indexes {first_index} and {pre_index}) '
                'with different base_revision, reason or change set; '
                'identity conflict',
                pre_id, pre_index)
            exc.result = _envelope(
                [initial_version], _INITIAL,
                {'index': pre_index, 'revision_id': pre_id,
                 'reason': exc.reason, 'message': str(exc)},
                [{'index': later,
                  'revision_id': later_batch.get('revision_id')
                  if isinstance(later_batch, dict) else _INITIAL}
                 for later, later_batch in enumerate(
                     batches[pre_index + 1:], start=pre_index + 1)])
            raise exc

    versions = [initial_version]
    # 已接受的 revision_id 集合；重放判定（内容是否完全相同）由预扫描完成
    accepted = set()
    head = _INITIAL
    current_edges = {
        (c['from'], c['to'], c['page']) for c in initial_result['citations']
    }

    for index, batch in enumerate(batches):
        try:
            try:
                revision_id, base_revision, reason, changes = \
                    _validate_batch_shape(batch, index)
                normalized = _normalize_changes(
                    changes, revision_id, index, set(doc_ids))
            except RevisionError:
                raise
            except ValueError as exc:
                # 复用 process 契约校验（fields/array/ident/text）抛出的
                # ValueError，统一归一化为带批次身份的 RevisionError。
                known_id = batch.get('revision_id') if isinstance(batch, dict) \
                    else _INITIAL
                if not isinstance(known_id, str):
                    known_id = _INITIAL
                raise RevisionError(str(exc), known_id, index)

            if revision_id in accepted:
                # 预扫描已保证同 ID 重复只能是完全同内容的重放；
                # 走到这里即确认重放：返回原版本、原影响，不推进版本、
                # 不重复应用变更，也不改变 head。
                continue

            expected_base = head
            if base_revision != expected_base:
                raise RevisionError(
                    f'base_revision {base_revision!r} does not point at the '
                    f'immediately preceding accepted revision '
                    f'{expected_base!r}',
                    revision_id, index)

            # 先基于批次开始时的状态检查全部变更，再统一提交。
            # 按定位排序检查，保证错误信息在输入顺序被打乱时仍然稳定。
            for action, locator in normalized:
                exists = locator in current_edges
                if action == _ACTION_ADD and exists:
                    raise RevisionError(
                        f'add targets locator {list(locator)} that already '
                        'exists in the current revision',
                        revision_id, index)
                if action == _ACTION_REMOVE and not exists:
                    raise RevisionError(
                        f'remove targets locator {list(locator)} that does not '
                        'exist in the current revision',
                        revision_id, index)

            # 全部有效：原子提交到全新的定位集合，输入对象与既有版本不变。
            next_edges = set(current_edges)
            added = set()
            removed = set()
            for action, locator in normalized:
                if action == _ACTION_ADD:
                    next_edges.add(locator)
                    added.add(locator)
                else:
                    next_edges.remove(locator)
                    removed.add(locator)

            result = process(_snapshot(data['documents'], next_edges))
            depths = _document_depths(result, doc_ids)
            candidate = _version(
                revision_id, base_revision, reason, result, depths,
                added, removed, impact=_empty_impact())
            candidate['impact'] = _impact_between(versions[-1], candidate)

            versions.append(candidate)
            accepted.add(revision_id)
            current_edges = next_edges
            head = revision_id
        except RevisionError as exc:
            failure = {
                'index': index,
                'revision_id': exc.revision_id,
                'reason': exc.reason,
                'message': str(exc),
            }
            unprocessed = [
                {'index': later,
                 'revision_id': later_batch.get('revision_id')
                 if isinstance(later_batch, dict) else _INITIAL}
                for later, later_batch in enumerate(batches[index + 1:],
                                                    start=index + 1)
            ]
            exc.result = _envelope(versions, head, failure, unprocessed)
            raise

    return _envelope(versions, head, _INITIAL, [])

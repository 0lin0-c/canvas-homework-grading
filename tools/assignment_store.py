"""Keep each assignment's inputs and review results separate."""
from pathlib import Path

from canvas_roster import assignment_ids
from qwen_grader import GradingError, read_json, save_json


def batch_path(root, url, kind):
    course, assignment = assignment_ids(url)
    if kind not in ('source', 'review'):
        raise ValueError('Unknown batch kind')
    return Path(root) / 'local' / 'assignments' / f'{course}-{assignment}' / f'batch-{kind}.json'


def load_batch(root, url, kind):
    path = batch_path(root, url, kind)
    if not path.exists():
        legacy = Path(root) / 'local' / f'batch-{kind}.json'
        if legacy.exists():
            data = read_json(legacy)
            if data.get('assignment_url', '').rstrip('/') == url.rstrip('/'):
                save_json(path, data)
    if not path.exists():
        return None
    data = read_json(path)
    if data.get('assignment_url', '').rstrip('/') != url.rstrip('/'):
        raise GradingError('保存的作业与所选日期不一致。')
    return data


def assignment_catalog(client, root):
    path = Path(root) / 'local' / 'assignment-catalog.json'
    warning = None
    try:
        assignments = client.read('/api/v1/courses/94139/assignments', {'per_page': 100})
        entries = [{'title': item['name'],
                    'assignment_url': f'https://oc.sjtu.edu.cn/courses/94139/assignments/{item["id"]}',
                    'due_at': item.get('due_at')} for item in assignments]
        save_json(path, entries)
    except GradingError as exc:
        if not path.exists():
            raise
        entries = read_json(path)
        warning = str(exc) + ' 当前显示已缓存的作业列表。'
    for item in entries:
        item['has_result'] = load_batch(root, item['assignment_url'], 'review') is not None
        item['configured'] = load_batch(root, item['assignment_url'], 'source') is not None
    return {'assignments': entries, 'warning': warning}

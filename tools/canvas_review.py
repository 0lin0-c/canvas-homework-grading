"""Submit one human-reviewed grade/comment, with conflict checks and readback."""
import hashlib
import json
import math
from pathlib import Path

from canvas_batch import fingerprint
from canvas_roster import assignment_ids
from qwen_grader import GradingError, read_json, save_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def target(batch, body):
    if batch.get('mode') != 'real' or body.get('assignment_url') != batch.get('assignment_url'):
        raise GradingError('只能提交当前真实作业的人工复核结果。')
    student = next((s for s in batch['students'] if s['id'] == body.get('student_id')), None)
    if (not student or not student['id'].isdecimal() or not student.get('source_fingerprint')
            or body.get('review_revision') != (student.get('review_revision') or batch.get('review_revision'))):
        raise GradingError('复核版本已变化，请刷新页面重新核对。')
    return student


def current(client, batch, student):
    course, assignment_id = assignment_ids(batch['assignment_url'])
    assignment = client.read(f'/api/v1/courses/{course}/assignments/{assignment_id}')
    if (str(assignment.get('id')) != assignment_id or str(assignment.get('course_id')) != course
            or assignment.get('points_possible') != 100 or assignment.get('grading_type') != 'points'
            or assignment.get('group_category_id') or assignment.get('moderated_grading')):
        raise GradingError('当前工作台仅支持满分 100 的普通个人计分作业。')
    submission = client.read(f'/api/v1/courses/{course}/assignments/{assignment_id}/submissions/{student["id"]}',
                             {'include[]': 'submission_comments'})
    if (str(submission.get('assignment_id')) != assignment_id
            or str(submission.get('user_id')) != student['id'] or submission.get('excused')
            or submission.get('assignment_visible') is False
            or fingerprint(submission) != student['source_fingerprint']):
        raise GradingError('学生答卷版本或免交状态已变化，请重新采集和复核；未覆盖当前成绩。')
    if not isinstance(submission.get('submission_comments'), list):
        raise GradingError('无法完整读取 Canvas 评论，暂不能提交。')
    return submission


def snapshot(submission):
    return digest({k: submission.get(k) for k in
                   ('score', 'grade', 'graded_at', 'grader_id', 'attempt', 'submitted_at', 'submission_comments')})


def verified(record, submission):
    score = submission.get('score')
    if not isinstance(score, (int, float)) or not math.isclose(score, record['score'], abs_tol=1e-6):
        return False
    if not record['comment']:
        return True
    for comment in submission['submission_comments']:
        if (str(comment.get('author_id')) == record['author_id']
                and comment.get('comment') == record['comment'] and not comment.get('draft')
                and (str(comment.get('id')) == record.get('comment_id')
                     or str(comment.get('id')) not in record['before_comment_ids'])):
            record['comment_id'] = str(comment['id'])
            return True
    return False


def review_state(client, batch, body, ledger_dir=None):
    student = target(batch, body)
    submission = current(client, batch, student)
    receipt = None
    if ledger_dir is not None:
        course, assignment_id = assignment_ids(batch['assignment_url'])
        path = Path(ledger_dir) / f'{course}-{assignment_id}-{student["id"]}.json'
        if path.exists():
            record = read_json(path)
            if record.get('source_fingerprint') == student['source_fingerprint'] and verified(record, submission):
                receipt = {'score': record['score'], 'comment': record['comment'], 'comment_id': record.get('comment_id')}
    return {'snapshot': snapshot(submission), 'score': submission.get('score'),
            'comment_count': len(submission['submission_comments']), 'receipt': receipt}


def submit_review(client, batch, body, ledger_dir):
    student = target(batch, body)
    try:
        if isinstance(body.get('score'), bool) or not str(body.get('score', '')).strip():
            raise ValueError()
        score = float(body['score'])
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise GradingError('评分必须是 0 到 100 的数字。') from None
    comment = body.get('comment')
    if not isinstance(comment, str) or len(comment) > 10000:
        raise GradingError('评语必须是文本且不超过 10000 字。')
    comment = comment.replace('\r\n', '\n').strip()
    course, assignment_id = assignment_ids(batch['assignment_url'])
    record_path = Path(ledger_dir) / f'{course}-{assignment_id}-{student["id"]}.json'
    submission = current(client, batch, student)
    desired = digest([student['source_fingerprint'], score, comment])
    record = read_json(record_path) if record_path.exists() else None
    if record and record['desired'] == desired:
        if verified(record, submission):
            record['state'] = 'verified'
            save_json(record_path, record)
            return {'state': 'submitted', 'score': score, 'comment_id': record.get('comment_id'),
                    'snapshot': snapshot(submission), 'reused': True}
        raise GradingError('此前提交状态未能与 Canvas 核对一致；为避免重复评论，请先到 Canvas 核对。')
    if record and record['state'] != 'verified':
        raise GradingError('该学生上一条提交结果未明，请先用原内容核验，勿重复发送。')
    if body.get('snapshot') != snapshot(submission):
        raise GradingError('Canvas 成绩或评论已变化，请重新读取并核对后提交。')
    author = client.read('/api/v1/users/self/profile')
    if not author.get('id'):
        raise GradingError('无法确认当前 Canvas 用户，未提交。')
    # Check again after resolving the author, immediately before the write.
    submission = current(client, batch, student)
    if body.get('snapshot') != snapshot(submission):
        raise GradingError('Canvas 成绩或评论已变化，请重新读取并核对后提交。')
    record = {'state': 'sent_or_unknown', 'desired': desired, 'student_id': student['id'],
              'source_fingerprint': student['source_fingerprint'], 'score': score, 'comment': comment,
              'author_id': str(author['id']),
              'before_comment_ids': [str(c['id']) for c in submission['submission_comments']]}
    save_json(record_path, record)
    send_error = None
    try:
        client.submit_review(course, assignment_id, student['id'], score, comment, submission['attempt'])
    except GradingError as exc:
        send_error = str(exc)
    try:
        received = current(client, batch, student)
    except GradingError:
        raise GradingError('提交后的 Canvas 回读失败；草稿已保留，再点提交只核验本次结果，不会重复发评论。') from None
    if not verified(record, received):
        record['error'] = send_error or 'Canvas 回读的成绩或评论与提交内容不一致。'
        save_json(record_path, record)
        raise GradingError(record['error'] + ' 提交状态待核验，禁止自动重发评论。')
    record['state'] = 'verified'
    save_json(record_path, record)
    return {'state': 'submitted', 'score': score, 'comment_id': record.get('comment_id'),
            'snapshot': snapshot(received), 'reused': False}

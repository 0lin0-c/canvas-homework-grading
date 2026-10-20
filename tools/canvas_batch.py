"""Prepare every current Canvas upload as local page images before grading."""

import copy
import hashlib
import io
import json
import re
import subprocess
import uuid
from pathlib import Path
from urllib import error, parse, request

from PIL import Image, ImageOps

from canvas_roster import CanvasClient, assignment_ids, read_roster
from qwen_grader import GradingError, NoRedirect, local_image, save_json, validate_batch


MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
MIME_EXTENSIONS = {'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/png': '.png'}


def current_submissions(client, assignment_url):
    course_id, assignment_id = assignment_ids(assignment_url)
    items = client.read(f'/api/v1/courses/{course_id}/students/submissions',
                        {'student_ids[]': 'all', 'assignment_ids[]': assignment_id,
                         'include[]': 'user', 'enrollment_state': 'active', 'per_page': 100})
    if not isinstance(items, list):
        raise GradingError('Canvas 未返回完整的答卷清单。')
    submitted = {}
    for item in items:
        if str(item.get('assignment_id')) != assignment_id:
            raise GradingError('Canvas 答卷清单包含其他作业。')
        if item.get('submitted_at') and not item.get('excused') and item.get('assignment_visible') is not False:
            student_id = str(item.get('user_id', ''))
            if not student_id or student_id in submitted:
                raise GradingError('Canvas 答卷学生 ID 缺失或重复。')
            submitted[student_id] = item
    return submitted


def fingerprint(submission):
    attachments = submission.get('attachments') or []
    if submission.get('submission_type') != 'online_upload' or not attachments:
        raise GradingError('有已交答卷并非包含附件的在线上传，不能跳过该学生。')
    files = []
    for item in attachments:
        file_id, mime, size = str(item.get('id', '')), item.get('content-type'), item.get('size')
        if not file_id.isdigit() or mime not in MIME_EXTENSIONS or type(size) is not int or not 0 < size <= MAX_ATTACHMENT_BYTES:
            raise GradingError('Canvas 附件类型、大小或 ID 不受支持，尚不能批改全部答卷。')
        files.append((file_id, mime, size, item.get('updated_at')))
    if len({item[0] for item in files}) != len(files):
        raise GradingError('Canvas 同一答卷包含重复的附件 ID。')
    version = [submission.get('submitted_at'), submission.get('attempt'), files,
               submission.get('late'), submission.get('seconds_late')]
    return hashlib.sha256(json.dumps(version, ensure_ascii=False).encode('utf-8')).hexdigest()


def download_attachment(client, attachment):
    file_id = str(attachment['id'])
    url = attachment.get('url', '')
    parsed = parse.urlparse(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'oc.sjtu.edu.cn'
            or parsed.path != f'/files/{file_id}/download' or parsed.fragment):
        raise GradingError('Canvas 附件下载地址不符合预期。')
    opener = request.build_opener(NoRedirect())
    canvas_request = request.Request(url, headers={'Authorization': 'Bearer ' + client.token})
    try:
        with opener.open(canvas_request, timeout=45) as response:
            payload = response.read(attachment['size'] + 1)
    except error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise GradingError(f'Canvas 附件下载返回 HTTP {exc.code}。') from None
        signed_url = exc.headers.get('Location', '')
        exc.close()
        target = parse.urlparse(signed_url)
        if (target.scheme != 'https' or target.netloc != 's3.jcloud.sjtu.edu.cn'
                or not target.path or target.fragment):
            raise GradingError('Canvas 附件跳转到了未允许的文件服务。')
        try:
            with opener.open(request.Request(signed_url), timeout=90) as response:
                payload = response.read(attachment['size'] + 1)
        except (error.HTTPError, error.URLError, TimeoutError):
            raise GradingError('Canvas 附件文件服务下载失败。') from None
    except (error.URLError, TimeoutError):
        raise GradingError('Canvas 附件下载连接失败。') from None
    if len(payload) != attachment['size']:
        raise GradingError('Canvas 附件大小与元数据不一致，不能用于批改。')
    return payload


def render_attachment(payload, mime, source, prefix):
    source.write_bytes(payload)
    if mime == 'application/pdf':
        if not payload.startswith(b'%PDF-'):
            raise GradingError('Canvas PDF 附件内容无效。')
        try:
            info = subprocess.run(['pdfinfo', str(source)], capture_output=True,
                                  timeout=30, check=True)
            match = re.search(rb'^Pages:\s+(\d+)\s*$', info.stdout, re.MULTILINE)
            count = int(match.group(1)) if match else 0
            if not 0 < count <= 40:
                raise GradingError('PDF 页数异常，尚不能批改。')
            subprocess.run(['pdftoppm', '-r', '150', '-png', str(source), str(prefix)],
                           capture_output=True, timeout=240, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            raise GradingError('PDF 附件无法完整渲染。') from None
        images = sorted(prefix.parent.glob(prefix.name + '-*.png'),
                        key=lambda path: int(path.stem.rsplit('-', 1)[-1]))
        if len(images) != count or [int(path.stem.rsplit('-', 1)[-1]) for path in images] != list(range(1, count + 1)):
            raise GradingError('PDF 页面渲染不完整。')
        return images
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format not in {'JPEG', 'PNG', 'MPO'}:
                raise GradingError('Canvas 图片附件的实际格式不受支持。')
            images = []
            for number in range(getattr(image, 'n_frames', 1)):
                image.seek(number)
                destination = prefix.parent / f'{prefix.name}-{number + 1}.png'
                ImageOps.exif_transpose(image).convert('RGB').save(destination)
                images.append(destination)
    except (OSError, ValueError):
        raise GradingError('图片附件无法完整读取。') from None
    return images


def reusable(student, version, root):
    if not student or student.get('source_fingerprint') != version:
        return False
    attachments, pages = student.get('attachments', []), student.get('pages', [])
    if not attachments or len(pages) != sum(item.get('page_count', 0) for item in attachments):
        return False
    try:
        for page in pages:
            local_image(root, page['path'])
    except GradingError:
        return False
    return True


def prepare_batch(client, assignment_url, root, source, downloader=download_attachment,
                  renderer=render_attachment, output_path=None):
    """Fetch changed uploads, verify full page coverage, then atomically publish the input manifest."""
    root = Path(root)
    if source.get('assignment_url') != assignment_url:
        raise GradingError('这份作业尚未配置对应的原题和官方答案，不能批改。')
    roster = read_roster(client, assignment_url)
    current = current_submissions(client, assignment_url)
    if set(current) != {item['id'] for item in roster['submitted']}:
        raise GradingError('读取期间 Canvas 提交名单发生变化，请重试。')
    old = {item['id']: item for item in source.get('students', [])}
    folder = root / 'local' / 'canvas-batches' / uuid.uuid4().hex
    students = []
    for person in roster['submitted']:
        submission = current[person['id']]
        version = fingerprint(submission)
        if reusable(old.get(person['id']), version, root):
            student = copy.deepcopy(old[person['id']])
            student['name'] = person['name']
            student.update(late=submission.get('late'), seconds_late=submission.get('seconds_late'))
            students.append(student)
            continue
        attachments, pages = [], []
        for item in submission['attachments']:
            file_id = str(item['id'])
            prefix = folder / f's{person["id"]}-a{file_id}'
            prefix.parent.mkdir(parents=True, exist_ok=True)
            source_file = prefix.with_suffix(MIME_EXTENSIONS[item['content-type']])
            images = renderer(downloader(client, item), item['content-type'], source_file, prefix)
            attachments.append({'id': file_id, 'name': item.get('display_name') or item.get('filename') or file_id,
                                'page_count': len(images), 'pages_read': list(range(1, len(images) + 1))})
            for number, image in enumerate(images, 1):
                pages.append({'id': f'a{file_id}-p{number}', 'attachment_id': file_id,
                              'page': number, 'path': image.relative_to(root).as_posix()})
        students.append({'id': person['id'], 'name': person['name'],
                         'submitted_at': submission['submitted_at'], 'source_fingerprint': version,
                         'late': submission.get('late'), 'seconds_late': submission.get('seconds_late'),
                         'attachments': attachments, 'pages': pages})
    batch = {'title': roster['title'], 'assignment_url': assignment_url,
             'question_numbers': copy.deepcopy(source['question_numbers']),
             'reference_materials': copy.deepcopy(source['reference_materials']), 'students': students}
    if 'reference_match' in source:
        batch['reference_match'] = copy.deepcopy(source['reference_match'])
    validate_batch(batch, root)
    latest = current_submissions(client, assignment_url)
    if set(latest) != set(current) or any(fingerprint(latest[id]) != fingerprint(item) for id, item in current.items()):
        raise GradingError('读取期间有学生更新答卷，请重新点击批量修改作业。')
    save_json(output_path or root / 'local' / 'batch-source.json', batch)
    summary = {'submitted_count': len(students), 'attachment_count': sum(len(s['attachments']) for s in students),
               'page_count': sum(len(s['pages']) for s in students),
               'unsubmitted_count': len(roster['unsubmitted'])}
    if 'reference_match' in batch:
        summary['reference_match'] = batch['reference_match']
        summary['total_count'] = roster['total_count']
    return batch, summary


def verify_current_batch(client, batch):
    current = current_submissions(client, batch['assignment_url'])
    prepared = {student['id']: student for student in batch['students']}
    if set(current) != set(prepared) or any(
            fingerprint(submission) != prepared[id].get('source_fingerprint')
            for id, submission in current.items()):
        raise GradingError('Canvas 答卷在采集后发生变化，请重新点击批量修改作业。')

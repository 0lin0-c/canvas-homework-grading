"""Match Canvas exercise lists to verified local textbook and official-answer pages."""
import copy
import hashlib
import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from canvas_roster import assignment_ids
from qwen_grader import GradingError, local_image, read_json, save_json


class DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ('br', 'p', 'div', 'li'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'li'):
            self.parts.append(' ')


def chapter_number(value):
    if value.isdecimal():
        return str(int(value))
    digits = {c: i for i, c in enumerate('零一二三四五六七八九')}
    if value == '十':
        return '10'
    if '十' in value:
        tens, ones = value.split('十')
        return str((digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0))
    return str(digits[value])


def parse_exercises(description):
    parser = DescriptionText()
    parser.feed(description or '')
    text = unicodedata.normalize('NFKC', ''.join(parser.parts)).strip().rstrip('。.;；')
    numeral = r'(?:[1-9][0-9]?|[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])'
    match = re.fullmatch(rf'(?:课本|教材)?\s*(?:习题\s*({numeral})|第\s*({numeral})\s*章(?:\s*习题)?)(?:\s*[:：]\s*|\s+|(?=第))(.+)', text, re.S)
    if not match:
        raise GradingError('无法明确识别作业章节和题号。请在 Canvas 作业说明中写明，例如“习题一：21、23、25、26”；暂不支持一份作业混合多章或指定部分小问。')
    chapter = chapter_number(match[1] or match[2])
    listing = match[3].strip()
    # Accept complete large-question lists only: never silently drop exclusions or subquestions.
    listing = re.sub(r'第\s*(?=\d)', '', listing)
    listing = re.sub(r'(?<=\d)\s*题', '', listing)
    listing = re.sub(r'\s*[-~～—–至到]\s*', '-', listing)
    pieces = re.split(r'[\s,，、;；]+', listing)
    numbers = []
    for piece in pieces:
        if not re.fullmatch(r'[1-9][0-9]{0,2}(?:-[1-9][0-9]{0,2})?', piece):
            raise GradingError('作业题号不明确或含部分小问、排除条件；请使用完整大题列表，例如“习题一：1、2、6-8”。')
        bounds = list(map(int, piece.split('-')))
        first, last = bounds[0], bounds[-1]
        if first > last or last > 500:
            raise GradingError('作业题号范围无效。')
        numbers.extend(range(first, last + 1))
    return chapter, list(dict.fromkeys(numbers))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def build_reference_index(root):
    """Rebuild a deterministic index from verified mappings and current asset hashes."""
    folder = Path(root) / 'local' / 'reference-library'
    catalog_path = folder / 'catalog.json'
    if not catalog_path.exists():
        raise GradingError('本机尚未收录题库与官方答案，请先导入并核验资料。')
    catalog = read_json(catalog_path)
    index = {key: catalog[key] for key in ('course_id', 'book_id', 'book_title')}
    index['chapters'] = {}
    checked = {}
    for chapter, section in catalog['chapters'].items():
        questions = {}
        for number, reference in section['questions'].items():
            problems = []
            for kind, label in (('question_images', '原题'), ('answer_images', '官方答案')):
                if not reference.get(kind):
                    problems.append('缺少' + label)
                for item in reference.get(kind, []):
                    path = item['path']
                    if path not in checked:
                        try:
                            current = hashlib.sha256(local_image(root, path).read_bytes()).hexdigest()
                            checked[path] = None if current == catalog['assets'].get(path) else '文件已变化，需重新核验'
                        except GradingError:
                            checked[path] = '文件缺失或路径无效'
                    if checked[path]:
                        problems.append(label + checked[path])
            questions[number] = {'reference': copy.deepcopy(reference), 'problems': sorted(set(problems))}
        index['chapters'][chapter] = {'title': section['title'], 'questions': questions}
    index['version'] = digest(index)
    path = folder / 'index.json'
    if not path.exists() or read_json(path) != index:
        save_json(path, index)
    return index


def resolve_assignment_source(client, assignment_url, root, previous=None):
    course, assignment_id = assignment_ids(assignment_url)
    assignment = client.read(f'/api/v1/courses/{course}/assignments/{assignment_id}')
    if str(assignment.get('id')) != assignment_id or str(assignment.get('course_id')) != course:
        raise GradingError('Canvas 返回的作业身份不匹配，未开始批改。')
    index = build_reference_index(root)
    if index['course_id'] != course:
        raise GradingError('当前课程尚未关联已核验的教材，不能仅按题号匹配。')
    chapter, numbers = parse_exercises(assignment.get('description'))
    section = index['chapters'].get(chapter)
    if section is None:
        raise GradingError(f'题库尚未收录第{chapter}章，未开始批改。')
    selected, missing = {}, []
    for number in numbers:
        entry = section['questions'].get(str(number))
        problems = entry['problems'] if entry else ['原题与官方答案未收录']
        if problems:
            missing.append(f'第{number}题：' + '、'.join(problems))
        else:
            selected[str(number)] = copy.deepcopy(entry['reference'])
    if missing:
        raise GradingError(section['title'] + '匹配未完成；' + '；'.join(missing) + '。补齐并核验这些资料后可再次点击批量修改作业。')
    references = {'status': 'ready', 'missing_answers': [], 'book_id': index['book_id'],
                  'chapter': chapter, 'questions': selected,
                  'version': 'reference-index-' + digest([index['book_id'], chapter, selected])}
    previous = previous if previous and previous.get('assignment_url') == assignment_url else {}
    old = previous.get('reference_materials', {})
    if previous.get('question_numbers') == numbers and old.get('questions') == selected:
        # Preserve the grading signature of already-verified, identical references.
        references = copy.deepcopy(old)
    return {'title': assignment['name'], 'assignment_url': assignment_url, 'question_numbers': numbers,
            'reference_materials': references, 'students': copy.deepcopy(previous.get('students', [])),
            'reference_match': {'book_id': index['book_id'], 'chapter': chapter,
                                'chapter_title': section['title'], 'question_numbers': numbers,
                                'index_version': index['version']}}


def verify_assignment_references(client, batch, root):
    current = resolve_assignment_source(client, batch['assignment_url'], root, batch)
    if (current['question_numbers'] != batch['question_numbers']
            or current['reference_materials'] != batch['reference_materials']):
        raise GradingError('作业题号或参考资料在采集后发生变化，请重新点击批量修改作业。')

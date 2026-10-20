"""Assemble four Codex model judgments into the local human review workbench."""
import argparse
import copy
import json
from pathlib import Path

from qwen_grader import GradingError, read_json, save_json, validate_batch, validate_result
from yxin_grader import build_student, merge_questions

ROOT = Path(__file__).resolve().parents[1] / 'prototypes'
FILES = {'luna': ('luna-draft.json', 'gpt-6-luna'),
         'sol6': ('sol6-review.json', 'gpt-6-sol'),
         'terra': ('terra-draft.json', 'gpt-5.6-terra'),
         'sol56': ('sol56-review.json', 'gpt-5.6-sol')}


def load_judgment(path, expected_model, batch):
    document = read_json(path)
    if document.get('model') != expected_model or document.get('assignment_url') != batch['assignment_url']:
        raise GradingError(f'{path.name} 的模型或作业链接不匹配。')
    submitted = document.get('students', [])
    if len(submitted) != len(batch['students']) or [s.get('id') for s in submitted] != [s['id'] for s in batch['students']]:
        raise GradingError(f'{path.name} 的学生清单不匹配。')
    for original, student in zip(batch['students'], submitted):
        if student.get('name') != original['name']:
            raise GradingError(f'{path.name} 的学生姓名不匹配。')
        student['questions'] = validate_result(student, batch['question_numbers'], original['pages'])
    return document


def assemble(batch, documents, folder, root=ROOT):
    result = {'mode': 'real', 'title': batch['title'] + ' · Codex 模型批改对照',
              'assignment_url': batch['assignment_url'], 'review_revision': folder.name,
              'reference_materials': copy.deepcopy(batch['reference_materials']),
              'codex_run': {'models': {role: model for role, (_, model) in FILES.items()},
                            'usage': None, 'note': 'Codex 会话模型用量与 YXIN API usage 不能直接比较。'},
              'students': [], 'writes': 0, 'comments_written': 0}
    for index, original in enumerate(batch['students']):
        versions = {}
        for version, first_key, second_key in (('6', 'luna', 'sol6'), ('5.6', 'terra', 'sol56')):
            first, second = (documents[key]['students'][index] for key in (first_key, second_key))
            mismatch = first['submission_mismatch'] or second['submission_mismatch']
            versions[version] = {'grader': FILES[first_key][1], 'reviewer': FILES[second_key][1],
                                 'grader_questions': copy.deepcopy(first['questions']),
                                 'reviewer_questions': copy.deepcopy(second['questions']),
                                 'questions': merge_questions(first, second),
                                 'submission_mismatch': mismatch,
                                 'reviewed_pages': second['reviewed_pages']}
        primary = versions['6']
        item = build_student(original, primary['questions'], primary['submission_mismatch'], root,
                             folder, index, primary['reviewed_pages'])
        item['model_comparison'] = versions
        result['students'].append(item)
    return result


def publish(folder, root=ROOT):
    folder = Path(folder).resolve()
    root = Path(root).resolve()
    if not folder.is_relative_to(root / 'local'):
        raise GradingError('运行目录必须位于本机私有资料目录。')
    batch = read_json(root / 'local' / 'batch-source.json')
    validate_batch(batch, root)
    documents = {role: load_judgment(folder / filename, model, batch)
                 for role, (filename, model) in FILES.items()}
    result = assemble(batch, documents, folder, root)
    save_json(folder / 'result.json', result)
    current = root / 'local' / 'batch-review.json'
    if current.exists():
        save_json(folder / 'previous-batch-review.json', read_json(current))
    save_json(current, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run_folder', type=Path)
    args = parser.parse_args()
    outcome = publish(args.run_folder)
    print(json.dumps({'students': len(outcome['students']), 'revision': outcome['review_revision']}, ensure_ascii=False))

from app.pipeline import build_transcript
from app.llm import LLMChecker
from pathlib import Path

PROJECT = Path(r'C:\Users\alena\HSE\Postprocess_project')

ROLE_MAP = {'SPEAKER_00': 'Инф. 1', 'SPEAKER_01': 'Инф. 1', 'SPEAKER_02': 'Соб. 1'}
tr = build_transcript(str(PROJECT / 'data' / 'Pyat_22_22.txt'), 'Pyat_22_22', ROLE_MAP)

# Склеить реплики в один текст для чанков
full_text = ' '.join(r.text for r in tr.replicas)

checker = LLMChecker(
    PROJECT / 'resources' / 'names.json',
    PROJECT / 'resources' / 'toponyms.json',
    PROJECT / 'resources' / 'terms.json',
)

# Первый чанк (первые 600 слов)
chunk = ' '.join(full_text.split()[:600])
print('Чанк: 600 слов')
errors = checker.check_chunk(chunk)
print(f'Ошибок: {len(errors)}')
for e in errors:
    print(f'  {e["wrong"]} → {e["correct"]}')
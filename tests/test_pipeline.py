# -*- coding: utf-8 -*-
"""Быстрая проверка: pipeline + export на Pyat_22_22."""

from app.pipeline import build_transcript
from app.exporter import export_docx
from pathlib import Path

PROJECT = Path(r'C:\Users\alena\HSE\Postprocess_project')
RAW = PROJECT / 'data' / 'Pyat_22_22.txt'
OUT = PROJECT / 'results' / 'Pyat_22_22_test.docx'

# Роли: SPEAKER_01 → Инф. 1, SPEAKER_02 → Соб. 1, SPEAKER_00 → Инф. 1 (как мы выяснили)
ROLE_MAP = {
    'SPEAKER_00': 'Инф. 1',
    'SPEAKER_01': 'Инф. 1',
    'SPEAKER_02': 'Соб. 1',
}

tr = build_transcript(str(RAW), 'Pyat_22_22', ROLE_MAP)
print(f'Сегментов: {len(tr.segments)}')
print(f'Реплик: {len(tr.replicas)}')
print(f'Роли: {sorted({r.role for r in tr.replicas})}')

OUT.parent.mkdir(exist_ok=True)
export_docx(tr, str(OUT))
print(f'Сохранено: {OUT}')

# Первые 10 реплик
print('\nПервые 10 реплик:')
for r in tr.replicas[:10]:
    print(f'  {r.role}: {r.text[:80]}')
# -*- coding: utf-8 -*-
"""
Двухпроходная схема:
  Проход 1: чанк + OOV-список + словари → модель говорит, какие OOV ошибки.
  Проход 2: для каждого предложенного OOV — отдельный маленький запрос-проверка.
"""

import json
import re
import time
from pathlib import Path
from collections import Counter
import ollama
import pymorphy3

PROJECT = Path(r'C:\Users\alena\HSE\Postprocess_project')
INTERVIEW = 'Pyat_22_22'

RAW_PATH = PROJECT / 'data' / f'{INTERVIEW}.txt'
NAMES_PATH = PROJECT / 'resources' / 'names.json'
TOPO_PATH = PROJECT / 'resources' / 'toponyms.json'
TERMS_PATH = PROJECT / 'resources' / 'terms.json'
OUT_ERRORS = PROJECT / 'results' / f'{INTERVIEW}_errors_2pass.json'
OUT_RAW = PROJECT / 'results' / f'{INTERVIEW}_raw_answers_2pass.json'

MODEL = 'qwen2.5:7b-32k'
CHUNK_WORDS = 600
OVERLAP_WORDS = 100
NUM_CTX = 4096

morph = pymorphy3.MorphAnalyzer()

# --- Парсеры ---
RE_RAW = re.compile(r'^\[(SPEAKER_\d+)\]\s*(.*)$')

def parse_raw(path):
    lines = []
    for line in path.read_text(encoding='utf-8').splitlines():
        m = RE_RAW.match(line.strip())
        if m:
            lines.append(m.group(2).strip())
    return ' '.join(lines)

def chunk_text(text, chunk_words=CHUNK_WORDS, overlap=OVERLAP_WORDS):
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_words, len(words))
        chunks.append(' '.join(words[start:end]))
        if end == len(words):
            break
        start = end - overlap
    return chunks

# --- OOV-детектор ---
def is_oov(word):
    for p in morph.parse(word):
        if p.is_known:
            return False
        for lp in morph.parse(p.normal_form):
            if lp.is_known:
                return False
    return True

def find_oov_in_chunk(chunk):
    counter = Counter()
    for w in re.findall(r'[А-Яа-яЁё]+', chunk):
        wl = w.lower().replace('ё', 'е')
        if len(wl) < 3:
            continue
        if is_oov(wl):
            counter[wl] += 1
    return counter

# --- Контекст вокруг слова ---
def get_context(chunk, word, window=8):
    words = chunk.split()
    wl = word.lower().replace('ё', 'е')
    for i, w in enumerate(words):
        w_clean = re.sub(r'[^\w]', '', w).lower().replace('ё', 'е')
        if w_clean == wl:
            start = max(0, i - window)
            end = min(len(words), i + window + 1)
            return ' '.join(words[start:end])
    return chunk[:200]

# --- Списки канонов (без форм, чтобы не раздувать промпт) ---
def load_canon_list(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    return sorted(data.keys())

names_list = load_canon_list(NAMES_PATH)
topo_list = load_canon_list(TOPO_PATH)
terms_list = load_canon_list(TERMS_PATH)

print(f'Имён: {len(names_list)}, топонимов: {len(topo_list)}, терминов: {len(terms_list)}')

# ============================================================
# ПРОХОД 1: чанк + OOV + словари → какие OOV ошибки
# ============================================================

PASS1_SYSTEM = f'''Ты — редактор антропологических интервью горско-еврейской общины Пятигорска.
Ты проверяешь расшифровки Whisper X.

Whisper X заменяет НЕЗНАКОМОЕ слово на ПОХОЖЕЕ ПО ЗВУЧАНИЮ знакомое. Из-за этого теряется смысл.

СЛОВАРИ ПРАВИЛЬНЫХ СУЩНОСТЕЙ:
ИМЕНА: {", ".join(names_list)}
ТОПОНИМЫ: {", ".join(topo_list)}
ТЕРМИНЫ: {", ".join(terms_list)}

Тебе дан фрагмент текста и список слов, которых НЕТ в словаре (OOV).
Твоя задача: для каждого OOV решить — это ОШИБКА распознавания (и предложить замену из словаря) или НОРМАЛЬНОЕ слово (имя, термин, которого нет в словаре, или обычное слово).

ПРАВИЛА:
- Если слово ЕСТЬ в словаре — не трогай (но оно и не будет в OOV).
- Если слово отличается от словарного только падежом/родом/числом — не ошибка.
- Предлагай замену ТОЛЬКО если слово ОТСУТСТВУЕТ в словаре И похоже на словарное И контекст подтверждает.
- НЕ ВЫДУМЫВАЙ замены. Если не уверен — пропусти.

Формат ответа (по одной строке на слово):
ОШИБКА: "<как в тексте>" → "<правильный вариант>"
или
НОРМА: "<слово>"

Если ни одного OOV не считаешь ошибкой — ответь "ВСЁ НОРМА".'''

def pass1(chunk, oov_list):
    prompt = f'''Фрагмент текста:
"{chunk}"

OOV-слова (которых нет в словаре):
{", ".join(oov_list)}

Проверь каждое OOV: это ошибка распознавания или нормальное слово?'''

    resp = ollama.generate(
        model=MODEL,
        system=PASS1_SYSTEM,
        prompt=prompt,
        options={'temperature': 0.0, 'num_ctx': NUM_CTX},
    )
    return resp['response'].strip()

RE_ERR = re.compile(r'ОШИБКА:\s*"([^"]+)"\s*→\s*"([^"]+)"')
RE_NORM = re.compile(r'НОРМА:\s*"([^"]+)"')

def parse_pass1(ans):
    errors = []
    norms = []
    for m in RE_ERR.finditer(ans):
        errors.append({'wrong': m.group(1).strip(), 'correct': m.group(2).strip()})
    for m in RE_NORM.finditer(ans):
        norms.append(m.group(1).strip())
    return errors, norms

# ============================================================
# ПРОХОД 2: проверка каждой предложенной замены
# ============================================================

PASS2_SYSTEM = '''Ты — редактор антропологических интервью. Проверяешь, действительно ли слово в тексте — ошибка распознавания.

Отвечай строго:
- "ДА" — если это ошибка и замена правильная.
- "НЕТ" — если это НЕ ошибка (слово правильное, или замена неверная).

Одна строка, без объяснений.'''

def pass2(chunk, wrong, correct):
    context = get_context(chunk, wrong)
    prompt = f'''Контекст: "{context}"

Слово в тексте: "{wrong}"
Предлагаемая замена: "{correct}"

Это ошибка распознавания? Замена правильная?'''

    resp = ollama.generate(
        model=MODEL,
        system=PASS2_SYSTEM,
        prompt=prompt,
        options={'temperature': 0.0, 'num_ctx': 2048},
    )
    return resp['response'].strip().upper()

# ============================================================
# MAIN
# ============================================================

def main():
    raw_text = parse_raw(RAW_PATH)
    print(f'Raw: {len(raw_text.split())} слов')

    chunks = chunk_text(raw_text)
    print(f'Чанков: {len(chunks)} (по {CHUNK_WORDS} слов)')

    all_errors = []
    raw_log = []
    t0 = time.time()

    for i, chunk in enumerate(chunks):
        print(f'\n[{i+1}/{len(chunks)}] {len(chunk.split())} слов...')

        # --- Проход 1: OOV-детектор ---
        oov = find_oov_in_chunk(chunk)
        oov_list = [w for w, _ in oov.most_common(50)]  # топ-50 OOV
        print(f'  OOV: {len(oov)} уникальных, берём топ-{len(oov_list)}')

        if not oov_list:
            print('  OOV нет — пропускаем')
            continue

        try:
            ans1 = pass1(chunk, oov_list)
            proposed, norms = parse_pass1(ans1)
            print(f'  Проход 1: предложено {len(proposed)} замен, {len(norms)} норм')
            for p in proposed:
                print(f'    {p["wrong"]} → {p["correct"]}')
        except Exception as e:
            print(f'  [!] Проход 1 ошибка: {e}')
            continue

        # --- Проход 2: проверка ---
        confirmed = []
        for p in proposed:
            try:
                ans2 = pass2(chunk, p['wrong'], p['correct'])
                ok = 'ДА' in ans2
                print(f'    Проверка {p["wrong"]} → {p["correct"]}: {"OK" if ok else "ОТКЛОНЕНО"}')
                if ok:
                    confirmed.append(p)
            except Exception as e:
                print(f'    [!] Проход 2 ошибка для {p["wrong"]}: {e}')

        all_errors.extend(confirmed)
        raw_log.append({
            'chunk_idx': i,
            'oov_count': len(oov),
            'pass1_answer': ans1,
            'proposed': proposed,
            'norms': norms,
            'confirmed': confirmed,
        })

    elapsed = time.time() - t0
    print(f'\n=== Итого: {len(all_errors)} подтверждённых ошибок за {elapsed:.1f} сек ===')

    OUT_ERRORS.parent.mkdir(exist_ok=True)
    OUT_ERRORS.write_text(json.dumps(all_errors, ensure_ascii=False, indent=2), encoding='utf-8')
    OUT_RAW.write_text(json.dumps(raw_log, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Сохранено: {OUT_ERRORS}')

if __name__ == '__main__':
    main()
# -*- coding: utf-8 -*-
"""Двухпроходная LLM-проверка через Ollama."""

import json
import re
from collections import Counter
from pathlib import Path

import ollama
import pymorphy3


class LLMChecker:
    """Проверяет текст на ошибки распознавания. Два прохода."""

    MODEL = 'qwen2.5:7b-32k'
    NUM_CTX = 4096

    PASS2_SYSTEM = (
        'Ты — редактор антропологических интервью. '
        'Проверяешь, действительно ли слово — ошибка распознавания.\n'
        'Отвечай строго: "ДА" (ошибка, замена правильная) или "НЕТ" (не ошибка / замена неверная).\n'
        'Одна строка.'
    )

    def __init__(self, names_path, topo_path, terms_path, ethnonyms_path=None):
        self.morph = pymorphy3.MorphAnalyzer()
        self.names = self._load_canon_list(names_path)
        self.toponyms = self._load_canon_list(topo_path)
        self.terms = self._load_canon_list(terms_path)
        if ethnonyms_path and Path(ethnonyms_path).exists():
            self.ethnonyms = self._load_canon_list(ethnonyms_path)
        else:
            self.ethnonyms = []

    # ---------- загрузка словарей ----------

    @staticmethod
    def _load_canon_list(path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return sorted(data.keys())

    # ---------- OOV-детектор ----------

    def is_oov(self, word):
        for p in self.morph.parse(word):
            if p.is_known:
                return False
            for lp in self.morph.parse(p.normal_form):
                if lp.is_known:
                    return False
        return True

    def find_oov(self, text):
        counter = Counter()
        for w in re.findall(r'[А-Яа-яЁё]+', text):
            wl = w.lower().replace('ё', 'е')
            if len(wl) < 3:
                continue
            if self.is_oov(wl):
                counter[wl] += 1
        return counter

    # ---------- вспомогательное ----------

    @staticmethod
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

    @staticmethod
    def _lev(a, b):
        if len(a) < len(b):
            a, b = b, a
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    def _stem(self, word):
        """Грубая основа: отсекаем окончания."""
        endings = ['ями', 'ами', 'ыми', 'ими', 'ого', 'его', 'ому', 'ему',
                   'ой', 'ей', 'ый', 'ий', 'ая', 'яя', 'ое', 'ее', 'ые', 'ие',
                   'ом', 'ем', 'ым', 'им', 'ов', 'ев', 'ин', 'ын',
                   'а', 'я', 'у', 'ю', 'е', 'ы', 'и', 'о', 'ь']
        for end in endings:
            if word.endswith(end) and len(word) - len(end) >= 3:
                return word[:-len(end)]
        return word

    def _is_valid_replacement(self, wrong, correct):
        """Отсекает падежи, регистр, латиницу, бред."""
        w = wrong.lower().replace('ё', 'е').strip()
        c = correct.lower().replace('ё', 'е').strip()

        if w == c:
            return False
        if re.search(r'[a-zA-Z]', correct):
            return False
        if self._stem(w) == self._stem(c):
            return False
        if len(c) > len(w) + 3:
            return False
        if len(c) < len(w) - 4:
            return False
        return True

    # ---------- проход 1 ----------

    def _build_pass1_system(self):
        ethno_line = ''
        if self.ethnonyms:
            ethno_line = 'ЭТНОНИМЫ (горско-еврейские): ' + ', '.join(self.ethnonyms) + '\n'

        return f'''Ты — редактор антропологических интервью горско-еврейской общины Пятигорска.
Ты проверяешь расшифровки Whisper X. Whisper X заменяет НЕЗНАКОМОЕ слово на ПОХОЖЕЕ ПО ЗВУЧАНИЮ знакомое.

СЛОВАРИ:
ИМЕНА: {", ".join(self.names)}
ТОПОНИМЫ: {", ".join(self.toponyms)}
ТЕРМИНЫ: {", ".join(self.terms)}
{ethno_line}
Тебе дан фрагмент и список OOV (слов, которых нет в словаре).
Для каждого OOV: это ОШИБКА распознавания (предложи замену) или НОРМАЛЬНОЕ слово?

ПРАВИЛА:
- Если слово отличается от словарного только падежом/родом/числом — НЕ ошибка.
- Если оба слова есть в словаре — НЕ ошибка.
- Замена НЕ должна содержать латиницу.
- Замена НЕ должна отличаться только регистром.
- НЕ ВЫДУМЫВАЙ. Если не уверен — пропусти.
- ВАЖНО: сравнивай каждое OOV со всеми канонами. Если OOV отличается от канона на 1-2 буквы (пропущена/заменена) — это ошибка, предложи канон.
- Помни: прилагательные/этнонимы от городов (например, «дербенские» от «Дербент» → «дербентские») — тоже ошибка, если написаны с искажением.

Формат: ОШИБКА: "<как в тексте>" → "<правильный вариант>" или НОРМА: "<слово>"'''

    def pass1(self, chunk, oov_list):
        prompt = f'''Фрагмент:
"{chunk}"

OOV: {", ".join(oov_list)}

Проверь каждое OOV.'''
        resp = ollama.generate(
            model=self.MODEL,
            system=self._build_pass1_system(),
            prompt=prompt,
            options={'temperature': 0.0, 'num_ctx': self.NUM_CTX},
        )
        return resp['response'].strip()

    @staticmethod
    def parse_pass1(ans):
        errors, norms = [], []
        for m in re.finditer(r'ОШИБКА:\s*"([^"]+)"\s*→\s*"([^"]+)"', ans):
            errors.append({'wrong': m.group(1).strip(), 'correct': m.group(2).strip()})
        for m in re.finditer(r'НОРМА:\s*"([^"]+)"', ans):
            norms.append(m.group(1).strip())
        return errors, norms

    # ---------- проход 2 ----------

    def pass2(self, chunk, wrong, correct):
        context = self.get_context(chunk, wrong)
        prompt = f'''Контекст: "{context}"
Слово: "{wrong}"
Замена: "{correct}"
Это ошибка? Замена правильная?'''
        resp = ollama.generate(
            model=self.MODEL,
            system=self.PASS2_SYSTEM,
            prompt=prompt,
            options={'temperature': 0.0, 'num_ctx': 2048},
        )
        return resp['response'].strip().upper()

    # ---------- главный метод ----------

    def check_chunk(self, chunk, progress_callback=None):
        """Возвращает список подтверждённых ошибок: [{'wrong', 'correct'}, ...]."""
        oov = self.find_oov(chunk)
        oov_list = [w for w, _ in oov.most_common(50)]
        if not oov_list:
            return []

        ans1 = self.pass1(chunk, oov_list)
        proposed, norms = self.parse_pass1(ans1)

        filtered = [p for p in proposed
                    if self._is_valid_replacement(p['wrong'], p['correct'])]

        confirmed = []
        for p in filtered:
            ans2 = self.pass2(chunk, p['wrong'], p['correct'])
            if 'ДА' in ans2:
                confirmed.append(p)
            if progress_callback:
                progress_callback()
        return confirmed
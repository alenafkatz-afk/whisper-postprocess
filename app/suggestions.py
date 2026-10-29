# -*- coding: utf-8 -*-
"""Сборка вариантов замены для OOV: LLM + словари + этнонимы + лог."""

import json
import re
from pathlib import Path


def levenshtein(a, b):
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


def load_log(log_path):
    """Читает лог правок: {wrong_lower: correct}."""
    result = {}
    p = Path(log_path)
    if not p.exists():
        return result
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return result
    for item in data:
        if item.get('action') in ('accept', 'custom'):
            w = item.get('wrong', '').lower().strip()
            c = item.get('correct', '').strip()
            if w and c:
                result[w] = c
    return result


def collect_variants(oov_word, llm_suggestion, names, toponyms, terms, log_map,
                     ethnonyms=None, max_dict=3):
    """Собирает все варианты замены для OOV.

    Аргументы:
        oov_word        — слово с ошибкой
        llm_suggestion  — что предложила LLM (может быть None)
        names           — список канонов имён
        toponyms        — список канонов топонимов
        terms           — список канонов терминов
        log_map         — dict {wrong: correct} из прошлых сессий
        ethnonyms       — список канонов этнонимов (может быть None)
        max_dict        — сколько вариантов из словарей показывать

    Возвращает dict:
        {
            'word': oov_word,
            'llm': str | None,
            'dict': [(canon, distance), ...],
            'log': str | None,
        }
    """
    w = oov_word.lower().replace('ё', 'е').strip()

    # 1. Из лога
    log_match = log_map.get(w)

    # 2. Из словарей (names, toponyms, terms, ethnonyms) — Лев <= 2
    dict_matches = []
    seen = set()
    canon_lists = [names or [], toponyms or [], terms or [], ethnonyms or []]
    for canon_list in canon_lists:
        for canon in canon_list:
            c = canon.lower().replace('ё', 'е').strip()
            if c in seen:
                continue
            d = levenshtein(w, c)
            if d <= 2 and d > 0:
                dict_matches.append((canon, d))
                seen.add(c)
    dict_matches.sort(key=lambda x: x[1])
    dict_matches = dict_matches[:max_dict]

    # 3. LLM — если не дублирует
    llm = llm_suggestion
    if llm and llm.lower() in {c.lower() for c, _ in dict_matches}:
        llm = None

    return {
        'word': oov_word,
        'llm': llm,
        'dict': dict_matches,
        'log': log_match,
    }
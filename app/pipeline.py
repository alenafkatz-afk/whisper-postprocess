# -*- coding: utf-8 -*-
"""Пайплайн обработки: парсинг, склейка, назначение ролей."""

import re
from .models import Segment, Replica, Transcript

RE_RAW = re.compile(r'^\[(SPEAKER_\d+)\]\s*(.*)$')


def parse_raw_txt(path: str, interview_id: str) -> Transcript:
    """Читает txt от Whisper X, возвращает Transcript с сегментами."""
    segments = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = RE_RAW.match(line)
            if m:
                segments.append(Segment(speaker_raw=m.group(1), text=m.group(2).strip()))
    return Transcript(interview_id=interview_id, segments=segments)


def merge_repair(segments: list) -> list:
    """Склеивает подряд одного спикера + приклеивает короткие вклинивания."""
    merged = []
    for s in segments:
        if merged and merged[-1].speaker_raw == s.speaker_raw:
            merged[-1].text += ' ' + s.text
        else:
            merged.append(Segment(speaker_raw=s.speaker_raw, text=s.text))

    fixed = []
    for i, it in enumerate(merged):
        if (i > 0
                and it.speaker_raw != merged[i-1].speaker_raw
                and len(it.text.split()) < 3
                and it.text[:1].islower()
                and fixed):
            fixed[-1].text += ' ' + it.text
        else:
            fixed.append(it)
    return fixed


def assign_roles(segments: list, role_map: dict) -> list:
    """Превращает сегменты в реплики с ролями."""
    replicas = []
    for s in segments:
        role = role_map.get(s.speaker_raw, s.speaker_raw)
        replicas.append(Replica(role=role, text=s.text, speaker_raw=s.speaker_raw))
    return replicas


def merge_by_role(replicas: list) -> list:
    """Склеивает подряд идущие реплики с одинаковой ролью."""
    out = []
    for r in replicas:
        if out and out[-1].role == r.role:
            out[-1].text += ' ' + r.text
        else:
            out.append(Replica(role=r.role, text=r.text, speaker_raw=r.speaker_raw))
    return out


def merge_broken(replicas: list, max_prev_words: int = 30) -> list:
    """Склеивает реплики, где предыдущая не закончена (!/?),
    а следующая начинается со строчной.
    Точка в конце prev — НЕ считается концом (Whisper ставит её на обрывах)."""
    if not replicas:
        return replicas
    out = [replicas[0]]
    for r in replicas[1:]:
        prev = out[-1]
        prev_ends_hard = prev.text.rstrip().endswith(('!', '?'))
        prev_short = len(prev.text.split()) <= max_prev_words
        curr_starts_lower = r.text[:1].islower() if r.text else False
        if not prev_ends_hard and prev_short and curr_starts_lower:
            out[-1] = Replica(role=prev.role,
                              text=prev.text + ' ' + r.text,
                              speaker_raw=prev.speaker_raw)
        else:
            out.append(r)
    return out


def build_transcript(raw_path: str, interview_id: str, role_map: dict = None) -> Transcript:
    """Полный пайплайн: парсинг → merge_repair → assign_roles → merge_by_role → merge_broken."""
    tr = parse_raw_txt(raw_path, interview_id)
    merged = merge_repair(tr.segments)
    if role_map:
        tr.role_map = role_map
        replicas = assign_roles(merged, role_map)
        replicas = merge_by_role(replicas)
        replicas = merge_broken(replicas)  # склейка обрывов
        tr.replicas = replicas
    else:
        tr.replicas = [Replica(role=s.speaker_raw, text=s.text, speaker_raw=s.speaker_raw)
                       for s in merged]
    return tr
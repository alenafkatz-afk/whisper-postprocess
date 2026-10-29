# -*- coding: utf-8 -*-
"""Модели данных: сегменты, транскрипт, реплики."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Segment:
    """Один сегмент из Whisper X."""
    speaker_raw: str            # SPEAKER_00
    text: str


@dataclass
class Replica:
    """Реплика после обработки (с ролью)."""
    role: str                   # Соб. 1 / Инф. 1
    text: str
    speaker_raw: Optional[str] = None  # исходный SPEAKER_XX (если было)
    suggestions: list = field(default_factory=list)  # предложения LLM для этой реплики


@dataclass
class Transcript:
    """Полный транскрипт интервью."""
    interview_id: str
    segments: list = field(default_factory=list)   # list[Segment]
    replicas: list = field(default_factory=list)   # list[Replica] — после merge
    role_map: dict = field(default_factory=dict)   # SPEAKER_XX -> роль
    suggestions: list = field(default_factory=list) # все предложения LLM

    def speakers(self):
        """Уникальные спикеры из сегментов."""
        return sorted({s.speaker_raw for s in self.segments})
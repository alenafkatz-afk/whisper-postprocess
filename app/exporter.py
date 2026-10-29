# -*- coding: utf-8 -*-
"""Экспорт транскрипта в docx."""

from docx import Document
from .models import Transcript


def export_docx(tr: Transcript, path: str) -> None:
    """Сохраняет reader-версию в docx: 'Роль: текст' без пустых строк."""
    doc = Document()
    for r in tr.replicas:
        doc.add_paragraph(f'{r.role}: {r.text}')
    doc.save(path)
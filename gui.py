# -*- coding: utf-8 -*-
"""GUI: загрузка txt, роли, LLM-проверка, правка по карточкам, ручная правка, аудио, сохранение docx."""

import json
import re
import sys
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QEvent, QTimer, QUrl
from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QDialog, QFormLayout,
    QComboBox, QDialogButtonBox, QTextEdit, QScrollArea,
    QMessageBox, QProgressBar, QSplitter, QInputDialog, QFrame,
    QSlider,
)

from app.pipeline import build_transcript, parse_raw_txt, merge_repair
from app.exporter import export_docx
from app.llm import LLMChecker
from app.models import Replica
from app.suggestions import collect_variants, load_log

PROJECT = Path(__file__).parent
RESOURCES = PROJECT / 'resources'
LOG_PATH = PROJECT / 'results' / 'edits_log.json'
INTERVIEWS_LIST_PATH = PROJECT / 'data' / 'перечень_интервью.txt'


# ============================================================
# Загрузка перечня интервью
# ============================================================

def load_interviews_list(path):
    """Читает 'Pyat_22_XX — Соб. N — Инф. M' → {id: {'sob': N, 'inf': M}}."""
    result = {}
    p = Path(path)
    if not p.exists():
        return result
    text = p.read_text(encoding='utf-8')
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split('—')]
        if len(parts) != 3:
            continue
        interview_id = parts[0]
        m_sob = re.search(r'(\d+)', parts[1])
        m_inf = re.search(r'(\d+)', parts[2])
        if m_sob and m_inf:
            result[interview_id] = {
                'sob': int(m_sob.group(1)),
                'inf': int(m_inf.group(1)),
            }
    return result


# ============================================================
# Диалог ролей
# ============================================================

class RoleDialog(QDialog):
    ROLE_OPTIONS = ['Соб. 1', 'Соб. 2', 'Соб. 3', 'Соб. 4',
                    'Инф. 1', 'Инф. 2', 'Инф. 3', '— пропустить —']

    def __init__(self, speakers, segments, expected=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Назначение ролей')
        self.setMinimumWidth(1000)
        self.combos = {}
        self.expected = expected

        layout = QVBoxLayout(self)

        # КРИТИЧЕСКОЕ предупреждение: один спикер в raw
        if len(speakers) == 1:
            warn = QLabel(
                '⚠️ В raw только ОДИН спикер — Whisper X не различил говорящих. '
                'Все реплики будут у одного спикера. Возможно, придётся разделять вручную.'
            )
            warn.setStyleSheet(
                'background: #f8d7da; color: #721c24; padding: 8px; '
                'border-radius: 4px; font-weight: bold;'
            )
            warn.setWordWrap(True)
            layout.addWidget(warn)

        # Плашка с ожиданием из перечня
        if expected:
            total_expected = expected['sob'] + expected['inf']
            if len(speakers) == total_expected:
                banner_text = (f'✅ По перечню интервью: Соб. {expected["sob"]} + '
                               f'Инф. {expected["inf"]} = {total_expected} человек. '
                               f'Whisper нашёл {len(speakers)} — совпадает.')
                banner_style = ('background: #d4edda; color: #155724; '
                                'padding: 8px; border-radius: 4px;')
            else:
                banner_text = (f'⚠️ По перечню интервью: Соб. {expected["sob"]} + '
                               f'Инф. {expected["inf"]} = {total_expected} человек. '
                               f'Whisper нашёл {len(speakers)} — возможно, '
                               f'склеил или разбил спикеров.')
                banner_style = ('background: #fff3cd; color: #856404; '
                                'padding: 8px; border-radius: 4px;')
            banner = QLabel(banner_text)
            banner.setStyleSheet(banner_style)
            banner.setWordWrap(True)
            layout.addWidget(banner)
        else:
            banner = QLabel('Нет данных в перечне интервью для этого файла.')
            banner.setStyleSheet('color: #999; font-style: italic; padding: 4px;')
            layout.addWidget(banner)

        layout.addWidget(QLabel(
            'Для каждого спикера выберите роль. Справа — первые реплики.'))

        grid = QFormLayout()
        for i, sp in enumerate(speakers):
            combo = QComboBox()
            combo.addItems(self.ROLE_OPTIONS)
            if i == 0:
                combo.setCurrentText('Соб. 1')
            elif i == 1:
                combo.setCurrentText('Инф. 1')
            elif i == 2:
                combo.setCurrentText('Инф. 2')
            self.combos[sp] = combo

            sp_texts = [s.text for s in segments if s.speaker_raw == sp]
            n_segments = len(sp_texts)
            preview = ' / '.join(t[:120] for t in sp_texts[:5])
            if len(preview) > 500:
                preview = preview[:500] + '…'

            row = QHBoxLayout()
            row.addWidget(combo, stretch=1)
            lbl = QLabel(f'<b>[{n_segments} реплик]</b>  {preview or "(нет реплик)"}')
            lbl.setStyleSheet('color: #666; font-style: italic;')
            lbl.setWordWrap(True)
            row.addWidget(lbl, stretch=4)

            wrapper = QWidget()
            wrapper.setLayout(row)
            grid.addRow(sp, wrapper)

        layout.addLayout(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        roles = [c.currentText() for c in self.combos.values()
                 if c.currentText() != '— пропустить —']
        if not roles:
            QMessageBox.warning(self, 'Ошибка', 'Не назначено ни одной роли')
            return
        if len(set(roles)) == 1 and len(roles) > 1:
            if QMessageBox.question(
                self, 'Внимание',
                f'Все спикеры получили одну роль ({roles[0]}). '
                f'Текст склеится в один абзац. Продолжить?',
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        self.accept()

    def get_role_map(self):
        return {sp: c.currentText() for sp, c in self.combos.items()
                if c.currentText() != '— пропустить —'}


# ============================================================
# Плеер аудио
# ============================================================

class AudioPlayer(QWidget):
    """Панель плеера: загрузить, play/pause, перемотка, ползунок, время."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.media = QMediaPlayer()
        self.audio = QAudioOutput()
        self.media.setAudioOutput(self.audio)
        self._is_seeking = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_load = QPushButton('Загрузить аудио')
        
        self.btn_back_min = QPushButton('⏪ 1м')
        self.btn_back_15 = QPushButton('⏪ 15с')
        self.btn_back_min.setEnabled(False)
        self.btn_back_15.setEnabled(False)
        self.btn_back_min.setFixedWidth(60)
        self.btn_back_15.setFixedWidth(60)

        self.btn_play = QPushButton('▶')
        self.btn_play.setFixedWidth(40)
        self.btn_play.setEnabled(False)

        self.btn_fwd_15 = QPushButton('15с ⏩')
        self.btn_fwd_min = QPushButton('1м ⏩')
        self.btn_fwd_15.setEnabled(False)
        self.btn_fwd_min.setEnabled(False)
        self.btn_fwd_15.setFixedWidth(60)
        self.btn_fwd_min.setFixedWidth(60)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)

        self.time_label = QLabel('00:00 / 00:00')
        self.time_label.setMinimumWidth(110)

        layout.addWidget(self.btn_load)
        layout.addWidget(self.btn_back_min)
        layout.addWidget(self.btn_back_15)
        layout.addWidget(self.btn_play)
        layout.addWidget(self.btn_fwd_15)
        layout.addWidget(self.btn_fwd_min)
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.time_label)

        self.btn_load.clicked.connect(self.on_load)
        self.btn_play.clicked.connect(self.on_play_pause)
        self.btn_back_15.clicked.connect(lambda: self._seek_by(-15_000))
        self.btn_back_min.clicked.connect(lambda: self._seek_by(-60_000))
        self.btn_fwd_15.clicked.connect(lambda: self._seek_by(15_000))
        self.btn_fwd_min.clicked.connect(lambda: self._seek_by(60_000))
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderReleased.connect(self.on_slider_released)
        self.media.positionChanged.connect(self.on_position_changed)
        self.media.durationChanged.connect(self.on_duration_changed)
        self.media.playbackStateChanged.connect(self.on_state_changed)

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Загрузить аудио', '',
            'Аудио (*.mp3 *.wav *.m4a *.ogg *.flac);;Все файлы (*)'
        )
        if not path:
            return
        self.media.setSource(QUrl.fromLocalFile(path))
        for b in [self.btn_play, self.btn_back_15, self.btn_back_min,
                  self.btn_fwd_15, self.btn_fwd_min]:
            b.setEnabled(True)
        self.slider.setEnabled(True)

    def on_play_pause(self):
        if self.media.playbackState() == QMediaPlayer.PlayingState:
            self.media.pause()
        else:
            self.media.play()

    def on_state_changed(self, state):
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setText('⏸')
        else:
            self.btn_play.setText('▶')

    def _seek_by(self, delta_ms):
        pos = self.media.position() + delta_ms
        dur = self.media.duration()
        if dur > 0:
            pos = max(0, min(pos, dur))
        else:
            pos = max(0, pos)
        self.media.setPosition(pos)

    def on_position_changed(self, pos):
        if not self._is_seeking:
            self.slider.setValue(pos)
        self._update_time_label(pos, self.media.duration())

    def on_duration_changed(self, dur):
        self.slider.setMaximum(dur)
        self._update_time_label(self.media.position(), dur)

    def on_slider_pressed(self):
        self._is_seeking = True

    def on_slider_released(self):
        self.media.setPosition(self.slider.value())
        self._is_seeking = False

    @staticmethod
    def _fmt(ms):
        if ms <= 0:
            return '00:00'
        s = ms // 1000
        return f'{s // 60:02d}:{s % 60:02d}'

    def _update_time_label(self, pos, dur):
        self.time_label.setText(f'{self._fmt(pos)} / {self._fmt(dur)}')


# ============================================================
# Поток LLM
# ============================================================

class LLMWorker(QThread):
    progress = Signal(int, int)
    finished_all = Signal(list)
    error = Signal(str)

    def __init__(self, replicas, checker, chunk_words=600):
        super().__init__()
        self.replicas = replicas
        self.checker = checker
        self.chunk_words = chunk_words

    def run(self):
        try:
            full_text = ' '.join(r.text for r in self.replicas)
            words = full_text.split()
            chunks = []
            start = 0
            while start < len(words):
                end = min(start + self.chunk_words, len(words))
                chunks.append(' '.join(words[start:end]))
                if end == len(words):
                    break
                start = end - 100

            all_errors = []
            for i, chunk in enumerate(chunks):
                self.progress.emit(i + 1, len(chunks))
                errors = self.checker.check_chunk(chunk)
                for e in errors:
                    e['chunk_idx'] = i
                all_errors.extend(errors)
            self.finished_all.emit(all_errors)
        except Exception as ex:
            self.error.emit(str(ex))


# ============================================================
# Карточка предложения
# ============================================================

class SuggestionCard(QFrame):
    def __init__(self, idx, oov, context, variants, on_choose, on_click, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            'SuggestionCard { background: #fafafa; border: 1px solid #ddd; '
            'border-radius: 4px; padding: 6px; margin: 3px; }'
        )
        self.oov = oov
        self.on_choose = on_choose
        self.on_click = on_click

        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        header = QLabel(f'<b>#{idx}</b> — <span style="color:#c00">{oov}</span>')
        layout.addWidget(header)

        ctx = QLabel(f'…{context}…')
        ctx.setStyleSheet('color: #555; font-style: italic;')
        ctx.setWordWrap(True)
        layout.addWidget(ctx)

        var_layout = QHBoxLayout()
        n = 0
        if variants.get('log'):
            b = QPushButton(f'{variants["log"]} (лог)')
            b.clicked.connect(lambda _, c=variants['log']: on_choose(oov, c))
            var_layout.addWidget(b)
            n += 1
        if variants.get('llm'):
            b = QPushButton(f'{variants["llm"]} (LLM)')
            b.clicked.connect(lambda _, c=variants['llm']: on_choose(oov, c))
            var_layout.addWidget(b)
            n += 1
        for canon, dist in variants.get('dict', []):
            b = QPushButton(f'{canon} (сл.{dist})')
            b.clicked.connect(lambda _, c=canon: on_choose(oov, c))
            var_layout.addWidget(b)
            n += 1
        if n == 0:
            lbl = QLabel('(нет вариантов)')
            lbl.setStyleSheet('color:#999')
            var_layout.addWidget(lbl)
        layout.addLayout(var_layout)

        ctrl = QHBoxLayout()
        b_custom = QPushButton('Ввести своё')
        b_custom.clicked.connect(lambda: on_choose(oov, None))
        b_skip = QPushButton('Пропустить')
        b_skip.clicked.connect(lambda: on_choose(oov, ''))
        ctrl.addWidget(b_custom)
        ctrl.addWidget(b_skip)
        ctrl.addStretch()
        layout.addLayout(ctrl)

    def mousePressEvent(self, event):
        self.on_click(self.oov)
        super().mousePressEvent(event)


# ============================================================
# Главное окно
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Постобработка интервью')
        self.resize(1400, 950)

        self.transcript = None
        self.raw_path = None
        self.edits_log = []
        self.log_map = load_log(LOG_PATH)
        self.interviews_list = load_interviews_list(INTERVIEWS_LIST_PATH)
        self.card_widgets = {}
        self.current_oov = None
        self._names = []
        self._toponyms = []
        self._terms = []
        self._ethnonyms = []

        top = QHBoxLayout()
        self.btn_open = QPushButton('Открыть txt')
        self.btn_roles = QPushButton('Назначить роли')
        self.btn_llm = QPushButton('Проверить LLM')
        self.btn_save = QPushButton('Сохранить docx')
        self.btn_roles.setEnabled(False)
        self.btn_llm.setEnabled(False)
        self.btn_save.setEnabled(False)
        for b in [self.btn_open, self.btn_roles, self.btn_llm, self.btn_save]:
            top.addWidget(b)
        top.addStretch()

        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(False)
        self.text_edit.setPlaceholderText('Здесь появится текст интервью...')
        self.text_edit.viewport().installEventFilter(self)

        self.cards_widget = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.cards_widget)
        self.scroll.setMinimumWidth(440)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.text_edit)
        splitter.addWidget(self.scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.audio_player = AudioPlayer()

        self.status = QLabel('Готово. Откройте txt-файл.')
        self.status.setStyleSheet('padding:5px; background:#f0f0f0;')

        main_layout = QVBoxLayout()
        main_layout.addLayout(top)
        main_layout.addWidget(self.progress)
        main_layout.addWidget(splitter, stretch=1)
        main_layout.addWidget(self.audio_player)
        main_layout.addWidget(self.status)

        central = QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

        self.btn_open.clicked.connect(self.on_open)
        self.btn_roles.clicked.connect(self.on_roles)
        self.btn_llm.clicked.connect(self.on_llm)
        self.btn_save.clicked.connect(self.on_save)

    # ---------- подсветка ----------

    def _paint_highlights(self):
        if not self.card_widgets:
            self.text_edit.setExtraSelections([])
            return

        plain = self.text_edit.toPlainText()
        lower = plain.lower()
        selections = []

        for oov in sorted(self.card_widgets.keys(), key=len, reverse=True):
            wl = oov.lower()
            idx = 0
            while True:
                pos = lower.find(wl, idx)
                if pos < 0:
                    break
                before_ok = (pos == 0 or not plain[pos - 1].isalpha())
                after_ok = (pos + len(wl) >= len(plain) or not plain[pos + len(wl)].isalpha())
                if before_ok and after_ok:
                    sel = QTextEdit.ExtraSelection()
                    fmt = QTextCharFormat()
                    if oov == self.current_oov:
                        fmt.setBackground(QColor('orange'))
                    else:
                        fmt.setBackground(QColor('yellow'))
                    sel.format = fmt
                    c = QTextCursor(self.text_edit.document())
                    c.setPosition(pos)
                    c.setPosition(pos + len(wl), QTextCursor.KeepAnchor)
                    sel.cursor = c
                    selections.append(sel)
                    break
                idx = pos + 1

        self.text_edit.setExtraSelections(selections)

    def _clear_highlights(self):
        self.text_edit.setExtraSelections([])

    # ---------- eventFilter ----------

    def eventFilter(self, obj, event):
        if obj is self.text_edit.viewport() and event.type() == QEvent.MouseButtonRelease:
            if event.button() == Qt.LeftButton:
                pos = event.position().toPoint()
                cursor = self.text_edit.cursorForPosition(pos)
                c2 = QTextCursor(cursor)
                c2.select(QTextCursor.WordUnderCursor)
                word = c2.selectedText().strip().strip('.,!?;:()[]«»"\'')
                if word and self.card_widgets:
                    key = None
                    for oov in self.card_widgets:
                        if oov.lower() == word.lower():
                            key = oov
                            break
                    if key:
                        self._activate_card(key)
        return super().eventFilter(obj, event)

    def _activate_card(self, oov):
        if oov not in self.card_widgets:
            return
        self.current_oov = oov
        self._paint_highlights()
        card = self.card_widgets[oov]
        QTimer.singleShot(50, lambda: self.scroll.ensureWidgetVisible(card, 30, 30))
        QTimer.singleShot(50, lambda: self._scroll_text_to_word(oov))

    def _scroll_text_to_word(self, word):
        plain = self.text_edit.toPlainText()
        wl = word.lower()
        idx = plain.lower().find(wl)
        if idx < 0:
            return
        cursor = QTextCursor(self.text_edit.document())
        cursor.setPosition(idx)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()

    # ---------- открытие ----------

    def on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Открыть txt', str(PROJECT / 'data'), '*.txt')
        if not path:
            return
        self.raw_path = path
        self.transcript = parse_raw_txt(path, Path(path).stem)
        speakers = self.transcript.speakers()

        # Обновляем заголовок окна
        self.setWindowTitle(f'Постобработка интервью — {self.transcript.interview_id}')

        # --- проверка диаризации ---
        total_segments = len(self.transcript.segments)
        if total_segments > 0:
            counts = Counter(s.speaker_raw for s in self.transcript.segments)
            max_count = max(counts.values())
            max_percent = max_count / total_segments * 100

            if len(speakers) == 1:
                QMessageBox.warning(
                    self, 'Диаризация не сработала',
                    f'В raw только ОДИН спикер ({speakers[0]}).\n\n'
                    f'Whisper X не различил говорящих. '
                    f'Все реплики будут у одного спикера — разделять придётся вручную.'
                )
            elif max_percent > 80:
                top_sp = counts.most_common(1)[0][0]
                QMessageBox.warning(
                    self, 'Диаризация подозрительная',
                    f'Спикер {top_sp} содержит {max_percent:.0f}% сегментов.\n\n'
                    f'Возможно, Whisper X не различил говорящих. '
                    f'Проверьте назначение ролей внимательно.'
                )

        # --- показать текст после merge_repair (с SPEAKER_XX) ---
        merged = merge_repair(self.transcript.segments)
        raw_lines = [f'[{s.speaker_raw}] {s.text}' for s in merged]
        self.text_edit.setPlainText('\n'.join(raw_lines))

        self.status.setText(f'Загружено: {self.transcript.interview_id} — '
                            f'{len(self.transcript.segments)} сегментов, '
                            f'спикеры: {", ".join(speakers)}')
        self.btn_roles.setEnabled(True)
        self.btn_llm.setEnabled(False)
        self.btn_save.setEnabled(False)
        self._clear_highlights()
        self._clear_cards()
        self.edits_log = []

    # ---------- роли ----------

    def on_roles(self):
        if not self.transcript:
            return

        if self.edits_log:
            reply = QMessageBox.question(
                self, 'Внимание',
                f'У вас есть {len(self.edits_log)} правок. '
                f'При переназначении ролей текст пересоберётся, '
                f'и часть правок может не примениться (если изменится разбивка реплик). '
                f'Продолжить?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        expected = self.interviews_list.get(self.transcript.interview_id)

        dlg = RoleDialog(
            self.transcript.speakers(),
            self.transcript.segments,
            expected=expected,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        role_map = dlg.get_role_map()
        if not role_map:
            return

        self.transcript = build_transcript(
            self.raw_path, self.transcript.interview_id, role_map)

        applied = 0
        for entry in self.edits_log:
            if entry.get('action') not in ('accept', 'custom'):
                continue
            wrong = entry.get('wrong', '')
            correct = entry.get('correct', '')
            if not wrong or not correct:
                continue
            if self._apply_edit_to_model(wrong, correct):
                applied += 1

        self.current_oov = None
        lines = [f'{r.role}: {r.text}' for r in self.transcript.replicas]
        self.text_edit.setPlainText('\n'.join(lines))

        total = sum(1 for e in self.edits_log if e.get('action') in ('accept', 'custom'))
        if total > 0:
            self.status.setText(
                f'Роли переназначены. Правок: {applied} из {total} применилось.'
            )
        else:
            self.status.setText(f'Роли назначены. Реплик: {len(self.transcript.replicas)}')

        self.btn_llm.setEnabled(True)
        self.btn_save.setEnabled(True)

    def _apply_edit_to_model(self, wrong, correct):
        pattern = re.compile(
            r'(?<![А-Яа-яЁёA-Za-z])' + re.escape(wrong) + r'(?![А-Яа-яЁёA-Za-z])',
            re.IGNORECASE
        )
        for r in self.transcript.replicas:
            new_text, n = pattern.subn(correct, r.text, count=1)
            if n > 0:
                r.text = new_text
                return True
        return False

    # ---------- LLM ----------

    def on_llm(self):
        if not self.transcript or not self.transcript.replicas:
            return
        self.btn_llm.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._clear_cards()

        try:
            checker = LLMChecker(
                RESOURCES / 'names.json',
                RESOURCES / 'toponyms.json',
                RESOURCES / 'terms.json',
                RESOURCES / 'ethnonyms.json',
            )
        except Exception as ex:
            QMessageBox.critical(self, 'Ошибка', f'Словари: {ex}')
            self.btn_llm.setEnabled(True)
            return

        self._names = checker.names
        self._toponyms = checker.toponyms
        self._terms = checker.terms
        self._ethnonyms = checker.ethnonyms

        self.worker = LLMWorker(self.transcript.replicas, checker)
        self.worker.progress.connect(self.on_llm_progress)
        self.worker.finished_all.connect(self.on_llm_finished)
        self.worker.error.connect(self.on_llm_error)
        self.worker.start()

    def on_llm_progress(self, cur, total):
        self.progress.setMaximum(total)
        self.progress.setValue(cur)
        self.status.setText(f'LLM: чанк {cur} из {total}...')

    def on_llm_finished(self, errors):
        self.progress.setVisible(False)
        self.btn_llm.setEnabled(True)
        self.transcript.suggestions = errors

        full_text = self.text_edit.toPlainText()

        for i, e in enumerate(errors, 1):
            ctx = self._context(full_text, e['wrong'])
            variants = collect_variants(
                e['wrong'], e['correct'],
                self._names, self._toponyms, self._terms,
                self.log_map,
                self._ethnonyms,
            )
            card = SuggestionCard(i, e['wrong'], ctx, variants,
                                  self.on_choose, self._activate_card)
            self.cards_layout.addWidget(card)
            self.card_widgets[e['wrong']] = card

        self.current_oov = None
        self._paint_highlights()
        self.status.setText(
            f'LLM: {len(errors)} OOV. Кликните на слово в тексте или карточку.')

    def on_llm_error(self, msg):
        self.progress.setVisible(False)
        self.btn_llm.setEnabled(True)
        QMessageBox.critical(self, 'Ошибка LLM', msg)

    # ---------- контекст ----------

    def _context(self, text, word, window=6):
        words = text.split()
        wl = word.lower()
        for i, w in enumerate(words):
            if wl in w.lower():
                s = max(0, i - window)
                e = min(len(words), i + window + 1)
                return ' '.join(words[s:e])
        return text[:80]

    # ---------- выбор варианта ----------

    def on_choose(self, oov, correct):
        if correct == '':
            self.edits_log.append({'action': 'skip', 'wrong': oov, 'correct': ''})
            self._remove_card(oov)
            self._paint_highlights()
            return

        if correct is None:
            text, ok = QInputDialog.getText(
                self, 'Своя замена', f'Правильный вариант для «{oov}»:')
            if not ok or not text.strip():
                return
            correct = text.strip()
            action = 'custom'
        else:
            action = 'accept'

        if not self._replace_in_textedit(oov, correct):
            self.status.setText(f'Слово «{oov}» не найдено в тексте')
            return

        self.log_map[oov.lower()] = correct
        self.edits_log.append({'action': action, 'wrong': oov, 'correct': correct})
        self._remove_card(oov)
        self._paint_highlights()
        self.status.setText(f'Заменено: {oov} → {correct}')

    def _replace_in_textedit(self, wrong, correct):
        plain = self.text_edit.toPlainText()
        wl = wrong.lower()
        idx = 0
        while True:
            pos = plain.lower().find(wl, idx)
            if pos < 0:
                return False
            before_ok = (pos == 0 or not plain[pos - 1].isalpha())
            after_ok = (pos + len(wl) >= len(plain) or not plain[pos + len(wl)].isalpha())
            if before_ok and after_ok:
                cursor = QTextCursor(self.text_edit.document())
                cursor.setPosition(pos)
                cursor.setPosition(pos + len(wl), QTextCursor.KeepAnchor)
                cursor.insertText(correct)
                return True
            idx = pos + 1

    def _remove_card(self, oov):
        card = self.card_widgets.pop(oov, None)
        if card:
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        if self.current_oov == oov:
            self.current_oov = None

    def _clear_cards(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.card_widgets = {}
        self.current_oov = None

    # ---------- синхронизация ----------

    def _sync_text_to_model(self):
        if not self.transcript:
            return
        text = self.text_edit.toPlainText()
        replicas = []
        for line in text.split('\n'):
            if ':' in line:
                role, _, txt = line.partition(':')
                replicas.append(Replica(role=role.strip(), text=txt.strip()))
        if replicas:
            self.transcript.replicas = replicas

    # ---------- сохранение ----------

    def on_save(self):
        if not self.transcript:
            return

        # Имя по умолчанию: Pyat_22_22.docx в папке results
        default_name = str(PROJECT / 'results' / f'{self.transcript.interview_id}.docx')

        path, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить docx', default_name, '*.docx')
        if not path:
            return
        if not path.endswith('.docx'):
            path += '.docx'

        self._sync_text_to_model()

        LOG_PATH.parent.mkdir(exist_ok=True)
        existing = []
        if LOG_PATH.exists():
            try:
                existing = json.loads(LOG_PATH.read_text(encoding='utf-8'))
            except Exception:
                existing = []
        existing.extend(self.edits_log)
        LOG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

        export_docx(self.transcript, path)
        self.status.setText(f'Сохранено: {path}')
        QMessageBox.information(
            self, 'Готово',
            f'Сохранено:\n{path}\nЛог: {LOG_PATH}'
        )


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
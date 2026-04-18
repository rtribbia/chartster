"""PySide6 wizard GUI for chartster — happy path from a Songsterr URL."""

from __future__ import annotations

import shutil
import subprocess
import sys
import traceback
import dataclasses
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional

import threading

from PySide6.QtCore import QObject, QPointF, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor, QCursor, QFont, QIcon, QMovie, QPainter, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QButtonGroup,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from . import config as cfg
from . import fetch as sfetch
from . import video_points as vp
from .chart import TICKS_PER_BEAT, estimate_duration, render
from .mapping import (
    BLUE, GREEN, KICK, LANE_OPTIONS, RED, SONGSTERR_TO_CH, YELLOW, drum_name,
)

if getattr(sys, "frozen", False):
    ASSETS_DIR = Path(sys._MEIPASS) / "chartster" / "assets"
else:
    ASSETS_DIR = Path(__file__).parent / "assets"
_LANE_ASSET = {
    (RED, False): "red_pad.gif",
    (YELLOW, False): "yellow_pad.gif",
    (YELLOW, True): "yellow_cymbal.gif",
    (BLUE, False): "blue_pad.gif",
    (BLUE, True): "blue_cymbal.gif",
    (GREEN, False): "green_pad.gif",
    (GREEN, True): "green_cymbal.gif",
}
_icon_cache: dict[str, QIcon] = {}


def _lane_label(lane) -> str:
    if lane is None:
        return "(removed)"
    for name, opt in LANE_OPTIONS:
        if opt is not None and opt.lane == lane.lane and opt.is_cymbal == lane.is_cymbal:
            return name
    return f"lane {lane.lane}{'c' if lane.is_cymbal else ''}"


def _lane_icon(lane) -> Optional[QIcon]:
    if lane is None:
        return _emoji_icon("🚫")
    if lane.lane == KICK:
        return _emoji_icon("👟")
    fname = _LANE_ASSET.get((lane.lane, lane.is_cymbal))
    if not fname:
        return None
    if fname in _icon_cache:
        return _icon_cache[fname]
    path = ASSETS_DIR / fname
    movie = QMovie(str(path))
    movie.jumpToFrame(0)
    pm = movie.currentPixmap()
    if pm.isNull():
        pm = QPixmap(str(path))
    icon = QIcon(pm)
    _icon_cache[fname] = icon
    return icon


class _VCenterDelegate(QStyledItemDelegate):
    """Force dropdown text to vertically center against the tall icon row,
    and give every row the same height regardless of whether its icon is a
    native-size GIF or a rendered-at-40px emoji pixmap.
    """

    ROW_HEIGHT = 44

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignVCenter | Qt.AlignLeft

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(self.ROW_HEIGHT)
        return size


def _emoji_icon(glyph: str, size: int = 40) -> QIcon:
    key = f"emoji:{glyph}:{size}"
    if key in _icon_cache:
        return _icon_cache[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    font = QFont()
    font.setPixelSize(int(size * 0.8))
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, glyph)
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon

from .songsterr import parse_dict


@dataclass
class State:
    url: str = ""
    song_id: Optional[int] = None
    revision_id: Optional[int] = None
    meta: dict = field(default_factory=dict)
    tracks: list = field(default_factory=list)
    popular_drum_part_id: Optional[int] = None
    part_id: Optional[int] = None
    song: Any = None
    mapping: dict = field(default_factory=dict)
    alignments: list = field(default_factory=list)
    alignment: Optional[dict] = None
    output_dir: str = ""
    song_name: str = ""
    artist: str = ""
    download_audio: bool = True
    ytdlp_path: str = "yt-dlp"
    ffmpeg_path: str = "ffmpeg"


class _Emitter(QObject):
    done = Signal(object)
    failed = Signal(str)


def run_async(parent: QObject, fn: Callable[[], Any],
              on_done: Callable[[Any], None],
              on_failed: Callable[[str], None]) -> _Emitter:
    """Run `fn` on a background thread; marshal result back via queued signal
    so on_done/on_failed fire on the main (GUI) thread."""
    emitter = _Emitter(parent)
    emitter.done.connect(on_done, Qt.QueuedConnection)
    emitter.failed.connect(on_failed, Qt.QueuedConnection)

    def work():
        try:
            result = fn()
        except Exception:
            emitter.failed.emit(traceback.format_exc())
        else:
            emitter.done.emit(result)

    threading.Thread(target=work, daemon=True).start()
    return emitter


def run_soon(fn, on_done, on_failed):
    """Backwards-compatible alias — now actually runs on a background thread."""
    # Hacky retrieval of the "parent" we should keep the emitter on: use the
    # QApplication instance; emitter parent matters only for lifetime and the
    # app outlives everything.
    run_async(QApplication.instance(), fn, on_done, on_failed)


class UrlPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._fetched = False
        self.setTitle("Songsterr URL")
        self.setSubTitle("Paste a Songsterr tab URL. We'll fetch the song info next.")

        layout = QVBoxLayout(self)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "https://www.songsterr.com/a/wsa/artist-title-drum-tab-sXXXXX"
        )
        self.url_edit.textChanged.connect(self._on_changed)
        layout.addWidget(QLabel("Songsterr URL:"))
        layout.addWidget(self.url_edit)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addStretch(1)

    def _on_changed(self, *_):
        self._fetched = False
        self.status.setText("")
        self.completeChanged.emit()

    def initializePage(self) -> None:
        # Clear widget state so Finish-as-restart returns a blank URL page.
        self.url_edit.clear()
        self.status.clear()
        self.progress.hide()
        self._fetched = False
        self.url_edit.setFocus()

    def isComplete(self) -> bool:
        return bool(self.url_edit.text().strip())

    def validatePage(self) -> bool:
        if self._fetched:
            return True
        url = self.url_edit.text().strip()
        if not url:
            self.status.setText("Please enter a URL.")
            return False
        self.status.setText("Fetching song info…")
        self.progress.show()
        self.url_edit.setEnabled(False)

        def work():
            song_id = sfetch.parse_song_url(url)
            revisions = sfetch.fetch_revisions(song_id)
            rev_id, meta = sfetch.latest_published_revision(song_id, revisions)
            return song_id, rev_id, meta

        def on_done(result):
            song_id, rev_id, meta = result
            self.state.url = url
            self.state.song_id = song_id
            self.state.revision_id = rev_id
            self.state.meta = meta
            current = meta.get("current") or {}
            tracks = meta.get("tracks") or current.get("tracks") or []
            for i, t in enumerate(tracks):
                t.setdefault("partId", i)
            self.state.tracks = tracks
            popular = (sfetch.popular_drum_part_id(meta)
                       or sfetch.popular_drum_part_id(current))
            if popular is None:
                for i, t in enumerate(tracks):
                    if (t.get("instrument") or "").lower().startswith("drum"):
                        popular = i
                        break
            self.state.popular_drum_part_id = popular
            self.state.song_name = meta.get("title") or current.get("title") or ""
            self.state.artist = meta.get("artist") or current.get("artist") or ""
            self._fetched = True
            self.progress.hide()
            self.url_edit.setEnabled(True)
            self.status.setText(
                f"songId={song_id} revisionId={rev_id} — {len(tracks)} tracks"
            )
            self.completeChanged.emit()
            self.wizard().next()

        def on_failed(msg):
            self.progress.hide()
            self.url_edit.setEnabled(True)
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")

        self._emitter = run_async(self, work, on_done, on_failed)
        return False


class TrackPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self.setTitle("Select track")
        self.setSubTitle("Pick which instrument track to convert.")

        outer = QVBoxLayout(self)
        note = QLabel("This tool only supports drum charts currently. "
                      "Non-drum tracks are hidden.")
        note.setStyleSheet("color: #888;")
        note.setWordWrap(True)
        outer.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        self._vbox.addStretch(1)
        scroll.setWidget(self._container)
        outer.addWidget(scroll)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(lambda _: self.completeChanged.emit())

    def initializePage(self) -> None:
        for btn in list(self._group.buttons()):
            self._group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        popular = self.state.popular_drum_part_id
        ordered = sorted(
            (
                (i, t) for i, t in enumerate(self.state.tracks)
                if "drum" in (t.get("instrument") or "").lower()
                or "percussion" in (t.get("instrument") or "").lower()
            ),
            key=lambda it: (0 if it[1].get("partId") == popular else 1, it[0]),
        )
        default_btn = None
        for orig_idx, t in ordered:
            inst = t.get("instrument") or ""
            name = t.get("name") or t.get("title") or ""
            label = f"{name} - {inst}" if name else inst
            if t.get("partId") == popular:
                label += "   ★ most viewed drum track"
            btn = QRadioButton(label)
            self._group.addButton(btn, orig_idx)
            self._vbox.insertWidget(self._vbox.count() - 1, btn)
            if t.get("partId") == popular and default_btn is None:
                default_btn = btn
        if default_btn is not None:
            default_btn.setChecked(True)
        elif self._group.buttons():
            self._group.buttons()[0].setChecked(True)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._group.checkedId() >= 0

    def validatePage(self) -> bool:
        idx = self._group.checkedId()
        if idx < 0:
            return False
        t = self.state.tracks[idx]
        self.state.part_id = t.get("partId", idx)
        return True


# Border palette — extra drums that pile onto the same CH lane get a
# distinguishing border from this list (in order). Entry 0 is `None` meaning
# "default outline" so the first drum on any given lane looks plain.
_BORDER_PALETTE: list = [
    None,
    QColor("#ff2d95"),   # magenta
    QColor("#00d9d9"),   # cyan
    QColor("#ffffff"),   # white
    QColor("#bf6eff"),   # purple
    QColor("#ff8800"),   # bright orange
    QColor("#e84b3a"),   # red
    QColor("#2d82d5"),   # blue
    QColor("#3aab3a"),   # green
]


def _instrument_icon(lane, border, size: int = 16) -> QIcon:
    """Render the per-hit preview shape (pad/cymbal/kick) into a small icon
    matching the preview's NOTE_SIZE, with an optional colored border."""
    pad = 3
    dim = size + pad * 2
    pm = QPixmap(dim, dim)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        cx = dim / 2
        cy = dim / 2
        half = size / 2
        if lane.lane == KICK:
            color = border if border is not None else ChartPreview.KICK_COLOR
            p.setPen(QPen(color, 2))
            p.drawLine(int(cx - half), int(cy),
                       int(cx + half), int(cy))
        else:
            p.setBrush(ChartPreview.LANE_COLOR[lane.lane])
            if border is not None:
                p.setPen(QPen(border, 2))
            else:
                p.setPen(QPen(ChartPreview.NOTE_OUTLINE, 1))
            if lane.is_cymbal:
                tri = QPolygonF([
                    QPointF(cx, cy - half),
                    QPointF(cx - half, cy + half),
                    QPointF(cx + half, cy + half),
                ])
                p.drawPolygon(tri)
            else:
                p.drawRect(int(cx - half), int(cy - half), size, size)
    finally:
        p.end()
    return QIcon(pm)


class _CollapsibleSection(QWidget):
    """Header-clickable section whose list of rows can be expanded/collapsed.
    Self-hides when empty so sections don't leave phantom headers around."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._expanded = False
        self._count = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._header = QPushButton()
        self._header.setFlat(True)
        self._header.setCursor(QCursor(Qt.PointingHandCursor))
        self._header.setStyleSheet(
            "text-align:left; font-weight:bold; padding:4px 2px;")
        self._header.clicked.connect(self._toggle)
        v.addWidget(self._header)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 0, 0, 4)
        self._content_layout.setSpacing(1)
        self._content.setVisible(False)
        v.addWidget(self._content)
        self._refresh_header()
        self.setVisible(False)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self._refresh_header()

    def _refresh_header(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow} {self._title} ({self._count})")

    def set_rows(self, rows: list) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for row in rows:
            self._content_layout.addWidget(row)
        self._count = len(rows)
        self._refresh_header()
        self.setVisible(self._count > 0)
        if self._count == 0 and self._expanded:
            self._expanded = False
            self._content.setVisible(False)


def _warning_row(measure_num: int, frets: list, mapping: dict,
                 borders: dict) -> QWidget:
    """A single warning-instance row: 'Measure N' text + instrument symbols."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(3)
    label = QLabel(f"M.{measure_num}")
    label.setMinimumWidth(44)
    label.setStyleSheet("color:#aaa;")
    row.addWidget(label)
    for fret in frets:
        lane = mapping.get(fret)
        if lane is None:
            continue
        sym = QLabel()
        sym.setFixedSize(22, 22)
        sym.setAlignment(Qt.AlignCenter)
        sym.setPixmap(_instrument_icon(lane, borders.get(fret)).pixmap(22, 22))
        sym.setToolTip(drum_name(fret))
        row.addWidget(sym)
    row.addStretch(1)
    return w


class ChartPreview(QWidget):
    """Vertical 2D visual preview of the chart.

    Song start at the bottom, end at the top. Pads = squares, cymbals =
    upward triangles, kicks = full-width yellow line. Height is proportional
    to note durations (pixels-per-whole-note) so dense passages look dense.
    """

    LANE_WIDTH = 34
    PAD_X = 18
    PAD_Y = 24
    LABEL_MARGIN = 30   # left gutter for measure numbers
    PX_PER_WHOLE = 200
    NOTE_SIZE = 16

    LANE_COLOR = {
        RED:    QColor("#e84b3a"),
        YELLOW: QColor("#f5c518"),
        BLUE:   QColor("#2d82d5"),
        GREEN:  QColor("#3aab3a"),
    }
    KICK_COLOR    = QColor("#e6a017")
    BG_COLOR      = QColor("#17191c")
    MEASURE_LINE  = QColor("#34373c")
    LANE_GUIDE    = QColor("#24262a")
    NOTE_OUTLINE  = QColor("#0a0b0d")
    LABEL_COLOR   = QColor("#6a6d72")

    _LANE_ORDER = (RED, YELLOW, BLUE, GREEN)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.song = None
        self.mapping: dict = {}
        self._borders: dict = {}           # fret -> QColor or None
        self._hits: list = []              # (pos_whole, fret, lane, is_cymbal)
        self._kicks: list = []             # (pos_whole, fret)
        self._measure_starts: list = []
        self._total_whole: float = 0.0
        self.setAutoFillBackground(False)
        self.setMinimumWidth(len(self._LANE_ORDER) * self.LANE_WIDTH
                             + 2 * self.PAD_X)

    def setSong(self, song) -> None:
        self.song = song
        self._rebuild()

    def setMapping(self, mapping: dict) -> None:
        self.mapping = mapping
        self._rebuild()

    def setBorders(self, borders: dict) -> None:
        self._borders = borders
        self.update()

    def _rebuild(self) -> None:
        self._hits.clear()
        self._kicks.clear()
        self._measure_starts.clear()
        self._total_whole = 0.0
        if self.song is not None:
            pos = 0.0
            for measure in self.song.measures:
                self._measure_starts.append(pos)
                measure_whole = measure.signature[0] / measure.signature[1]
                for voice in measure.voices:
                    voice_pos = pos
                    grace_buf: list = []
                    for beat in voice.beats:
                        if beat.grace:
                            grace_buf.append(beat)
                            continue
                        trailing = 0.0
                        for i in range(len(grace_buf) - 1, -1, -1):
                            trailing += float(grace_buf[i].duration)
                            g = grace_buf[i]
                            g_pos = max(pos, voice_pos - trailing)
                            if not g.rest:
                                self._emit_hits(g_pos, g)
                        grace_buf.clear()
                        if not beat.rest:
                            self._emit_hits(voice_pos, beat)
                        voice_pos += float(beat.duration)
                pos += measure_whole
            self._total_whole = pos

        self._assign_offsets()
        h = int(self._total_whole * self.PX_PER_WHOLE) + 2 * self.PAD_Y
        self.setMinimumHeight(max(200, h))
        self.updateGeometry()
        self.update()

    def _assign_offsets(self) -> None:
        """Nudge overlapping same-lane hits horizontally so both are visible.
        Groups hits by (pos, lane, is_cymbal); within a group sorts by fret
        and spreads each across the lane center with fixed spacing."""
        groups: dict = {}
        for pos, _fret, lane, is_cymbal in self._hits:
            groups.setdefault((pos, lane, is_cymbal), [])
        # Second pass collects distinct frets per group in stable order.
        for pos, fret, lane, is_cymbal in self._hits:
            bucket = groups[(pos, lane, is_cymbal)]
            if fret not in bucket:
                bucket.append(fret)
        for bucket in groups.values():
            bucket.sort()
        rewritten = []
        for pos, fret, lane, is_cymbal in self._hits:
            frets = groups[(pos, lane, is_cymbal)]
            n = len(frets)
            if n <= 1:
                offset = 0.0
            else:
                i = frets.index(fret)
                spacing = 6.0 if n == 2 else 5.0
                offset = (i - (n - 1) / 2) * spacing
            rewritten.append((pos, fret, lane, is_cymbal, offset))
        self._hits = rewritten

    def _emit_hits(self, pos: float, beat) -> None:
        for n in beat.notes:
            lane = self.mapping.get(n.fret)
            if lane is None:
                continue
            if lane.lane == KICK:
                self._kicks.append((pos, n.fret))
            else:
                self._hits.append((pos, n.fret, lane.lane, lane.is_cymbal))

    def sizeHint(self):
        h = int(self._total_whole * self.PX_PER_WHOLE) + 2 * self.PAD_Y
        w = len(self._LANE_ORDER) * self.LANE_WIDTH + 2 * self.PAD_X
        return QSize(w, max(200, h))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.fillRect(self.rect(), self.BG_COLOR)
            if self.song is None:
                return

            w = self.width()
            h = self.height()
            top = self.PAD_Y
            bottom = h - self.PAD_Y
            chart_px = self._total_whole * self.PX_PER_WHOLE
            # Song start anchored to the widget's bottom; end = bottom - chart_px.
            base_y = bottom

            lanes_inner_left = self.LABEL_MARGIN
            lanes_inner_right = w - self.PAD_X
            lane_count = len(self._LANE_ORDER)
            inner_w = lanes_inner_right - lanes_inner_left
            lane_x = {
                lane: lanes_inner_left + (i + 0.5) * inner_w / lane_count
                for i, lane in enumerate(self._LANE_ORDER)
            }

            p.setPen(self.LANE_GUIDE)
            for x in lane_x.values():
                p.drawLine(int(x), int(base_y - chart_px), int(x), int(base_y))

            label_font = QFont()
            label_font.setPointSize(8)
            for i, m_pos in enumerate(self._measure_starts):
                y = base_y - m_pos * self.PX_PER_WHOLE
                p.setPen(self.MEASURE_LINE)
                p.drawLine(int(lanes_inner_left), int(y),
                           int(lanes_inner_right), int(y))
                p.setPen(self.LABEL_COLOR)
                p.setFont(label_font)
                box = QRect(0, int(y) - 8, int(lanes_inner_left) - 4, 16)
                p.drawText(box, Qt.AlignRight | Qt.AlignVCenter, str(i + 1))
            end_y = base_y - chart_px
            p.setPen(self.MEASURE_LINE)
            p.drawLine(int(lanes_inner_left), int(end_y),
                       int(lanes_inner_right), int(end_y))

            for pos, fret in self._kicks:
                border = self._borders.get(fret)
                color = border if border is not None else self.KICK_COLOR
                p.setPen(QPen(color, 2))
                y = base_y - pos * self.PX_PER_WHOLE
                p.drawLine(int(lanes_inner_left), int(y),
                           int(lanes_inner_right), int(y))

            size = self.NOTE_SIZE
            half = size / 2
            for pos, fret, lane, is_cymbal, x_offset in self._hits:
                y = base_y - pos * self.PX_PER_WHOLE
                x = lane_x[lane] + x_offset
                p.setBrush(self.LANE_COLOR[lane])
                border = self._borders.get(fret)
                if border is not None:
                    p.setPen(QPen(border, 2))
                else:
                    p.setPen(QPen(self.NOTE_OUTLINE, 1))
                if is_cymbal:
                    tri = QPolygonF([
                        QPointF(x, y - half),
                        QPointF(x - half, y + half),
                        QPointF(x + half, y + half),
                    ])
                    p.drawPolygon(tri)
                else:
                    p.drawRect(int(x - half), int(y - half), size, size)
        finally:
            p.end()


class MappingPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._loaded = False
        self._combos: list[tuple[int, QComboBox]] = []
        self._symbols: dict[int, QLabel] = {}
        self.setTitle("Map drums to Clone Hero pads")
        self.setSubTitle("Each drum in this track is auto-mapped to a CH pad. "
                         "Override any row, or pick \"— Remove —\" to drop it.")

        outer = QVBoxLayout(self)
        self.status = QLabel("Fetching notes…")
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        outer.addWidget(self.progress)

        content = QHBoxLayout()
        content.setSpacing(8)
        outer.addLayout(content, 1)

        map_scroll = QScrollArea()
        map_scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._grid = QVBoxLayout(self._container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(2)
        self._grid.addStretch(1)
        map_scroll.setWidget(self._container)
        map_scroll.setFixedWidth(470)
        content.addWidget(map_scroll)

        self.preview = ChartPreview()
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setFixedWidth(220)
        self._preview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preview_scroll.setWidget(self.preview)
        content.addWidget(self._preview_scroll)

        warn_box = QWidget()
        warn_v = QVBoxLayout(warn_box)
        warn_v.setContentsMargins(4, 0, 4, 0)
        warn_v.setSpacing(4)
        warn_title = QLabel("<b>Mapping warnings</b>")
        warn_v.addWidget(warn_title)
        self._warn_empty = QLabel("No issues detected ✓")
        self._warn_empty.setStyleSheet("color:#7a9;")
        warn_v.addWidget(self._warn_empty)
        self._lane_section = _CollapsibleSection(
            "Simultaneous notes in single lane")
        self._stack_section = _CollapsibleSection(
            "3+ non-kick notes at once")
        warn_v.addWidget(self._lane_section)
        warn_v.addWidget(self._stack_section)
        warn_v.addStretch(1)
        self._warn_scroll = QScrollArea()
        self._warn_scroll.setWidgetResizable(True)
        self._warn_scroll.setMinimumWidth(240)
        self._warn_scroll.setWidget(warn_box)
        content.addWidget(self._warn_scroll, 1)

    def initializePage(self) -> None:
        self._loaded = False
        self._combos.clear()
        self._symbols.clear()
        while self._grid.count() > 1:
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.preview.setSong(None)
        self.preview.setMapping({})
        self._lane_section.set_rows([])
        self._stack_section.set_rows([])
        self._warn_empty.setVisible(True)
        self.status.setText("Fetching notes…")
        self.progress.show()

        state = self.state

        def work():
            image = (state.meta.get("image")
                     or (state.meta.get("current") or {}).get("image"))
            notes = sfetch.fetch_notes(state.song_id, state.revision_id,
                                       image, state.part_id)
            song = parse_dict(notes)
            song.song_id = state.song_id
            song.revision_id = state.revision_id
            counts: dict[int, int] = {}
            for m in song.measures:
                for v in m.voices:
                    for b in v.beats:
                        for n in b.notes:
                            counts[n.fret] = counts.get(n.fret, 0) + 1
            duration = estimate_duration(song)
            return song, counts, duration

        def on_done(result):
            song, counts, duration = result
            state.song = song
            self.progress.hide()
            if not counts:
                self.status.setText("No drums found on this track.")
                return
            saved_mappings = cfg.load_mappings()
            self.status.setText(f"{len(counts)} distinct drums in this track.")
            for fret in sorted(counts, key=lambda f: -counts[f]):
                n = counts[fret]
                nps = n / duration if duration > 0 else 0.0
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                label = QLabel(
                    f"<b style='font-size:14pt'>{drum_name(fret)}</b>"
                    f"  <span style='color:#888'>({n} notes · {nps:.1f}/s)</span>"
                )
                label.setTextFormat(Qt.RichText)
                label.setMinimumWidth(220)
                label.setWordWrap(True)
                combo = QComboBox()
                combo.setIconSize(QSize(40, 40))
                combo.view().setIconSize(QSize(40, 40))
                combo.setItemDelegate(_VCenterDelegate(combo))
                combo.setFixedWidth(192)
                default_lane = SONGSTERR_TO_CH.get(fret)
                default_idx = 0
                saved_label = saved_mappings.get(fret)
                for i, (name, lane) in enumerate(LANE_OPTIONS):
                    icon = _lane_icon(lane)
                    if icon is not None:
                        combo.addItem(icon, name, lane)
                    else:
                        combo.addItem(name, lane)
                    combo.setItemData(i, Qt.AlignVCenter | Qt.AlignLeft,
                                      Qt.TextAlignmentRole)
                    if saved_label is None and default_lane is not None \
                            and lane is not None \
                            and lane.lane == default_lane.lane \
                            and lane.is_cymbal == default_lane.is_cymbal:
                        default_idx = i
                    if saved_label is not None and name == saved_label:
                        default_idx = i
                if saved_label is None and default_lane is None:
                    # Unknown fret → default to Remove
                    default_idx = len(LANE_OPTIONS) - 1
                combo.setCurrentIndex(default_idx)
                combo.currentIndexChanged.connect(
                    lambda _=None: self._update_preview())
                symbol = QLabel()
                symbol.setFixedSize(22, 22)
                symbol.setAlignment(Qt.AlignCenter)
                row.addWidget(symbol)
                row.addWidget(label)
                row.addWidget(combo)
                row.addStretch(1)
                holder = QWidget()
                holder.setLayout(row)
                self._grid.insertWidget(self._grid.count() - 1, holder)
                self._combos.append((fret, combo))
                self._symbols[fret] = symbol
            self.preview.setSong(song)
            self._update_preview()
            QTimer.singleShot(0, self._scroll_preview_to_start)
            self._loaded = True
            self.completeChanged.emit()

        def on_failed(msg):
            self.progress.hide()
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")

        self._emitter = run_async(self, work, on_done, on_failed)

    def _current_mapping(self) -> dict:
        mapping = {}
        for fret, combo in self._combos:
            lane = combo.currentData()
            if lane is not None:
                mapping[fret] = lane
        return mapping

    def _compute_borders(self, mapping: dict) -> dict:
        """Walk frets in note-count order; extra drums on the same CH lane
        cycle through _BORDER_PALETTE so the preview can tell them apart.
        Filters palette entries matching the lane's own fill color so the
        border never blends into the shape."""
        borders: dict = {}
        lane_counts: dict = {}
        for fret, _ in self._combos:
            lane = mapping.get(fret)
            if lane is None:
                continue
            key = (lane.lane, lane.is_cymbal)
            idx = lane_counts.get(key, 0)
            lane_counts[key] = idx + 1
            own_fill = (ChartPreview.KICK_COLOR if lane.lane == KICK
                        else ChartPreview.LANE_COLOR[lane.lane])
            own_rgb = own_fill.rgb()
            palette = [c for c in _BORDER_PALETTE
                       if c is None or c.rgb() != own_rgb]
            borders[fret] = palette[idx % len(palette)]
        return borders

    def _update_preview(self) -> None:
        mapping = self._current_mapping()
        borders = self._compute_borders(mapping)
        self.preview.setBorders(borders)
        self.preview.setMapping(mapping)
        for fret, symbol in self._symbols.items():
            lane = mapping.get(fret)
            if lane is None:
                symbol.clear()
                continue
            icon = _instrument_icon(lane, borders.get(fret))
            symbol.setPixmap(icon.pixmap(22, 22))
        self._refresh_warnings(mapping, borders)

    def _collect_warnings(self, mapping: dict):
        """Walk the song once grouping mapped hits by (measure, position).
        Returns (lane_collisions, stacking) where each is a list of
        (measure_number_1based, [fret, fret, …]) tuples."""
        song = self.state.song
        if song is None:
            return [], []
        by_tick: dict = {}  # (m_idx, Fraction pos) -> {fret: Lane}
        for m_idx, measure in enumerate(song.measures):
            for voice in measure.voices:
                voice_pos = Fraction(0)
                grace_buf: list = []
                for beat in voice.beats:
                    if beat.grace:
                        grace_buf.append(beat)
                        continue
                    trailing = Fraction(0)
                    for i in range(len(grace_buf) - 1, -1, -1):
                        trailing += grace_buf[i].duration
                        g = grace_buf[i]
                        g_pos = max(Fraction(0), voice_pos - trailing)
                        if not g.rest:
                            bucket = by_tick.setdefault((m_idx, g_pos), {})
                            for n in g.notes:
                                lane = mapping.get(n.fret)
                                if lane is not None:
                                    bucket[n.fret] = lane
                    grace_buf.clear()
                    if not beat.rest:
                        bucket = by_tick.setdefault((m_idx, voice_pos), {})
                        for n in beat.notes:
                            lane = mapping.get(n.fret)
                            if lane is not None:
                                bucket[n.fret] = lane
                    voice_pos += beat.duration

        lane_collisions: list = []
        stacking: list = []
        for (m_idx, _), fret_to_lane in sorted(by_tick.items()):
            by_lane: dict = {}
            for fret, lane in fret_to_lane.items():
                by_lane.setdefault((lane.lane, lane.is_cymbal), []).append(fret)
            for frets in by_lane.values():
                if len(frets) >= 2:
                    lane_collisions.append((m_idx + 1, frets))
            non_kick = [f for f, lane in fret_to_lane.items()
                        if lane.lane != KICK]
            if len(non_kick) >= 3:
                stacking.append((m_idx + 1, non_kick))
        return lane_collisions, stacking

    def _refresh_warnings(self, mapping: dict, borders: dict) -> None:
        lane_collisions, stacking = self._collect_warnings(mapping)
        self._lane_section.set_rows([
            _warning_row(m, frets, mapping, borders)
            for m, frets in lane_collisions
        ])
        self._stack_section.set_rows([
            _warning_row(m, frets, mapping, borders)
            for m, frets in stacking
        ])
        self._warn_empty.setVisible(
            not lane_collisions and not stacking)

    def _scroll_preview_to_start(self) -> None:
        bar = self._preview_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def isComplete(self) -> bool:
        return self._loaded

    def validatePage(self) -> bool:
        self.state.mapping = self._current_mapping()
        return True


class AlignmentPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._loaded = False
        self.setTitle("Select audio alignment")
        self.setSubTitle("Songsterr publishes per-measure sync tables for various "
                         "YouTube audio sources.")

        outer = QVBoxLayout(self)
        self.status = QLabel("Loading alignments…")
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        outer.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        self._vbox.addStretch(1)
        scroll.setWidget(self._container)
        outer.addWidget(scroll)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(lambda _: self.completeChanged.emit())

        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_btn = QPushButton("Copy YouTube URL")
        self.copy_btn.clicked.connect(self._copy_url)
        row.addWidget(self.copy_btn)
        outer.addLayout(row)

    def _copy_url(self):
        idx = self._group.checkedId()
        if idx < 0 or idx >= len(self.state.alignments):
            return
        vid = self.state.alignments[idx].get("videoId") or ""
        QApplication.clipboard().setText(f"https://youtu.be/{vid}")
        self.status.setText(f"Copied https://youtu.be/{vid}")

    def initializePage(self) -> None:
        self._loaded = False
        for btn in list(self._group.buttons()):
            self._group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.status.setText("Loading alignments…")
        self.progress.show()
        song_id = self.state.song_id
        rev_id = self.state.revision_id

        def work():
            records = vp.fetch(song_id, rev_id)
            return [r for r in records
                    if r.get("status") == "done" and r.get("points")]

        def on_done(done):
            self.progress.hide()
            if not done:
                self.state.alignments = []
                self.status.setText("No alignments available for this tab.")
                return
            default_orig = next((i for i, r in enumerate(done)
                                 if r.get("feature") is None), 0)
            ordered = sorted(
                enumerate(done),
                key=lambda it: (0 if it[0] == default_orig else 1, it[0]),
            )
            self.state.alignments = [r for _, r in ordered]
            default_btn = None
            for new_idx, (orig_idx, r) in enumerate(ordered):
                vid = r.get("videoId") or ""
                marker = "   ★ site default" if orig_idx == default_orig else ""
                btn = QRadioButton(f"youtu.be/{vid}{marker}")
                self._group.addButton(btn, new_idx)
                self._vbox.insertWidget(self._vbox.count() - 1, btn)
                if orig_idx == default_orig and default_btn is None:
                    default_btn = btn
            if default_btn is not None:
                default_btn.setChecked(True)
            elif self._group.buttons():
                self._group.buttons()[0].setChecked(True)
            self.status.setText(f"{len(done)} alignment(s) available.")
            self._loaded = True
            self.completeChanged.emit()

        def on_failed(msg):
            self.progress.hide()
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")

        self._emitter = run_async(self, work, on_done, on_failed)

    def isComplete(self) -> bool:
        return self._loaded and self._group.checkedId() >= 0

    def validatePage(self) -> bool:
        idx = self._group.checkedId()
        if idx < 0:
            return False
        self.state.alignment = self.state.alignments[idx]
        return True


class OutputPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self.setTitle("Output")
        self.setSubTitle("Choose where to write the Clone Hero song folder.")
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Export directory:"))
        row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.download_cb = QCheckBox("Download audio via yt-dlp from the selected alignment")
        self.download_cb.setChecked(True)
        layout.addWidget(self.download_cb)

        exe_suffix = ".exe" if sys.platform == "win32" else ""
        self.ytdlp_row, self.ytdlp_edit = self._build_tool_row(
            layout, "yt-dlp", f"/path/to/yt-dlp{exe_suffix}",
            "https://github.com/yt-dlp/yt-dlp/releases",
        )
        self.ffmpeg_row, self.ffmpeg_edit = self._build_tool_row(
            layout, "ffmpeg", f"/path/to/ffmpeg{exe_suffix}",
            "https://github.com/BtbN/FFmpeg-Builds/releases",
        )

        self.download_cb.toggled.connect(self._refresh_tool_rows)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch(1)

        warning = QLabel(
            "<b style='color:#c60;'>⚠ Heads up:</b> "
            "Generated charts are transcribed one-for-one from Songsterr tabs "
            "and may contain errors, missing sections, or timing inaccuracies. "
            "Always review and fine-tune in Moonscraper before considering a "
            "chart done."
        )
        warning.setTextFormat(Qt.RichText)
        warning.setWordWrap(True)
        warning.setMaximumWidth(700)
        layout.addWidget(warning)

        ack_row = QHBoxLayout()
        self.ack_cb = QCheckBox()
        self.ack_cb.toggled.connect(lambda _: self.completeChanged.emit())
        ack_label = QLabel(
            "I understand the outputted chart may have inaccuracies. "
            "It is intended for personal use and I will not blindly share "
            "it with the community."
        )
        ack_label.setStyleSheet("font-weight: bold;")
        ack_label.setWordWrap(True)
        ack_label.setFixedWidth(680)
        ack_label.mousePressEvent = lambda _e: self.ack_cb.toggle()
        ack_row.addWidget(self.ack_cb, 0, Qt.AlignTop)
        ack_row.addWidget(ack_label)
        ack_row.addStretch(1)
        layout.addLayout(ack_row)

    def _build_tool_row(self, layout, tool_name: str, placeholder: str, download_url: str):
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(
            f'{tool_name} not found on PATH — browse to it '
            f'(<a href="{download_url}">download</a>):'
        )
        lbl.setStyleSheet("color: #c60;")
        lbl.setTextFormat(Qt.RichText)
        lbl.setOpenExternalLinks(True)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(lambda _: self.completeChanged.emit())
        btn = QPushButton("Browse…")
        btn.clicked.connect(lambda: self._browse_tool(edit, tool_name))
        row.addWidget(lbl)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        layout.addWidget(row_w)
        row_w.setVisible(False)
        return row_w, edit

    def _browse(self):
        start = str(Path(self.dir_edit.text()).expanduser()) if self.dir_edit.text() else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose export directory", start)
        if d:
            self.dir_edit.setText(_tildify(d))

    def _browse_tool(self, edit: QLineEdit, tool_name: str):
        f, _ = QFileDialog.getOpenFileName(
            self, f"Locate {tool_name} executable", str(Path.home()))
        if f:
            edit.setText(_tildify(f))

    def _refresh_tool_rows(self):
        want = self.download_cb.isChecked()
        ytdlp_missing = not _tool_works(self.ytdlp_edit.text().strip() or "yt-dlp")
        ffmpeg_missing = not _tool_works(self.ffmpeg_edit.text().strip() or "ffmpeg")
        self.ytdlp_row.setVisible(want and ytdlp_missing)
        self.ffmpeg_row.setVisible(want and ffmpeg_missing)
        self.completeChanged.emit()

    def initializePage(self) -> None:
        saved = cfg.load()

        default_name = self.state.song_name or f"song-{self.state.song_id}"
        if self.state.artist:
            default_name = f"{self.state.artist} - {default_name}"
        if not self.dir_edit.text():
            parent = saved.get("export_parent") or str(Path.cwd() / "exported")
            self.dir_edit.setText(_tildify(str(Path(parent).expanduser() / default_name)))

        if not self.ytdlp_edit.text() and saved.get("ytdlp_path"):
            self.ytdlp_edit.setText(_tildify(saved["ytdlp_path"]))
        if not self.ffmpeg_edit.text() and saved.get("ffmpeg_path"):
            self.ffmpeg_edit.setText(_tildify(saved["ffmpeg_path"]))

        a = self.state.alignment
        vid = a.get("videoId") if a else "?"
        self.info.setText(
            f"Song: {self.state.artist} — {self.state.song_name}\n"
            f"Alignment: youtu.be/{vid}"
        )
        self._refresh_tool_rows()

    def isComplete(self) -> bool:
        if not self.dir_edit.text().strip():
            return False
        if self.ytdlp_row.isVisible() and not self.ytdlp_edit.text().strip():
            return False
        if self.ffmpeg_row.isVisible() and not self.ffmpeg_edit.text().strip():
            return False
        if not self.ack_cb.isChecked():
            return False
        return True

    def validatePage(self) -> bool:
        out_path = Path(self.dir_edit.text().strip()).expanduser()
        self.state.output_dir = str(out_path)
        self.state.download_audio = self.download_cb.isChecked()
        self.state.ytdlp_path = (
            str(Path(self.ytdlp_edit.text().strip()).expanduser())
            if self.ytdlp_edit.text().strip() else "yt-dlp"
        )
        self.state.ffmpeg_path = (
            str(Path(self.ffmpeg_edit.text().strip()).expanduser())
            if self.ffmpeg_edit.text().strip() else "ffmpeg"
        )
        cfg.save({
            "ytdlp_path": self.state.ytdlp_path if self.state.ytdlp_path != "yt-dlp" else "",
            "ffmpeg_path": self.state.ffmpeg_path if self.state.ffmpeg_path != "ffmpeg" else "",
            "export_parent": str(out_path.parent),
        })
        return True


class RunPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._done = False
        self.setTitle("Generating chart")
        self.setSubTitle("Fetching notes, aligning tempo, writing output.")
        layout = QVBoxLayout(self)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

    def _append(self, msg: str):
        self.log.appendPlainText(_tildify(msg))

    def initializePage(self) -> None:
        self._done = False
        self.log.clear()
        QTimer.singleShot(0, self._step_fetch_notes)

    def _step(self, delay_ms: int, fn):
        QTimer.singleShot(delay_ms, fn)

    def _fail(self, stage: str):
        self._append(f"Failed during {stage}:")
        self._append(traceback.format_exc())

    def _step_fetch_notes(self):
        state = self.state
        self._append(f"Track ready ({len(state.song.measures)} measures, "
                     f"{len(state.mapping)} drums mapped)")
        self._step(0, self._step_render)

    def _step_render(self):
        state = self.state
        a = state.alignment
        self._append(f"Aligning to youtu.be/{a['videoId']} "
                     f"feature={a.get('feature') or 'primary'} "
                     f"({len(a['points'])} points)…")
        try:
            points = a["points"]
            tempos_override, vp_delay = vp.derive_tempos(
                state.song, points, TICKS_PER_BEAT)
            self._vp_delay = vp_delay
            self._append(f"  delay={vp_delay:.3f}s")

            out_dir = Path(state.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            self._out_dir = out_dir
            chart_path = out_dir / "notes.chart"
            self._append(f"Writing {_tildify(str(chart_path))}…")
            summary = render(
                state.song, str(chart_path),
                name=state.song_name, artist=state.artist,
                charter="Chartster", tempos_override=tempos_override,
                mapping=state.mapping,
            )
            self._append(f"  {summary['hits']} notes, "
                         f"{summary['tempo_changes']} tempo changes, "
                         f"{summary['time_sig_changes']} time-sig changes")
            if summary["hand_warnings"]:
                self._append(f"  Warning: {summary['hand_warnings']} tick(s) "
                             "with >2 simultaneous hand notes")
        except Exception:
            return self._fail("render")
        self._step(0, self._step_ini)

    def _step_ini(self):
        try:
            _write_song_ini(self._out_dir / "song.ini",
                            self.state.song_name, self.state.artist,
                            delay_ms=int(round(self._vp_delay * 1000)))
            self._append(f"Wrote {_tildify(str(self._out_dir / 'song.ini'))}")
            readme = self._out_dir / "README.txt"
            _write_readme(readme, self.state)
            self._append(f"Wrote {_tildify(str(readme))}")
        except Exception:
            return self._fail("song.ini")
        if self.state.download_audio:
            self._step(0, self._step_download)
        else:
            self._finish()

    def _step_download(self):
        vid = self.state.alignment["videoId"]
        url = f"https://www.youtube.com/watch?v={vid}"
        dst = self._out_dir / "song.mp3"
        self._append(f"Downloading audio via yt-dlp from youtu.be/{vid}…")
        try:
            popen_kwargs = dict(
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            cmd = [self.state.ytdlp_path, "-x", "--audio-format", "mp3",
                   "--audio-quality", "0", "--no-playlist", "--newline",
                   "-o", str(dst.with_suffix(".%(ext)s"))]
            if self.state.ffmpeg_path and self.state.ffmpeg_path != "ffmpeg":
                cmd += ["--ffmpeg-location", self.state.ffmpeg_path]
            cmd.append(url)
            self._proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            self._append("  yt-dlp not installed — skipping.")
            return self._finish()

        import queue
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        def reader():
            try:
                for line in iter(self._proc.stdout.readline, ""):
                    self._log_queue.put(line)
            finally:
                self._log_queue.put("")  # sentinel
        threading.Thread(target=reader, daemon=True).start()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(150)
        self._poll_timer.timeout.connect(self._poll_download)
        self._poll_timer.start()

    def _poll_download(self):
        import queue
        proc = self._proc
        try:
            while True:
                line = self._log_queue.get_nowait()
                if line:
                    self._append("  " + line.rstrip())
        except queue.Empty:
            pass
        if proc.poll() is None:
            return
        self._poll_timer.stop()
        if proc.returncode == 0:
            self._append(f"  audio saved to {_tildify(str(self._out_dir / 'song.mp3'))}")
        else:
            self._append(f"  yt-dlp exited {proc.returncode}")
        self._finish()

    def _finish(self):
        self._append("\nDone.")
        self._done = True
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._done


def _tool_works(path: str) -> bool:
    """True if `path -version` / `path --version` runs successfully."""
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    for flag in ("--version", "-version"):
        try:
            if subprocess.run([path, flag], **kwargs).returncode == 0:
                return True
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
    return False


def _tildify(s: str) -> str:
    """Replace any $HOME occurrence with '~' for display."""
    home = str(Path.home())
    return s.replace(home, "~") if home else s


def _ytdlp_download(video_id: str, out_dir: Path) -> Optional[str]:
    dst = out_dir / "song.mp3"
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
             "--no-playlist", "-o", str(dst.with_suffix(".%(ext)s")), url],
            check=True, capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return str(dst) if dst.exists() else None


def _write_song_ini(path: Path, name: str, artist: str,
                    delay_ms: int = 0, charter: str = "Chartster") -> None:
    lines = [
        "[song]",
        f"name = {name}",
        f"artist = {artist}",
        f"charter = {charter}",
        "diff_drums = -1",
        "preview_start_time = 0",
    ]
    if delay_ms:
        lines.append(f"delay = {delay_ms}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_readme(path: Path, state: "State") -> None:
    vid = state.alignment.get("videoId") if state.alignment else None
    yt = f"https://www.youtube.com/watch?v={vid}" if vid else "(none)"
    lines = [
        "Generated by Chartster",
        "",
        f"Songsterr URL:  {state.url}",
        f"songId:         {state.song.song_id}",
        f"revisionId:     {state.song.revision_id}",
        f"YouTube (sync): {yt}",
        "",
        "Mappings:",
    ]
    for fret in sorted(state.mapping):
        lane = state.mapping[fret]
        lines.append(f"  {drum_name(fret)} (id: {fret}) -> {_lane_label(lane)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Wizard(QWizard):
    def __init__(self):
        super().__init__()
        self.state = State()
        self.setWindowTitle("Chartster")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.NoCancelButton, True)
        self.resize(1040, 620)
        self.addPage(UrlPage(self.state))
        self.addPage(TrackPage(self.state))
        self.addPage(MappingPage(self.state))
        self.addPage(AlignmentPage(self.state))
        self.addPage(OutputPage(self.state))
        self.addPage(RunPage(self.state))
        self.setButtonText(QWizard.FinishButton, "Chart another")

    def accept(self):
        # Instead of closing, reset state and restart at page 0 so the user
        # can chart another song without relaunching.
        defaults = State()
        for f in dataclasses.fields(State):
            setattr(self.state, f.name, getattr(defaults, f.name))
        # Clear OutputPage widgets that don't get re-derived from state.
        for p in self.pageIds():
            page = self.page(p)
            if isinstance(page, OutputPage):
                page.dir_edit.clear()
                page.ack_cb.setChecked(False)
        self.restart()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            btn = self.button(QWizard.NextButton)
            if btn.isVisible() and btn.isEnabled():
                btn.click()
                return
            fbtn = self.button(QWizard.FinishButton)
            if fbtn.isVisible() and fbtn.isEnabled():
                fbtn.click()
                return
        super().keyPressEvent(event)


def main() -> None:
    cfg.bootstrap_if_missing()
    app = QApplication(sys.argv)
    w = Wizard()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

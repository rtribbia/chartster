"""PySide6 wizard GUI for chartster — happy path from a Songsterr URL."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import traceback
import urllib.request
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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QButtonGroup,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
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
from . import lyrics as lyrics_mod
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
_trimmed_pm_cache: dict = {}


def _trim_transparent(pm: QPixmap) -> QPixmap:
    """Crop transparent border rows/columns from a QPixmap."""
    if pm.isNull():
        return pm
    img = pm.toImage()
    w, h = img.width(), img.height()
    top = 0
    while top < h and all(img.pixelColor(x, top).alpha() == 0
                          for x in range(w)):
        top += 1
    bottom = h - 1
    while bottom > top and all(img.pixelColor(x, bottom).alpha() == 0
                               for x in range(w)):
        bottom -= 1
    left = 0
    while left < w and all(img.pixelColor(left, y).alpha() == 0
                           for y in range(top, bottom + 1)):
        left += 1
    right = w - 1
    while right > left and all(img.pixelColor(right, y).alpha() == 0
                               for y in range(top, bottom + 1)):
        right -= 1
    if left == 0 and top == 0 and right == w - 1 and bottom == h - 1:
        return pm
    return pm.copy(left, top, right - left + 1, bottom - top + 1)


def _lane_pixmap_trimmed(lane, size: int) -> Optional[QPixmap]:
    """Scaled, transparency-trimmed pixmap for a lane gif — cached per size."""
    if lane is None or lane.lane == KICK:
        icon = _lane_icon(lane)
        return icon.pixmap(size, size) if icon else None
    fname = _LANE_ASSET.get((lane.lane, lane.is_cymbal))
    if not fname:
        return None
    key = (fname, size)
    if key in _trimmed_pm_cache:
        return _trimmed_pm_cache[key]
    icon = _lane_icon(lane)
    if icon is None:
        return None
    pm = icon.pixmap(128, 128)
    trimmed = _trim_transparent(pm).scaled(
        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _trimmed_pm_cache[key] = trimmed
    return trimmed


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


class _SortableItem(QTableWidgetItem):
    """QTableWidgetItem whose < comparison uses a hidden sort key (e.g. an
    ISO date) instead of the displayed text."""

    def __init__(self, display: str, sort_key: str):
        super().__init__(display)
        self._sort_key = sort_key

    def __lt__(self, other: "QTableWidgetItem") -> bool:
        other_key = getattr(other, "_sort_key", None)
        if other_key is None:
            return super().__lt__(other)
        return self._sort_key < other_key


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
    chart_dynamics: bool = True
    dynamics_enabled: dict = field(default_factory=dict)
    lyric_candidates: dict = field(default_factory=dict)  # partId -> notes JSON
    vocal_part_id: Optional[int] = None  # None = skip lyrics
    lyrics: list = field(default_factory=list)            # [(tick, syllable)]
    phrase_ranges: list = field(default_factory=list)     # [(start_tick, end_tick)]
    alignments: list = field(default_factory=list)
    alignment: Optional[dict] = None
    album_art_bytes: Optional[bytes] = None
    album_art_video_id: Optional[str] = None
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

        self.history_label = QLabel("")
        self.history_label.setStyleSheet("color: #888;")
        layout.addWidget(self.history_label)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["Date", "Artist", "Song", "URL"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.setSortingEnabled(True)
        self.history_table.setTextElideMode(Qt.ElideRight)
        self.history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_table.setWordWrap(False)
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setStretchLastSection(True)
        self.history_table.itemClicked.connect(self._on_history_click)
        layout.addWidget(self.history_table, 1)
        self._bottom_spacer = QWidget()
        self._bottom_spacer.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout.addWidget(self._bottom_spacer, 1)
        self._refresh_history()

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
        self._refresh_history()

    def _refresh_history(self) -> None:
        entries = cfg.load_history()
        self.history_table.setSortingEnabled(False)
        self.history_table.clearContents()
        self.history_table.setRowCount(0)
        if not entries:
            self.history_label.setText("No chart history yet.")
            self.history_table.setVisible(False)
            self._bottom_spacer.setVisible(True)
            self.history_table.setSortingEnabled(True)
            return
        self.history_table.setVisible(True)
        self._bottom_spacer.setVisible(False)
        self.history_label.setText(
            f"Recent charts ({len(entries)}):")
        self.history_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            iso = entry.get("date", "") or ""
            try:
                y, m, d = iso.split("-")
                pretty_date = f"{int(m):02d}/{int(d):02d}/{y}"
            except Exception:
                pretty_date = iso
            url = entry.get("url", "")
            date_item = _SortableItem(pretty_date, iso)
            artist_item = QTableWidgetItem(entry.get("artist", ""))
            title_item = QTableWidgetItem(entry.get("title", ""))
            url_item = QTableWidgetItem(url)
            for it in (date_item, artist_item, title_item, url_item):
                it.setData(Qt.UserRole, url)
                it.setToolTip(url)
            self.history_table.setItem(row, 0, date_item)
            self.history_table.setItem(row, 1, artist_item)
            self.history_table.setItem(row, 2, title_item)
            self.history_table.setItem(row, 3, url_item)
        self.history_table.setSortingEnabled(True)
        self.history_table.sortByColumn(0, Qt.DescendingOrder)

    def _on_history_click(self, item: QTableWidgetItem) -> None:
        url = item.data(Qt.UserRole) or ""
        if url:
            self.url_edit.setText(url)

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
                 borders: dict, winner: Optional[int] = None) -> QWidget:
    """A single warning-instance row: 'Measure N' text + instrument symbols,
    optionally followed by '(<winner-symbol> wins)'."""
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
    if winner is not None and mapping.get(winner) is not None:
        open_p = QLabel("(")
        open_p.setStyleSheet("color:#aaa;")
        row.addWidget(open_p)
        w_sym = QLabel()
        w_sym.setFixedSize(22, 22)
        w_sym.setAlignment(Qt.AlignCenter)
        w_sym.setPixmap(_instrument_icon(
            mapping[winner], borders.get(winner)).pixmap(22, 22))
        w_sym.setToolTip(drum_name(winner))
        row.addWidget(w_sym)
        wins = QLabel("wins)")
        wins.setStyleSheet("color:#aaa;")
        row.addWidget(wins)
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
    LYRIC_GUTTER = 24   # left gutter for lyric column when lyrics attached
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
    LYRIC_COLOR   = QColor("#ffffff")
    PHRASE_COLOR  = QColor("#5a6068")

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
        self._lyrics: list = []            # [(pos_whole, syllable)]
        self._phrase_marks: list = []      # [pos_whole] phrase-start positions
        self._dim_chart: bool = False      # fade non-lyric elements when set
        self.setAutoFillBackground(False)
        self.setMinimumWidth(self._base_width())

    def setSong(self, song) -> None:
        self.song = song
        self._rebuild()

    def setMapping(self, mapping: dict) -> None:
        self.mapping = mapping
        self._rebuild()

    def setBorders(self, borders: dict) -> None:
        self._borders = borders
        self.update()

    def setLyrics(self, events: list, phrase_starts: list) -> None:
        """Attach lyric syllables to the preview.

        events: [(pos_whole, syllable), ...]
        phrase_starts: [pos_whole, ...] — phrase boundaries to mark visually.
        Both positions are in the same fraction-of-whole-note unit the
        chart uses internally. The gutter is rendered on the LEFT of the
        lanes so it stays visible when the preview is width-constrained.
        """
        self._lyrics = list(events)
        self._phrase_marks = list(phrase_starts)
        self.setMinimumWidth(self._base_width())
        self.updateGeometry()
        self.update()

    def setDimChart(self, dim: bool) -> None:
        """When True, draw notes/lanes/labels at 50% opacity so attached
        lyrics read as the page's primary content."""
        self._dim_chart = dim
        self.update()

    def _base_width(self) -> int:
        w = len(self._LANE_ORDER) * self.LANE_WIDTH + 2 * self.PAD_X + self.LABEL_MARGIN
        if self._lyrics:
            w += self.LYRIC_GUTTER
        return w

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
        return QSize(self._base_width(), max(200, h))

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

            lyric_gutter_w = self.LYRIC_GUTTER if self._lyrics else 0
            lanes_inner_left = lyric_gutter_w + self.LABEL_MARGIN
            lanes_inner_right = w - self.PAD_X
            lane_count = len(self._LANE_ORDER)
            inner_w = lanes_inner_right - lanes_inner_left
            lane_x = {
                lane: lanes_inner_left + (i + 0.5) * inner_w / lane_count
                for i, lane in enumerate(self._LANE_ORDER)
            }

            chart_alpha = 0.5 if (self._dim_chart and self._lyrics) else 1.0
            p.setOpacity(chart_alpha)
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
                box = QRect(int(lyric_gutter_w), int(y) - 8,
                            int(self.LABEL_MARGIN) - 4, 16)
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

            if self._lyrics:
                p.setOpacity(1.0)
                self._paint_lyrics(p, base_y)
        finally:
            p.end()

    def _paint_lyrics(self, p: QPainter, base_y: float) -> None:
        # Gutter occupies the leftmost LYRIC_GUTTER pixels (before
        # LABEL_MARGIN) so it stays visible when the preview is
        # width-constrained.
        gutter_left = 0
        gutter_right = self.LYRIC_GUTTER
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        p.setFont(font)
        # Faint horizontal divider at each phrase start.
        p.setPen(QPen(self.PHRASE_COLOR, 1, Qt.DashLine))
        for pos in self._phrase_marks:
            y = base_y - pos * self.PX_PER_WHOLE
            p.drawLine(int(gutter_left + 2), int(y),
                       int(gutter_right - 2), int(y))
        # Syllables rotated -90° read upward along the chart. Rotated
        # text occupies only ~font-height horizontally, so a tight gutter
        # keeps the column flush against the widget's left border.
        p.setPen(self.LYRIC_COLOR)
        for pos, syl in self._lyrics:
            y = base_y - pos * self.PX_PER_WHOLE
            p.save()
            p.translate(gutter_left + 16, y)
            p.rotate(-90)
            p.drawText(0, 4, syl)
            p.restore()


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
        self._tom_tom_section = _CollapsibleSection(
            "Note collision - tom/tom")
        self._cymbal_cymbal_section = _CollapsibleSection(
            "Note collision - cymbal/cymbal")
        self._tom_cymbal_section = _CollapsibleSection(
            "Note collision - tom/cymbal")
        self._stack_section = _CollapsibleSection(
            "3+ non-kick notes at once")
        warn_v.addWidget(self._tom_tom_section)
        warn_v.addWidget(self._cymbal_cymbal_section)
        warn_v.addWidget(self._tom_cymbal_section)
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
        self._tom_tom_section.set_rows([])
        self._cymbal_cymbal_section.set_rows([])
        self._tom_cymbal_section.set_rows([])
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
        Returns (tom_tom, cymbal_cymbal, tom_cymbal, stacking). The collision
        lists contain (measure_number_1based, [frets…], winner_fret) tuples
        where the winner is the fret whose note survives in the chart output
        (cymbal > tom; lowest fret number among ties)."""
        song = self.state.song
        if song is None:
            return [], [], [], []
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

        tom_tom: list = []
        cymbal_cymbal: list = []
        tom_cymbal: list = []
        stacking: list = []
        for (m_idx, _), fret_to_lane in sorted(by_tick.items()):
            by_lane_kind: dict = {}
            by_lane_only: dict = {}
            for fret, lane in fret_to_lane.items():
                if lane.lane == KICK:
                    continue
                by_lane_kind.setdefault(
                    (lane.lane, lane.is_cymbal), []).append(fret)
                by_lane_only.setdefault(lane.lane, set()).add(lane.is_cymbal)
            for (lane_int, is_cymbal), frets in by_lane_kind.items():
                if len(frets) >= 2:
                    entry = (m_idx + 1, sorted(frets), min(frets))
                    (cymbal_cymbal if is_cymbal else tom_tom).append(entry)
            for lane_int, kinds in by_lane_only.items():
                if len(kinds) == 2:
                    frets = [f for f, lane in fret_to_lane.items()
                             if lane.lane == lane_int]
                    cymbal_frets = [f for f in frets
                                    if fret_to_lane[f].is_cymbal]
                    winner = min(cymbal_frets)
                    tom_cymbal.append((m_idx + 1, sorted(frets), winner))
            non_kick = [f for f, lane in fret_to_lane.items()
                        if lane.lane != KICK]
            if len(non_kick) >= 3:
                stacking.append((m_idx + 1, sorted(non_kick)))
        return tom_tom, cymbal_cymbal, tom_cymbal, stacking

    def _refresh_warnings(self, mapping: dict, borders: dict) -> None:
        tom_tom, cymbal_cymbal, tom_cymbal, stacking = \
            self._collect_warnings(mapping)
        self._tom_tom_section.set_rows([
            _warning_row(m, frets, mapping, borders, winner=w)
            for m, frets, w in tom_tom
        ])
        self._cymbal_cymbal_section.set_rows([
            _warning_row(m, frets, mapping, borders, winner=w)
            for m, frets, w in cymbal_cymbal
        ])
        self._tom_cymbal_section.set_rows([
            _warning_row(m, frets, mapping, borders, winner=w)
            for m, frets, w in tom_cymbal
        ])
        self._stack_section.set_rows([
            _warning_row(m, frets, mapping, borders)
            for m, frets in stacking
        ])
        self._warn_empty.setVisible(
            not tom_tom and not cymbal_cymbal
            and not tom_cymbal and not stacking)

    def _scroll_preview_to_start(self) -> None:
        bar = self._preview_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def isComplete(self) -> bool:
        return self._loaded

    def validatePage(self) -> bool:
        self.state.mapping = self._current_mapping()
        return True


def _scan_dynamics(song, mapping) -> list:
    """Return sorted list of ((lane, is_cymbal, kind), count) for every combo
    whose mapped notes produce ghost or accent output in the rendered chart."""
    from .mapping import classify_velocity
    counts: dict = {}
    for measure in song.measures:
        for voice in measure.voices:
            for beat in voice.beats:
                if beat.rest:
                    continue
                for note in beat.notes:
                    lane = mapping.get(note.fret)
                    if lane is None:
                        continue
                    v = beat.velocity
                    if note.ghost:
                        v = max(1, v - 50)
                    elif note.accent == 1:
                        v = min(127, v + 20)
                    elif note.accent == 2:
                        v = max(1, v - 50)
                    kind = classify_velocity(v)
                    if kind != "normal":
                        key = (lane.lane, lane.is_cymbal, kind)
                        counts[key] = counts.get(key, 0) + 1
    order = {RED: 0, YELLOW: 1, BLUE: 2, GREEN: 3, KICK: 4}
    return sorted(counts.items(),
                  key=lambda item: (order.get(item[0][0], 9),
                                    item[0][1], item[0][2]))


class DynamicsPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self.setTitle("Dynamics")
        self.setSubTitle("Choose which ghost and accent notes to keep. "
                         "Unchecked combos become normal notes.")
        outer = QVBoxLayout(self)

        self._empty_label = QLabel(
            "No ghost or accent notes were detected in the mapped track.")
        self._empty_label.setStyleSheet("color: #888;")
        self._empty_label.setVisible(False)
        outer.addWidget(self._empty_label)

        self.enable_cb = QCheckBox(
            "Enable chart dynamics in Clone Hero")
        self.enable_cb.setChecked(True)
        self.enable_cb.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.enable_cb)

        self._hint = QLabel(
            "Without this, Clone Hero ignores ghost/accent markers entirely.")
        self._hint.setStyleSheet("color: #888;")
        outer.addWidget(self._hint)

        self._btn_row = QWidget()
        btn_row = QHBoxLayout(self._btn_row)
        btn_row.setContentsMargins(0, 0, 0, 0)
        self.check_all_btn = QPushButton("Check all")
        self.uncheck_all_btn = QPushButton("Uncheck all")
        self.check_all_btn.clicked.connect(lambda: self._set_all(True))
        self.uncheck_all_btn.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self.check_all_btn)
        btn_row.addWidget(self.uncheck_all_btn)
        btn_row.addStretch(1)
        outer.addWidget(self._btn_row)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._rows_layout = QVBoxLayout(self._container)
        self._rows_layout.setContentsMargins(4, 4, 4, 4)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)
        outer.addStretch(1)

        self._row_checks: dict = {}

    def _set_all(self, value: bool) -> None:
        self.enable_cb.setChecked(value)
        for cb in self._row_checks.values():
            cb.setChecked(value)

    def initializePage(self) -> None:
        # Clear any previous rows (mapping may have changed).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._row_checks.clear()

        combos = _scan_dynamics(self.state.song, self.state.mapping) \
            if self.state.song is not None else []

        self.enable_cb.setChecked(self.state.chart_dynamics)

        for combo, count in combos:
            lane_int, is_cymbal, kind = combo
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)

            icon_label = QLabel()
            icon_label.setFixedSize(28, 28)
            icon_label.setAlignment(Qt.AlignCenter)
            from .mapping import Lane
            lane_obj = Lane(lane_int, is_cymbal)
            pm = _lane_pixmap_trimmed(lane_obj, 28)
            if pm is not None:
                icon_label.setPixmap(pm)
            row.addWidget(icon_label)

            cb = QCheckBox()
            existing = self.state.dynamics_enabled.get(combo)
            cb.setChecked(existing if existing is not None else True)
            row.addWidget(cb)

            noun = "note" if count == 1 else "notes"
            lbl = QLabel(
                f"<b>{_lane_label(lane_obj)}</b> "
                f"<span>— {count} {kind} {noun}</span>"
            )
            lbl.setTextFormat(Qt.RichText)
            row.addWidget(lbl)
            row.addStretch(1)

            lbl.mousePressEvent = lambda _e, c=cb: c.toggle()

            self._rows_layout.insertWidget(self._rows_layout.count() - 1, w)
            self._row_checks[combo] = cb

        has_combos = bool(combos)
        self._empty_label.setVisible(not has_combos)
        self.enable_cb.setVisible(has_combos)
        self._hint.setVisible(has_combos)
        self._btn_row.setVisible(has_combos)
        self._scroll.setVisible(has_combos)

    def validatePage(self) -> bool:
        if self._row_checks:
            self.state.chart_dynamics = self.enable_cb.isChecked()
        else:
            self.state.chart_dynamics = True
        self.state.dynamics_enabled = {
            combo: cb.isChecked() for combo, cb in self._row_checks.items()
        }
        return True


_VOCAL_INSTRUMENT_RE = re.compile(
    r"^(soprano|alto|tenor|baritone|bass)\s+sax(?:ophone)?$", re.I)
_VOCAL_NAME_RE = re.compile(
    r"\b(vocal|vocals|voice|lead vox|backing|choir|lyrics|singer)\b", re.I)


def _is_vocal_candidate(track: dict) -> bool:
    inst = (track.get("instrument") or "").strip()
    name = (track.get("name") or track.get("title") or "").strip()
    if _VOCAL_INSTRUMENT_RE.match(inst):
        return True
    if _VOCAL_NAME_RE.search(name):
        return True
    return False


class LyricsTrackPage(QWizardPage):
    """Pick which vocal track's lyrics to embed (or skip lyrics entirely).

    Candidates are pre-filtered by instrument/name heuristic, then their
    notes JSON is fetched in parallel to confirm `withLyrics: true` before
    they're shown.
    """
    # Distinct from Qt's "no selection" sentinel (-1) and from any partId.
    SKIP_ID = -2

    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._loaded = False
        self._preview_page_id = -1
        self._post_lyrics_page_id = -1
        self.setTitle("Lyrics")
        self.setSubTitle(
            "Songsterr stores lyrics on vocal tracks. Pick one to include "
            "synced lyrics in the chart, or skip.")

        outer = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
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
        self._loaded = False
        self.state.lyric_candidates = {}
        self.state.vocal_part_id = None

        candidates = [t for t in self.state.tracks if _is_vocal_candidate(t)]
        if not candidates:
            self._show_skip_only("Songsterr has no vocal tracks for this song.")
            return

        self.status.setText(f"Checking {len(candidates)} vocal track(s) for lyrics…")
        self.progress.show()
        state = self.state
        image = (state.meta.get("image")
                 or (state.meta.get("current") or {}).get("image"))

        def work():
            results: list[tuple[dict, dict | None, str | None]] = []
            for t in candidates:
                pid = t.get("partId")
                try:
                    notes = sfetch.fetch_notes(state.song_id, state.revision_id,
                                               image, pid)
                except Exception as e:
                    results.append((t, None, str(e)))
                    continue
                if lyrics_mod.has_lyrics(notes):
                    results.append((t, notes, None))
                else:
                    results.append((t, None, None))
            return results

        def on_done(results):
            self.progress.hide()
            confirmed = [(t, notes) for t, notes, _ in results if notes]
            if not confirmed:
                self._show_skip_only(
                    "None of the vocal tracks have lyrics on Songsterr.")
                return
            self.state.lyric_candidates = {
                t.get("partId"): notes for t, notes in confirmed}
            self.status.setText(
                f"{len(confirmed)} vocal track(s) with lyrics. "
                "Pick one to include, or skip.")
            for t, notes in confirmed:
                pid = t.get("partId")
                inst = t.get("instrument") or ""
                name = t.get("name") or t.get("title") or ""
                events, _ = lyrics_mod.walk(notes)
                label = f"{name or inst} — {len(events)} syllables"
                btn = QRadioButton(label)
                self._group.addButton(btn, pid)
                self._vbox.insertWidget(self._vbox.count() - 1, btn)
            skip_btn = QRadioButton("Skip lyrics")
            self._group.addButton(skip_btn, self.SKIP_ID)
            self._vbox.insertWidget(self._vbox.count() - 1, skip_btn)
            self._group.buttons()[0].setChecked(True)
            self._loaded = True
            self.completeChanged.emit()

        def on_failed(msg):
            self.progress.hide()
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")
            self._show_skip_only("")

        self._emitter = run_async(self, work, on_done, on_failed)

    def _show_skip_only(self, msg: str) -> None:
        if msg:
            self.status.setText(msg)
        skip_btn = QRadioButton("Skip lyrics")
        self._group.addButton(skip_btn, self.SKIP_ID)
        self._vbox.insertWidget(self._vbox.count() - 1, skip_btn)
        skip_btn.setChecked(True)
        self._loaded = True
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._loaded and self._group.checkedButton() is not None

    def validatePage(self) -> bool:
        cid = self._group.checkedId()
        if cid == self.SKIP_ID or cid not in self.state.lyric_candidates:
            self.state.vocal_part_id = None
        else:
            self.state.vocal_part_id = cid
        return True

    def set_skip_target(self, preview_id: int, post_id: int) -> None:
        self._preview_page_id = preview_id
        self._post_lyrics_page_id = post_id

    def nextId(self) -> int:
        # Bypass the preview page entirely when no track was picked. Keeps
        # the back button working — clicking Back from AlignmentPage lands
        # back here, not on a self-skipping preview page.
        cid = self._group.checkedId()
        if cid == self.SKIP_ID or cid not in self.state.lyric_candidates:
            if self._post_lyrics_page_id >= 0:
                return self._post_lyrics_page_id
        return super().nextId()


class LyricsPreviewPage(QWizardPage):
    """Visualize the picked vocal track's syllables alongside the drum chart.

    Skipped automatically when no vocal track was chosen.
    """

    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self.setTitle("Preview lyrics")
        self.setSubTitle(
            "Lyric syllables run top-to-bottom alongside the drum lanes. "
            "Use Back to swap tracks or skip.")

        outer = QVBoxLayout(self)
        self.status = QLabel("")
        outer.addWidget(self.status)

        self.preview = ChartPreview()
        self.preview.setDimChart(True)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self.preview)
        # Constrain preview width to ~40% of the wizard so the lyric gutter
        # stays comfortably on-screen and the page doesn't feel chart-heavy.
        self._scroll.setMaximumWidth(420)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_row = QHBoxLayout()
        scroll_row.setContentsMargins(0, 0, 0, 0)
        scroll_row.addWidget(self._scroll)
        scroll_row.addStretch(1)
        outer.addLayout(scroll_row, 1)

    def initializePage(self) -> None:
        state = self.state
        # LyricsTrackPage.nextId() routes around this page when lyrics are
        # skipped, so reaching here means a vocal track was picked.
        notes = state.lyric_candidates.get(state.vocal_part_id)
        if notes is None:
            self.status.setText("Lyric data missing — going back.")
            QTimer.singleShot(0, lambda: self.wizard().back())
            return
        events, phrases = lyrics_mod.walk(notes)
        state.lyrics = events
        state.phrase_ranges = phrases
        self.status.setText(
            f"{len(events)} syllables across {len(phrases)} phrases.")
        # Convert tick positions → fraction-of-whole-note for the preview
        # widget (matches how existing notes are positioned).
        ticks_to_whole = 1.0 / (TICKS_PER_BEAT * 4.0)
        lyric_pos = [(t * ticks_to_whole, syl) for t, syl in events]
        phrase_pos = [start * ticks_to_whole for start, _ in phrases]
        self.preview.setSong(state.song)
        self.preview.setMapping(state.mapping)
        self.preview.setLyrics(lyric_pos, phrase_pos)
        QTimer.singleShot(0, self._scroll_to_start)

    def _scroll_to_start(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def validatePage(self) -> bool:
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


class AlbumArtPage(QWizardPage):
    """Pick a YouTube thumbnail to save as album.jpg, or skip."""
    SKIP_ID = -2
    THUMB_W = 240
    THUMB_H = 180

    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._loaded = False
        self._video_ids: list[str] = []
        self._thumbs: dict[str, bytes] = {}
        self.setTitle("Album art")
        self.setSubTitle(
            "Pick a YouTube thumbnail to save as album.jpg, or skip.")

        outer = QVBoxLayout(self)
        self.status = QLabel("")
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        outer.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self._container)
        outer.addWidget(scroll, 1)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(lambda _: self.completeChanged.emit())

    def initializePage(self) -> None:
        for btn in list(self._group.buttons()):
            self._group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._loaded = False
        self._thumbs = {}
        self._video_ids = []
        self.state.album_art_bytes = None
        self.state.album_art_video_id = None

        seen: list[str] = []
        for a in self.state.alignments or []:
            vid = a.get("videoId")
            if vid and vid not in seen:
                seen.append(vid)
        if not seen:
            self._show_skip_only(
                "No alignments — nothing to grab thumbnails from.")
            return

        self._video_ids = seen
        self.status.setText(f"Fetching {len(seen)} thumbnail(s)…")
        self.progress.show()

        def work():
            results: dict[str, bytes] = {}
            for vid in seen:
                url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
                try:
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=8) as r:
                        results[vid] = r.read()
                except Exception:
                    pass
            return results

        def on_done(results):
            self.progress.hide()
            self._thumbs = results
            self._build_grid()
            self.status.setText(
                f"{len(results)}/{len(seen)} thumbnail(s) fetched. "
                "Pick one or skip.")
            self._loaded = True
            self.completeChanged.emit()

        def on_failed(msg):
            self.progress.hide()
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")
            self._show_skip_only("")

        self._emitter = run_async(self, work, on_done, on_failed)

    def _build_grid(self) -> None:
        cols = 3
        picked_vid = (self.state.alignment or {}).get("videoId")
        default_btn = None
        cell_idx = 0
        for vid in self._video_ids:
            data = self._thumbs.get(vid)
            if data is None:
                continue
            pix = QPixmap()
            pix.loadFromData(data)
            if pix.isNull():
                continue
            pix = pix.scaled(
                self.THUMB_W, self.THUMB_H,
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(4)
            btn = QPushButton()
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setFixedSize(self.THUMB_W + 12, self.THUMB_H + 12)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { border: 2px solid #2b2d31; border-radius: 4px;"
                " padding: 2px; background: #1a1c1f; }"
                "QPushButton:hover { border-color: #5a6068; }"
                "QPushButton:checked { border: 2px solid #4ea1ff; }")
            self._group.addButton(btn, self._video_ids.index(vid))
            cell_layout.addWidget(btn, 0, Qt.AlignCenter)
            label = QLabel(f"youtu.be/{vid}")
            label.setStyleSheet("color: #888; font-size: 9pt;")
            label.setAlignment(Qt.AlignCenter)
            cell_layout.addWidget(label)
            self._grid.addWidget(cell, cell_idx // cols, cell_idx % cols)
            if vid == picked_vid and default_btn is None:
                default_btn = btn
            cell_idx += 1

        skip_btn = QRadioButton("Skip album art")
        self._group.addButton(skip_btn, self.SKIP_ID)
        skip_row = cell_idx // cols + 1
        self._grid.addWidget(skip_btn, skip_row, 0, 1, cols)

        if default_btn is not None:
            default_btn.setChecked(True)
        elif cell_idx > 0:
            # Pick the first thumbnail if none matched.
            for b in self._group.buttons():
                if self._group.id(b) != self.SKIP_ID:
                    b.setChecked(True)
                    break
        else:
            skip_btn.setChecked(True)

    def _show_skip_only(self, msg: str) -> None:
        if msg:
            self.status.setText(msg)
        skip_btn = QRadioButton("Skip album art")
        self._group.addButton(skip_btn, self.SKIP_ID)
        skip_btn.setChecked(True)
        self._grid.addWidget(skip_btn, 0, 0)
        self._loaded = True
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._loaded and self._group.checkedButton() is not None

    def validatePage(self) -> bool:
        cid = self._group.checkedId()
        if cid == self.SKIP_ID or cid < 0 or cid >= len(self._video_ids):
            self.state.album_art_bytes = None
            self.state.album_art_video_id = None
        else:
            vid = self._video_ids[cid]
            self.state.album_art_bytes = self._thumbs.get(vid)
            self.state.album_art_video_id = vid
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
        if out_path.exists():
            candidates = ["notes.chart", "song.ini", "README.txt"]
            if self.state.album_art_bytes:
                candidates.append("album.jpg")
            if self.download_cb.isChecked():
                candidates.append("song.mp3")
            existing = [n for n in candidates if (out_path / n).exists()]
            if existing:
                msg = (f"The folder <b>{out_path}</b> already exists and "
                       f"contains {len(existing)} file(s) that will be "
                       f"overwritten:<br><br>" +
                       "<br>".join(f"• {n}" for n in existing) +
                       "<br><br>Continue?")
            else:
                msg = (f"The folder <b>{out_path}</b> already exists. Any "
                       "existing files with the same names will be "
                       "overwritten. Continue?")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Overwrite existing folder?")
            box.setTextFormat(Qt.RichText)
            box.setText(msg)
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            if box.exec() != QMessageBox.Yes:
                return False
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
            dyn_enabled = {k for k, v in state.dynamics_enabled.items() if v}
            summary = render(
                state.song, str(chart_path),
                name=state.song_name, artist=state.artist,
                charter="Chartster", tempos_override=tempos_override,
                mapping=state.mapping,
                chart_dynamics=state.chart_dynamics,
                dynamics_enabled=dyn_enabled,
                lyrics=state.lyrics or None,
                phrase_ranges=state.phrase_ranges or None,
            )
            self._append(f"  {summary['hits']} notes, "
                         f"{summary['tempo_changes']} tempo changes, "
                         f"{summary['time_sig_changes']} time-sig changes")
            if summary["hand_warnings"]:
                self._append(f"  Warning: {summary['hand_warnings']} tick(s) "
                             "with >2 simultaneous hand notes")
            cfg.log_history(state.artist, state.song_name, state.url)
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
            if self.state.album_art_bytes:
                art = self._out_dir / "album.jpg"
                art.write_bytes(self.state.album_art_bytes)
                self._append(
                    f"Wrote {_tildify(str(art))} "
                    f"(youtu.be/{self.state.album_art_video_id})")
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
        self.addPage(DynamicsPage(self.state))
        lyric_track_page = LyricsTrackPage(self.state)
        lyric_preview_page = LyricsPreviewPage(self.state)
        self._lyric_track_id = self.addPage(lyric_track_page)
        self._lyric_preview_id = self.addPage(lyric_preview_page)
        self._alignment_id = self.addPage(AlignmentPage(self.state))
        self.addPage(AlbumArtPage(self.state))
        self.addPage(OutputPage(self.state))
        self.addPage(RunPage(self.state))
        # Wire LyricsTrackPage to skip the preview when "Skip lyrics" is picked.
        lyric_track_page.set_skip_target(
            self._lyric_preview_id, self._alignment_id)
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

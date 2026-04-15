"""PySide6 wizard GUI for chartster — happy path from a Songsterr URL."""

from __future__ import annotations

import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import threading

from PySide6.QtCore import QObject, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QCursor, QIcon, QMovie, QPixmap
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
    BLUE, GREEN, LANE_OPTIONS, RED, SONGSTERR_TO_CH, YELLOW, drum_name,
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
        return None
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


class MappingPage(QWizardPage):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._loaded = False
        self._combos: list[tuple[int, QComboBox]] = []
        self.setTitle("Map drums to Clone Hero pads")
        self.setSubTitle("Each drum in this track is auto-mapped to a CH pad. "
                         "Override any row, or pick \"— Remove —\" to drop it.")

        outer = QVBoxLayout(self)
        self.status = QLabel("Fetching notes…")
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        outer.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._grid = QVBoxLayout(self._container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(2)
        self._grid.addStretch(1)
        scroll.setWidget(self._container)
        outer.addWidget(scroll, 1)

    def initializePage(self) -> None:
        self._loaded = False
        self._combos.clear()
        while self._grid.count() > 1:
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
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
                label.setMinimumWidth(260)
                combo = QComboBox()
                combo.setIconSize(QSize(40, 40))
                combo.view().setIconSize(QSize(40, 40))
                default_lane = SONGSTERR_TO_CH.get(fret)
                default_idx = 0
                saved_label = saved_mappings.get(fret)
                for i, (name, lane) in enumerate(LANE_OPTIONS):
                    icon = _lane_icon(lane)
                    if icon is not None:
                        combo.addItem(icon, name, lane)
                    else:
                        combo.addItem(name, lane)
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
                row.addWidget(label)
                row.addWidget(combo, 1)
                holder = QWidget()
                holder.setLayout(row)
                self._grid.insertWidget(self._grid.count() - 1, holder)
                self._combos.append((fret, combo))
            self._loaded = True
            self.completeChanged.emit()

        def on_failed(msg):
            self.progress.hide()
            self.status.setText(f"Failed: {msg.strip().splitlines()[-1]}")

        self._emitter = run_async(self, work, on_done, on_failed)

    def isComplete(self) -> bool:
        return self._loaded

    def validatePage(self) -> bool:
        mapping = {}
        for fret, combo in self._combos:
            lane = combo.currentData()
            if lane is not None:
                mapping[fret] = lane
        self.state.mapping = mapping
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
        self.resize(780, 560)
        self.addPage(UrlPage(self.state))
        self.addPage(TrackPage(self.state))
        self.addPage(MappingPage(self.state))
        self.addPage(AlignmentPage(self.state))
        self.addPage(OutputPage(self.state))
        self.addPage(RunPage(self.state))

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

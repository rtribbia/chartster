# Chartster

Convert [Songsterr](https://www.songsterr.com) drum tabs into [Clone Hero](https://clonehero.net) expert drum charts, automatically aligned to a YouTube audio track.

If song sections and lyrics exist in songster, they will also be included and synced with the song.

https://github.com/user-attachments/assets/acdc7344-43a9-4fd8-9fef-3fbd0b7c1c34


### ⚠️ Disclaimer ⚠️

**The output of this tool is only as good as the input.** 

If the timing of notes, lyrics, etc is not aligned to the backing track in the songster UI when you play the song there, they will not be aligned in the chart this tool produces. Quality is variable on Songsterr so try different tabs or alignment tracks.

## Install

Two options: download a prebuilt release or run from source.

### Prebuilt releases (recommended)

Grab the latest `Chartster-macos.zip` or `Chartster-windows.zip` from the [Releases page](../../releases), unzip, and launch.

**macOS users:** the build is **unsigned**, so you'll see *"Chartster cannot be opened because the developer cannot be verified"* the first time. Two ways around it:
1. **Right-click → Open** on `Chartster.app`, then click **Open** in the warning dialog. macOS remembers this choice.
2. Or run from source (see below) — no signing needed.

**Windows users:** SmartScreen may warn on first launch. Click *More info → Run anyway*.

### From source

Requirements:
- Python ≥ `3.9.6`
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on `PATH` if you want audio download
- [`ffmpeg`](https://github.com/BtbN/FFmpeg-Builds/releases) on `PATH` (required by `yt-dlp` for MP3 extraction)

```bash
pip install -e '.[gui]'
```

The `[gui]` extra pulls in PySide6 (essentials).

#### Starting

```bash
chartster-gui
```

If `chartster-gui` isn't on your `PATH` (common with `pip install --user` or an inactive venv), run it as a module instead:

```bash
python3 -m chartster.gui
```

### Usage

Chartster is a step-by-step wizard:

1. **URL** — paste a Songsterr drum-tab URL.
2. **Track** — pick the drum track.
3. **Mapping** — pick which Clone Hero pad each drum lands on.
4. **Dynamics** — toggle which ghost/accent combos to keep.
5. **Lyrics track** — pick a vocal track to embed synced lyrics, or skip.
6. **Lyrics preview** — visualize the lyrics alongside the chart.
7. **Alignment** — pick a YouTube source to align the chart to.
8. **Album art** — pick a YouTube thumbnail for `album.jpg`, or skip.
9. **Output** — choose the export folder.
10. **Run** — render and write the song folder.

Output folder, ready to drop into your Clone Hero songs directory:

```
Metallica - Master of Puppets/
├── notes.chart
├── song.ini
├── README.txt
├── album.jpg          # if you picked a thumbnail
└── song.mp3           # if you enabled yt-dlp download
```


# Chartster

Convert [Songsterr](https://www.songsterr.com) drum tabs into [Clone Hero](https://clonehero.net) expert drum charts, automatically aligned to a YouTube audio track.

https://github.com/user-attachments/assets/79cd8d56-b1f6-4324-81bd-e133c5e96079

Given a Songsterr song URL, this tool:
1. Fetches the tab JSON
2. Lets you pick a drum track and map each Songsterr drum to a Clone Hero pad/cymbal
3. Uses Songsterr's per-measure video-point alignment to lock the chart to a specific YouTube video's timing
4. Downloads the audio via `yt-dlp`,
5. Writes a ready-to-play Clone Hero song folder (`notes.chart`, `song.ini`, `song.mp3`, `README.txt`).

TLDR: **Songsterr URL** -> `notes.chart` + `song.ini` +`song.mp3`

## What to expect

- **Only drum charts are supported.** Guitar/bass/vocal tracks are filtered out in the GUI.
- **Output quality depends on the source tab.** If the Songsterr tab is sparse or inaccurate, the chart will be too. chartster doesn't transcribe audio — it transcribes the tab one-for-one.
- **Sync uses Songsterr's published alignments.** Coverage varies per song. When multiple alignments exist, chartster defaults to the one Songsterr's own site uses, but you're able to pick.
- **Mappings are auto-set from GM defaults and are editable per-song** via the mapping screen. Exotic percussion (wood blocks, tambourine, etc.) gets a reasonable fallback but may want manual remapping.
- This is a happy-path tool, not a full editor. 

**Fine-tuning (BPM offsets, adding sections, etc.) still belongs in [Moonscraper](https://github.com/FireFox2000000/moonscraper-chart-editor).**

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

## Usage

```bash
chartster-gui
```

If `chartster-gui` isn't on your `PATH` (common with `pip install --user` or an inactive venv), run it as a module instead:

```bash
python3 -m chartster.gui
```

1. Paste a Songsterr URL
2. Pick a track
3. Confirm drum mappings 
4. Pick a youtube video to sync to
5. Choose an output folder. 
6. Import the outputted folder into Clone Hero or Moonscraper!

## Releasing (maintainers)

Tag a commit with `vX.Y.Z` and push the tag — GitHub Actions builds macOS + Windows bundles and attaches them to a new Release automatically.

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow lives in `.github/workflows/release.yml`. To test locally:

```bash
pip install -e '.[gui,build]'
pyinstaller Chartster.spec
# -> dist/Chartster.app (macOS) or dist/Chartster/ (Windows)
```

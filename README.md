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

## Setup

### Requirements
 * Python ≥ `3.9.6`. 
 * Requires `yt-dlp` on `PATH` if you want audio download.
    - https://github.com/yt-dlp/yt-dlp

### Installation

```bash
pip install -e '.[gui]'
```

The `[gui]` extra pulls in PySide6.

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

# VideoOrganizer

Seven people came back from a trip with 925 photos and videos in one shared
folder. No consistent naming, no consistent dates, HEIC files no browser would
display, Live Photos split into pairs, and no way to answer "where's that shot
of everyone on the boat?" without scrolling for twenty minutes.

VideoOrganizer turns that pile into a searchable library, then cuts a recap
video out of it.

```
vorg import drive          # or: vorg import zip *.zip / vorg import folder ~/pics
vorg tag                   # a local vision model describes every file
vorg search "sunset on the water"
vorg web                   # browse, queue, and export
vorg recap exports/recap-01
```

Everything runs locally. Your media never leaves the machine.

---

## What it does

**Files everything by contributor and capture date.** Reads the real capture
time out of EXIF, HEIC metadata, or the video container, and falls back to
parsing the filename when a camera wrote no metadata at all. Files land in
`organized/<person>/<YYYY-MM-DD>/`.

**Describes every file with a local vision model.** Each item's thumbnail goes
to [Ollama](https://ollama.com), which returns semantic tags and a
one-sentence description. Those land in SQLite and are indexed with FTS5, so
"kids playing in the water at sunset" finds the right photos.

**Browses in a real UI.** A local web app with thumbnails, filters by person,
date, and type, a lightbox, and a drag-to-reorder export queue. HEIC files are
converted to JPEG on the fly so the browser can actually show them, and Live
Photos play their motion half in place.

**Exports with attribution.** Every item renders to a uniform frame — blurred
fill behind, source centered, contributor's name watermarked in the corner — so
a mix of portrait phone video and landscape photos cuts together cleanly.

**Cuts a recap video.** Classifies each exported item by what its tags say it
shows, assigns it to a chapter, spends each chapter's time budget on a
representative spread, and concatenates with title cards. Near-duplicate shots
get less screen time, so a burst of twelve reads as one moment.

---

## Install

```bash
pip install "videoorganizer[web]"        # library + CLI + web UI
pip install "videoorganizer[web,drive]"  # ...plus Google Drive sync
```

Or from source:

```bash
git clone https://github.com/R2bEEaton/VideoOrganizer
cd VideoOrganizer
pip install -e ".[web,dev]"
```

**Requirements**

| | |
|---|---|
| Python | 3.10+ |
| [ffmpeg](https://ffmpeg.org) + ffprobe | on `PATH` — video thumbnails, dates, and all rendering |
| [Ollama](https://ollama.com) | only for `vorg tag`; `ollama pull llama3.2-vision` |

A vision model wants real VRAM. `llama3.2-vision` uses about 8.9 GB, so a 12 GB
card handles it comfortably. Without a GPU, tagging works but is slow — run it
overnight with `vorg tag` and stop worrying about it.

---

## Getting started

```bash
mkdir ~/trip-2026 && cd ~/trip-2026
vorg init
```

That writes `videoorganizer.toml` and an empty database. Open the config and set
two things before importing anything:

```toml
[tagging]
# This single line does more for tag quality than anything else in the file.
# A model told it is looking at a tropical vacation returns "snorkeling" and
# "catamaran"; an unprimed one returns "water" and "boat".
subject = "a week-long trip to the coast with friends"

[people]
# Map the account name a file arrives with to the name you want shown.
# Anyone not listed keeps their original name.
"somelogin123" = "Alex"
"jordan.rivera88" = "Jordan"
```

Then import, tag, and browse:

```bash
vorg import folder ~/Downloads/trip-photos --person-from-subfolder
vorg tag
vorg web
```

---

## Commands

| Command | What it does |
|---|---|
| `vorg init [dir]` | Create a config and database |
| `vorg import zip <archives...>` | Import from zip archives |
| `vorg import folder <dir>` | Import from a folder |
| `vorg import drive` | Sync from a shared Google Drive folder |
| `vorg tag` | Describe and tag with a local vision model |
| `vorg search <query>` | Search descriptions and tags |
| `vorg stats` | Library overview |
| `vorg thumbs` | Generate missing thumbnails |
| `vorg fix-dates` | Re-read capture times and refile |
| `vorg people` | Show contributors; re-apply the name map |
| `vorg export --ids ... --to <dir>` | Export a queue with watermarks |
| `vorg recap <export-dir>` | Build the recap video |
| `vorg web` | Start the browser UI |

Every command takes `--config` to point at a specific library. Without it,
VideoOrganizer searches upward from the current directory for
`videoorganizer.toml`, so working inside your library folder just works.

Add `--dry-run` to any import to see what *would* happen. Imports are
idempotent — rerunning one skips what is already there rather than duplicating
it.

### Importing

A zip from a shared cloud folder nests everything under the folder's own name,
so the contributor is the *second* path component
(`Trip 2026/Alex's Photos/IMG_1.HEIC` means Alex). That is the default. A zip
straight off one person's phone has no such structure:

```bash
vorg import zip alex-photos.zip --person Alex
```

Some cameras write a dead-clock date — every file stamped 2009-01-01 and
counting up from there. Shift them onto the real timeline, preserving their
relative order:

```bash
vorg import zip gopro.zip --person Alex --offset-days 6266 --offset-hours 5
```

### Google Drive

Drive is the only source that knows who actually *uploaded* a file, which is
what makes attribution possible for a folder everyone dumps into.

1. Create an OAuth **Desktop app** credential in the
   [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and enable the Drive API.
2. Save the JSON next to your config as `client_secret.json`.
3. Put the folder id — the last path segment of the Drive URL — in your config:

```toml
[drive]
folder_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
credentials = "client_secret.json"
token = "token.json"
```

```bash
vorg import drive --dry-run   # see what is new
vorg import drive             # download it
```

The first run opens a browser to authorize; the refreshed token is cached in
`token.json` after that. Scope is read-only — VideoOrganizer never writes to
your Drive.

### Making a recap

Define chapters in your config. A chapter can be pinned to a date range, to
content sections, or to neither:

```toml
[recap]
title = "Coast Trip"
subtitle = "2026"
default_chapter = "main"

[[recap.sections]]
name = "water"
keywords = ["beach", "ocean", "snorkeling", "boat", "swimming"]

[[recap.chapters]]
id = "arrival"
title = "Getting There"
dates = ["2026-03-01", "2026-03-02"]   # inclusive range
budget = 25.0                           # target seconds of screen time

[[recap.chapters]]
id = "main"
title = "The Week"
budget = 90.0
```

A date range wins over a section match, because "the day we drove to the coast"
is a stronger signal than "this photo happens to have a boat in it."

Then queue items in the web UI, export, and build:

```bash
vorg export --ids 12 45 78 --to exports/recap-01
vorg recap exports/recap-01
```

`vorg recap` writes `recap_video.mp4` plus `recap_analysis.json`, which shows
how every item was classified — the file to read when the cut picked something
strange.

---

## How it is put together

```
videoorganizer/
  config.py      TOML → Library object; every path and name lives here
  db.py          schema, additive migrations, FTS5 index
  media.py       capture-time chain, Live Photo pairing, dedupe naming
  thumbs.py      id-keyed thumbnail generation
  tagging.py     vision backend + tolerant JSON extraction
  search.py      FTS5 prefix search with LIKE fallback
  watermark.py   uniform-frame export rendering
  export.py      ordered queue → folder + manifest
  recap.py       classification, budgeting, ffmpeg timeline
  sources/       zips.py · folder.py · gdrive.py
  web/           Flask app + single-page frontend
  cli.py         the vorg command
```

Two details worth knowing, because both were bugs first:

**Thumbnails are keyed by database id, never by filename stem.** Two people's
phones produce `IMG_4335.HEIC` constantly, and a Live Photo is a `.HEIC` and a
`.MOV` sharing one stem. Only the id is unique.

**Live Photo pairing requires the same contributor and a capture time within a
day.** Matching on the stem alone pairs one person's still with a stranger's
unrelated clip, because sequential iPhone numbering collides across devices
constantly.

---

## Development

```bash
pip install -e ".[dev,web]"
pytest
```

The suite covers the pure logic — date parsing, the model-output JSON
extractor, recap classification and budgeting, config loading, search, and
import round-trips through a temp library. No network, no GPU, and no ffmpeg
required to run it.

---

## License

MIT

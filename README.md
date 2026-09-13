# 📺 M3U Playlist Generator — Pixeldrain + External M3U

Auto-generates organized **M3U playlists** from:
- 📁 **Pixeldrain folders** (`/l/` links)
- 📄 **Pixeldrain single files** (`/u/` links)
- 🌐 **External M3U playlists**

Runs **every hour** via GitHub Actions and auto-commits updates.

---

## 📂 Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── update-playlists.yml    ← Hourly cron + manual trigger
│
├── input/                          ← YOUR SOURCE LISTS
│   ├── folder.txt                  ← Pixeldrain folder links
│   ├── folder_s.txt                ← Pixeldrain single-file links
│   └── playlist.txt                ← External M3U URLs
│
├── output/                         ← AUTO-GENERATED (do not edit)
│   ├── All.m3u                     ← Combined playlist
│   ├── Blovers.m3u                 ← Per-group playlist
│   ├── ECG.m3u
│   └── Kamadesi.m3u
│
├── scripts/
│   └── generate_playlists.py       ← Main generator
│
├── requirements.txt
├── .gitignore
└── README.md
```

---

## ⚡ Quick Start

### 1️⃣ Fork or Create This Repo

Copy all files from this repository into a new GitHub repository, keeping the exact folder structure shown above.

### 2️⃣ Enable Workflow Write Permissions

Go to your repository:

**Settings → Actions → General → Workflow permissions**

Select ✅ **Read and write permissions** → **Save**

### 3️⃣ Edit Your Source Files

Open the files in the `input/` folder and add your sources:

- `input/folder.txt` → Pixeldrain folder links
- `input/folder_s.txt` → Pixeldrain single-file links
- `input/playlist.txt` → External M3U URLs

### 4️⃣ Trigger the Workflow

- Automatic: Runs every hour ⏰
- Manual: **Actions** tab → **Update M3U Playlists** → **Run workflow** 🚀

Playlists appear in the `output/` folder after the first successful run.

---

## 📝 Input File Format

Every input file uses the **same format**:

```
GROUP NAME - URL
```

### Rules

| Rule | Example |
|------|---------|
| Split on the **first** ` - ` (space-dash-space) | `Group - https://...` |
| Group names **may contain hyphens** | `My-Drama-Show - https://...` → group = `My-Drama-Show` |
| Blank lines ignored | |
| Lines starting with `#` are comments | `# my comment` |
| One source per line | |

### Examples

**`input/folder.txt`** (Pixeldrain folders):
```
Blovers - https://pixeldrain.com/l/isE9wov4
My-Drama-Collection - https://pixeldrain.com/l/XxYyZzAa
```

**`input/folder_s.txt`** (Pixeldrain single files):
```
ECG - https://pixeldrain.com/u/UnL4ozgo
Trailer - https://pixeldrain.com/u/AbCdEfGh
```

**`input/playlist.txt`** (External M3U URLs):
```
Kamadesi - https://raw.githubusercontent.com/user/repo/main/playlist.m3u
News Channels - https://example.com/news.m3u
```

---

## 📤 Output

### Per-Group Playlist (e.g., `output/Blovers.m3u`)

```m3u
#EXTM3U
#EXTINF:-1 tvg-name="Episode 1" tvg-logo="https://pixeldrain.com/api/file/abc/thumbnail" group-title="Blovers",Episode 1
https://pixeldrain.com/api/file/abc
#EXTINF:-1 tvg-name="Episode 2 4K" tvg-logo="https://pixeldrain.com/api/file/def/thumbnail" group-title="Blovers [2160P]",Episode 2 4K
https://pixeldrain.com/api/file/def
```

### Combined Playlist (`output/All.m3u`)

Contains **all entries** with source-type tags:

| Source Type      | Example `group-title`      |
|------------------|----------------------------|
| Pixeldrain Folder| `Blovers`                  |
| Pixeldrain Single| `ECG [Single]`             |
| External M3U     | `Kamadesi [M3U]`           |
| 4K (any source)  | `Blovers [2160P]`          |

---

## 🎯 Features

### ✅ Video-Only Filtering
Skips images, audio, documents, archives — only playable video files are added.

### ✅ Automatic 4K Detection
Detects `2160p`, `4K`, `UHD`, `3840x2160`, `4096x2160`, etc. from filenames. Anything **> 1420p vertical** is placed into a `[2160P]` sub-group.

**Recognized formats:**
- `2160p` / `2160P` / `4K` / `UHD`
- `1440p` / `2K`
- `1080p` / `FHD`
- `720p` / `HD`
- `480p` / `360p` / `240p`
- Explicit sizes: `3840x2160`, `1920x1080`, etc.

### ✅ Thumbnails
Automatically extracts:
- **Pixeldrain:** `https://pixeldrain.com/api/file/{id}/thumbnail`
- **External M3U:** preserves `tvg-logo` from source

### ✅ Duplicate Prevention
Entries are deduplicated by stream URL hash.

### ✅ Error Resilience
- Automatic retries with backoff (3 attempts)
- Per-source error isolation — one bad source won't stop others
- Timeouts on all requests
- Handles deleted/unavailable files gracefully
- Full Unicode filename support

### ✅ Smart Commits
Only commits & pushes when playlists actually change.

---

## 🔌 Using the Playlists

Direct raw URLs to your generated files (replace `USER/REPO`):

```
https://raw.githubusercontent.com/USER/REPO/main/output/All.m3u
https://raw.githubusercontent.com/USER/REPO/main/output/Blovers.m3u
https://raw.githubusercontent.com/USER/REPO/main/output/ECG.m3u
https://raw.githubusercontent.com/USER/REPO/main/output/Kamadesi.m3u
```

Compatible with:
- 📱 **IPTV Smarters**, **TiviMate**, **OTT Navigator**
- 💻 **VLC**, **MPV**, **Kodi**
- Any standard M3U/IPTV player

---

## 🛠️ Local Testing

```bash
# Clone your repo
git clone https://github.com/USER/REPO.git
cd REPO

# Install dependencies
pip install -r requirements.txt

# Run generator
python scripts/generate_playlists.py

# Check output
ls -la output/
cat output/All.m3u
```

---

## 🔧 Pixeldrain API Reference

| Purpose | Endpoint |
|---------|----------|
| Folder file listing | `GET https://pixeldrain.com/api/list/{id}` |
| File metadata | `GET https://pixeldrain.com/api/file/{id}/info` |
| Stream URL | `https://pixeldrain.com/api/file/{id}` |
| Thumbnail | `https://pixeldrain.com/api/file/{id}/thumbnail` |

---

## ⚙️ Workflow Details

The GitHub Actions workflow (`.github/workflows/update-playlists.yml`):

- ⏰ **Schedule:** every hour (`0 * * * *`)
- 🖱️ **Manual trigger:** available in Actions tab
- 🔄 **Auto-run on push** to `input/`, `scripts/`, or workflow file
- 🔒 **Concurrency lock:** prevents overlapping runs
- ⏱️ **Timeout:** 30 minutes
- 📊 **Summary report:** entry counts per playlist in Actions summary

---

## 📜 License

MIT — free to use, modify, and distribute.

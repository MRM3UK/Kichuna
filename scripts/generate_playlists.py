#!/usr/bin/env python3
"""
M3U Playlist Generator + Website Builder
Fetches videos from Pixeldrain and external M3U playlists,
generates M3U files and a single-page website dashboard at the root.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

FOLDER_TXT = INPUT_DIR / "folder.txt"
FOLDER_S_TXT = INPUT_DIR / "folder_s.txt"
PLAYLIST_TXT = INPUT_DIR / "playlist.txt"

PIXELDRAIN_API = "https://pixeldrain.com/api"
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 0.4
HIGH_RES_THRESHOLD = 1420

VIDEO_MIME_PREFIXES = ("video/",)
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".vob", ".3gp", ".3g2",
    ".mts", ".m2ts", ".divx", ".ogv", ".asf", ".f4v", ".rm",
    ".rmvb", ".mxf",
}
NON_VIDEO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
    ".ico", ".tiff", ".tif", ".heic", ".heif",
    ".mp3", ".aac", ".flac", ".ogg", ".wav", ".wma", ".m4a",
    ".opus", ".ape",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls",
    ".xlsx", ".ppt", ".pptx", ".epub",
    ".exe", ".msi", ".apk", ".iso", ".dmg", ".deb", ".rpm",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("m3u-gen")


# ============================================================
# HTTP SESSION
# ============================================================

def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3, backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"], raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (M3U-Playlist-Generator/2.0)",
        "Accept": "application/json, text/plain, */*",
    })
    return session


SESSION = build_session()


# ============================================================
# RESOLUTION DETECTION
# ============================================================

_RES_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:3840\s*[x\u00d7]\s*2160|4096\s*[x\u00d7]\s*2160|7680\s*[x\u00d7]\s*4320)", re.I), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])8[kK](?:[\s._\-\])]|$)"), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])4[kK](?:[\s._\-\])]|$)"), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])UHD(?:[\s._\-\])]|$)", re.I), "2160p"),
    (re.compile(r"\b4320[pPiI]\b"), "2160p"),
    (re.compile(r"\b2160[pPiI]\b"), "2160p"),
    (re.compile(r"2560\s*[x\u00d7]\s*1440", re.I), "1440p"),
    (re.compile(r"\b1440[pPiI]\b"), "1440p"),
    (re.compile(r"(?:^|[\s._\-\[(])2[kK](?:[\s._\-\])]|$)"), "1440p"),
    (re.compile(r"1920\s*[x\u00d7]\s*1080", re.I), "1080p"),
    (re.compile(r"\b1080[pPiI]\b"), "1080p"),
    (re.compile(r"(?:^|[\s._\-\[(])FHD(?:[\s._\-\])]|$)", re.I), "1080p"),
    (re.compile(r"1280\s*[x\u00d7]\s*720", re.I), "720p"),
    (re.compile(r"\b720[pPiI]\b"), "720p"),
    (re.compile(r"854\s*[x\u00d7]\s*480", re.I), "480p"),
    (re.compile(r"\b480[pPiI]\b"), "480p"),
    (re.compile(r"\b360[pPiI]\b"), "360p"),
    (re.compile(r"\b240[pPiI]\b"), "240p"),
]


def detect_resolution(text: str) -> str:
    if not text:
        return ""
    for pattern, label in _RES_PATTERNS:
        if pattern.search(text):
            return label
    return ""


def resolution_pixels(res: str) -> int:
    m = re.match(r"(\d+)", res or "")
    return int(m.group(1)) if m else 0


def is_high_res(resolution: str) -> bool:
    return resolution_pixels(resolution) > HIGH_RES_THRESHOLD


# ============================================================
# HELPERS
# ============================================================

_UNSAFE_TITLE_RE = re.compile(r"[\x00-\x1f\x7f\r\n]")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(name: str) -> str:
    if not name:
        return ""
    cleaned = _UNSAFE_TITLE_RE.sub("", name)
    cleaned = cleaned.replace('"', "'")
    return cleaned.strip()


def sanitize_filename(name: str) -> str:
    safe = _UNSAFE_FILENAME_RE.sub("_", name).strip(". ")
    return safe or "Unnamed"


def strip_extension(filename: str) -> str:
    return Path(filename).stem if filename else ""


def is_video_by_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def is_video_by_mime(mime: str) -> bool:
    return any(mime.lower().startswith(p) for p in VIDEO_MIME_PREFIXES) if mime else False


def is_definitely_not_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in NON_VIDEO_EXTENSIONS


def unique_key(stream_url: str) -> str:
    return hashlib.sha256(stream_url.strip().lower().encode()).hexdigest()


# ============================================================
# ENTRY
# ============================================================

def make_entry(
    title: str, stream_url: str, thumbnail_url: str = "",
    group: str = "", resolution: str = "", source_type: str = "",
    file_size: int = 0, mime_type: str = "", pd_file_id: str = "",
    original_filename: str = "",
) -> dict:
    return {
        "title": title, "stream_url": stream_url,
        "thumbnail_url": thumbnail_url, "group": group,
        "resolution": resolution, "source_type": source_type,
        "file_size": file_size, "mime_type": mime_type,
        "pd_file_id": pd_file_id, "original_filename": original_filename,
    }


# ============================================================
# INPUT PARSER
# ============================================================

def parse_input_file(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if not path.exists():
        log.warning("Input file missing: %s", path)
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        idx = line.find(" - ")
        if idx == -1:
            log.warning("  %s:%d missing separator: %s", path.name, lineno, line)
            continue
        group = line[:idx].strip()
        url = line[idx + 3:].strip()
        if group and url:
            entries.append((group, url))
    log.info("Parsed %d source(s) from %s", len(entries), path.name)
    return entries


# ============================================================
# PIXELDRAIN
# ============================================================

def extract_pd_file_id(url: str) -> Optional[str]:
    m = re.search(r"pixeldrain\.com/(?:u|api/file)/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def extract_pd_folder_id(url: str) -> Optional[str]:
    m = re.search(r"pixeldrain\.com/(?:l|api/list)/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def pd_stream_url(file_id: str) -> str:
    return f"{PIXELDRAIN_API}/file/{file_id}"


def pd_thumbnail_url(file_id: str) -> str:
    return f"{PIXELDRAIN_API}/file/{file_id}/thumbnail"


def pd_api_get(endpoint: str) -> Optional[dict]:
    url = f"{PIXELDRAIN_API}{endpoint}"
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            log.warning("  404 Not Found: %s", url)
            return None
        if resp.status_code != 200:
            log.warning("  HTTP %d for %s", resp.status_code, url)
            return None
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("  Timeout: %s", url)
    except requests.exceptions.ConnectionError as e:
        log.error("  Connection error: %s (%s)", url, e)
    except requests.exceptions.JSONDecodeError:
        log.error("  Invalid JSON: %s", url)
    except requests.RequestException as e:
        log.error("  Request failed: %s (%s)", url, e)
    finally:
        time.sleep(RATE_LIMIT_DELAY)
    return None


def pd_file_to_entry(file_info: dict, group: str, source_type: str) -> Optional[dict]:
    if not file_info:
        return None
    name = file_info.get("name", "") or ""
    mime = file_info.get("mime_type", "") or ""
    file_id = file_info.get("id", "")
    file_size = file_info.get("size", 0) or 0
    if not file_id:
        return None
    if is_definitely_not_video(name):
        return None
    if not (is_video_by_mime(mime) or is_video_by_extension(name)):
        return None

    title = safe_title(strip_extension(name)) or file_id
    resolution = detect_resolution(name)

    return make_entry(
        title=title, stream_url=pd_stream_url(file_id),
        thumbnail_url=pd_thumbnail_url(file_id),
        group=group, resolution=resolution, source_type=source_type,
        file_size=file_size, mime_type=mime, pd_file_id=file_id,
        original_filename=name,
    )


def process_pixeldrain_folder(group: str, url: str) -> list[dict]:
    folder_id = extract_pd_folder_id(url)
    if not folder_id:
        log.error("Invalid folder URL: %s", url)
        return []
    data = pd_api_get(f"/list/{folder_id}")
    if not data:
        return []
    files = data.get("files", [])
    log.info("  Folder [%s]: %d file(s)", folder_id, len(files))
    entries = []
    for f in files:
        entry = pd_file_to_entry(f, group, source_type="Folder")
        if entry:
            entries.append(entry)
    log.info("  '%s' -> %d video(s)", group, len(entries))
    return entries


def process_pixeldrain_single(group: str, url: str) -> list[dict]:
    file_id = extract_pd_file_id(url)
    if not file_id:
        log.error("Invalid file URL: %s", url)
        return []
    data = pd_api_get(f"/file/{file_id}/info")
    if not data:
        return []
    entry = pd_file_to_entry(data, group, source_type="Single")
    if entry:
        log.info("  '%s' -> %s", group, entry["title"])
        return [entry]
    log.info("  '%s' -> not video, skipped", group)
    return []


# ============================================================
# EXTERNAL M3U PARSER — FIXED POSTER/LOGO EXTRACTION
# ============================================================

_EXTINF_FULL_RE = re.compile(
    r'#EXTINF\s*:\s*(-?\d+)\s*(.*?)\s*,\s*(.*)',
    re.DOTALL,
)
_M3U_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_M3U_ATTR_SQ_RE = re.compile(r"([\w-]+)\s*=\s*'([^']*)'")


def parse_m3u_attributes(attr_str: str) -> dict[str, str]:
    if not attr_str:
        return {}
    attrs = dict(_M3U_ATTR_RE.findall(attr_str))
    if not attrs:
        attrs = dict(_M3U_ATTR_SQ_RE.findall(attr_str))
    return attrs


def extract_thumbnail_from_extinf(attr_str: str, attrs: dict[str, str]) -> str:
    logo_keys = [
        "tvg-logo", "tvg-logo-small", "logo", "poster", "thumbnail",
        "icon", "tvg-icon", "channel-logo", "cover", "art", "artwork"
    ]
    for key in logo_keys:
        val = attrs.get(key, "").strip()
        if val and val.startswith(("http://", "https://")):
            return val

    for key in logo_keys:
        pattern = re.compile(rf'{re.escape(key)}\s*=\s*"([^"]+)"', re.IGNORECASE)
        m = pattern.search(attr_str)
        if m:
            val = m.group(1).strip()
            if val.startswith(("http://", "https://")):
                return val
        pattern_sq = re.compile(rf"{re.escape(key)}\s*=\s*'([^']+)'", re.IGNORECASE)
        m = pattern_sq.search(attr_str)
        if m:
            val = m.group(1).strip()
            if val.startswith(("http://", "https://")):
                return val
    return ""


def looks_like_video_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    ext = Path(urlparse(url.lower()).path).suffix
    return ext not in NON_VIDEO_EXTENSIONS


def process_external_m3u(group: str, url: str) -> list[dict]:
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content = resp.text
    except requests.RequestException as e:
        log.error("Failed to fetch M3U '%s': %s", url, e)
        return []

    lines = content.splitlines()
    entries: list[dict] = []
    pending_extinf: Optional[str] = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            pending_extinf = line
            continue
        if line.startswith("#"):
            continue

        stream_url = line
        if not looks_like_video_url(stream_url):
            pending_extinf = None
            continue

        title = ""
        thumbnail = ""

        if pending_extinf:
            m = _EXTINF_FULL_RE.match(pending_extinf)
            if m:
                attr_str = m.group(2) or ""
                title = (m.group(3) or "").strip()
                attrs = parse_m3u_attributes(attr_str)
                thumbnail = extract_thumbnail_from_extinf(attr_str, attrs)
                if not title:
                    title = attrs.get("tvg-name", "")
            pending_extinf = None

        if not title:
            path = unquote(urlparse(stream_url).path)
            title = strip_extension(Path(path).name) or "Untitled"

        title = safe_title(title)
        resolution = detect_resolution(title) or detect_resolution(stream_url)

        pd_id = extract_pd_file_id(stream_url)
        file_size = 0
        mime_type = ""
        if pd_id:
            if not thumbnail:
                thumbnail = pd_thumbnail_url(pd_id)
            info = pd_api_get(f"/file/{pd_id}/info")
            if info:
                file_size = info.get("size", 0) or 0
                mime_type = info.get("mime_type", "") or ""
                fname = info.get("name", "")
                if fname and not resolution:
                    resolution = detect_resolution(fname)

        entries.append(make_entry(
            title=title, stream_url=stream_url,
            thumbnail_url=thumbnail, group=group,
            resolution=resolution, source_type="M3U",
            file_size=file_size, mime_type=mime_type,
            pd_file_id=pd_id or "", original_filename="",
        ))

    log.info("  '%s' -> %d video(s) from M3U", group, len(entries))
    return entries


# ============================================================
# M3U WRITER
# ============================================================

def build_group_title(entry: dict, combined: bool = False) -> str:
    group = entry["group"]
    res = entry["resolution"]
    src = entry["source_type"]
    if is_high_res(res):
        base = f"{group} [2160P]"
    else:
        base = group
    if combined:
        suffix = {"Single": " [Single]", "M3U": " [M3U]", "Folder": ""}
        base += suffix.get(src, "")
    return base


def format_extinf(entry: dict, combined: bool = False) -> str:
    title = entry["title"]
    logo = entry["thumbnail_url"]
    url = entry["stream_url"]
    group_title = build_group_title(entry, combined=combined)
    logo_attr = f' tvg-logo="{logo}"' if logo else ""
    extinf = (
        f'#EXTINF:-1 tvg-name="{title}"{logo_attr}'
        f' group-title="{group_title}",{title}'
    )
    return f"{extinf}\n{url}"


def write_m3u(filepath: Path, entries: list[dict], combined: bool = False) -> int:
    lines = ["#EXTM3U"]
    seen: set[str] = set()
    for entry in entries:
        key = unique_key(entry["stream_url"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(format_extinf(entry, combined=combined))
    content = "\n".join(lines) + "\n"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    log.info("  Wrote %d entries -> %s", len(seen), filepath.name)
    return len(seen)


def validate_m3u(filepath: Path) -> bool:
    if not filepath.exists():
        return False
    text = filepath.read_text(encoding="utf-8").strip()
    if not text.startswith("#EXTM3U"):
        log.error("  Invalid M3U (no #EXTM3U): %s", filepath.name)
        return False
    return True


# ============================================================
# WEBSITE BUILDER — 100% BULLETPROOF STRING REPLACEMENT
# ============================================================

def format_file_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def generate_website(
    all_entries: list[dict],
    group_entries: dict[str, list[dict]],
    playlist_files: dict[str, str],
    source_configs: dict[str, list[tuple[str, str]]],
    repo_base_url: str = "",
) -> str:
    update_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_channels = len(all_entries)
    total_groups = len(group_entries)

    channels_json = []
    seen: set[str] = set()
    for entry in all_entries:
        key = unique_key(entry["stream_url"])
        if key in seen:
            continue
        seen.add(key)
        channels_json.append({
            "title": entry["title"],
            "stream_url": entry["stream_url"],
            "thumbnail_url": entry["thumbnail_url"],
            "group": entry["group"],
            "resolution": entry["resolution"],
            "source_type": entry["source_type"],
            "file_size": entry["file_size"],
            "file_size_formatted": format_file_size(entry["file_size"]),
            "mime_type": entry["mime_type"],
            "pd_file_id": entry["pd_file_id"],
            "original_filename": entry["original_filename"],
            "group_title_combined": build_group_title(entry, combined=True),
        })

    playlists_json = []
    for name, filename in playlist_files.items():
        playlists_json.append({
            "name": name,
            "filename": filename,
            "count": len(group_entries.get(name, [])),
        })

    sources_json = {
        "folders": source_configs.get("folders", []),
        "singles": source_configs.get("singles", []),
        "playlists": source_configs.get("playlists", []),
    }

    # Clean standard HTML template (Zero f-string curly bracket conflicts!)
    template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<meta name="theme-color" content="#0f172a">
<meta name="description" content="M3U Playlist Dashboard">
<title>Playlist Dashboard</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg-primary: #0f172a;
  --bg-secondary: #1e293b;
  --bg-card: #1e293b;
  --bg-card-hover: #2a3a52;
  --bg-modal: #1e293b;
  --bg-input: #0f172a;
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --accent: #3b82f6;
  --accent-hover: #2563eb;
  --accent-light: rgba(59, 130, 246, 0.15);
  --success: #22c55e;
  --warning: #f59e0b;
  --danger: #ef4444;
  --border: #334155;
  --border-light: #475569;
  --shadow: 0 4px 6px -1px rgba(0,0,0,0.3);
  --shadow-lg: 0 10px 25px -5px rgba(0,0,0,0.4);
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --transition: 0.2s ease;
  --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
  --font-mono: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
}
body {
  font-family: var(--font);
  background: var(--bg-primary);
  color: var(--text-primary);
  min-height: 100vh;
  line-height: 1.5;
  overflow-x: hidden;
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); }
button {
  cursor: pointer;
  border: none;
  background: none;
  font-family: var(--font);
  font-size: inherit;
  color: inherit;
}
.icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}
.icon svg {
  width: 100%;
  height: 100%;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}
.header {
  background: linear-gradient(135deg, var(--bg-secondary) 0%, #1a2744 100%);
  border-bottom: 1px solid var(--border);
  padding: 20px 0;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(8px);
}
.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--text-primary);
}
.header-title .icon {
  width: 28px;
  height: 28px;
  color: var(--accent);
}
.header-stats {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.stat-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--accent-light);
  border-radius: 20px;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--accent);
  white-space: nowrap;
}
.stat-badge .icon { width: 14px; height: 14px; }
.update-time {
  font-size: 0.75rem;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  padding-top: 4px;
}
.update-time .icon { width: 12px; height: 12px; }
.tabs {
  display: flex;
  gap: 4px;
  padding: 12px 0;
  overflow-x: auto;
}
.tab-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  border-radius: var(--radius-sm);
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-secondary);
  background: transparent;
  transition: all var(--transition);
  white-space: nowrap;
}
.tab-btn:hover {
  color: var(--text-primary);
  background: var(--bg-secondary);
}
.tab-btn.active {
  color: var(--accent);
  background: var(--accent-light);
  border: 1px solid var(--accent);
}
.tab-count {
  background: var(--bg-primary);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 0.7rem;
}
.tab-btn.active .tab-count {
  background: rgba(59,130,246,0.3);
  color: var(--accent);
}
.toolbar {
  display: flex;
  gap: 8px;
  padding: 8px 0;
  flex-wrap: wrap;
  align-items: center;
}
.search-box {
  flex: 1;
  min-width: 200px;
  position: relative;
}
.search-box .icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
}
.search-box input {
  width: 100%;
  padding: 10px 12px 10px 38px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  outline: none;
}
.search-box input:focus { border-color: var(--accent); }
.filter-btn, .action-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 0.85rem;
  white-space: nowrap;
}
.filter-btn:hover, .action-btn:hover {
  color: var(--text-primary);
  border-color: var(--accent);
}
.filter-btn.active {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-light);
}
.action-btn.primary {
  background: var(--accent);
  color: white;
}
.select-all-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  color: var(--text-secondary);
  font-size: 0.85rem;
}
.channel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  padding: 8px 0 20px;
}
.channel-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  position: relative;
  transition: all var(--transition);
}
.channel-card:hover {
  border-color: var(--accent);
  transform: translateY(-2px);
}
.card-select {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 5;
  width: 20px;
  height: 20px;
}
.card-thumb {
  position: relative;
  width: 100%;
  padding-top: 56.25%;
  background: #0c1525;
}
.card-thumb img {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 100%;
  object-fit: cover;
}
.res-badge {
  position: absolute;
  top: 8px; right: 8px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.65rem;
  font-weight: bold;
}
.res-badge.res-2160p { background: #dc2626; color: white; }
.res-badge.res-1440p { background: #ea580c; color: white; }
.res-badge.res-1080p { background: #16a34a; color: white; }
.res-badge.res-720p { background: #2563eb; color: white; }
.card-body { padding: 12px; }
.card-title {
  font-size: 0.9rem;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-meta { display: flex; gap: 6px; margin: 6px 0 10px; flex-wrap: wrap; }
.card-tag {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.7rem;
}
.tag-group { background: rgba(59,130,246,0.15); color: var(--accent); }
.tag-source { background: rgba(34,197,94,0.15); color: var(--success); }
.card-actions { display: flex; gap: 4px; }
.card-btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px;
  border-radius: var(--radius-xs);
  font-size: 0.75rem;
  font-weight: 600;
}
.btn-play { background: var(--accent); color: white; }
.btn-copy, .btn-details { background: var(--bg-primary); border: 1px solid var(--border); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.playlist-list { display: flex; flex-direction: column; gap: 8px; }
.playlist-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.playlist-info { flex: 1; }
.playlist-name { font-weight: bold; }
.playlist-actions { display: flex; gap: 6px; }
.source-section { margin-bottom: 20px; }
.source-section-title {
  font-weight: bold;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
  margin-bottom: 8px;
}
.source-item {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--bg-card);
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
}
.source-item-name { font-weight: bold; width: 150px; }
.source-item-url { flex: 1; color: var(--text-muted); font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; }
.modal-overlay {
  display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.8); z-index: 1000; align-items: center; justify-content: center;
}
.modal-overlay.visible { display: flex; }
.modal {
  background: var(--bg-modal); border: 1px solid var(--border);
  border-radius: var(--radius); width: 100%; max-width: 500px;
}
.modal-header { display: flex; justify-content: space-between; padding: 16px; border-bottom: 1px solid var(--border); }
.modal-body { padding: 16px; max-height: 70vh; overflow-y: auto; }
.detail-thumb { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 6px; margin-bottom: 12px; }
.detail-row { display: flex; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
.detail-label { width: 120px; color: var(--text-muted); font-weight: bold; }
.modal-actions { display: flex; gap: 8px; padding: 16px; }
.modal-btn { flex: 1; padding: 10px; border-radius: 6px; font-weight: bold; text-align: center; }
.modal-btn.primary { background: var(--accent); color: white; }
.player-list { display: flex; flex-direction: column; gap: 6px; }
.player-option {
  display: flex; align-items: center; gap: 10px; width: 100%;
  background: var(--bg-primary); padding: 12px; border-radius: var(--radius-sm);
}
.toast {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%) translateY(100px);
  background: var(--bg-secondary); border: 1px solid var(--border);
  padding: 12px 24px; border-radius: 6px; z-index: 2000; transition: transform 0.3s ease;
}
.toast.visible { transform: translateX(-50%) translateY(0); }
.download-bar {
  position: fixed; bottom: 0; left: 0; right: 0; background: var(--bg-secondary);
  border-top: 1px solid var(--accent); padding: 12px; display: none; justify-content: space-between;
}
.download-bar.visible { display: flex; }
@media(max-width: 640px) {
  .toolbar { flex-direction: column; align-items: stretch; }
  .channel-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<svg style="display:none">
  <symbol id="i-tv" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="15" rx="2"/><polyline points="17 2 12 7 7 2"/></symbol>
  <symbol id="i-film" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></symbol>
  <symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
  <symbol id="i-list" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></symbol>
  <symbol id="i-database" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></symbol>
</svg>

<header class="header">
  <div class="header-inner">
    <div class="header-title">
      <span class="icon"><svg><use href="#i-tv"/></svg></span> Playlist Dashboard
    </div>
    <div class="header-stats">
      <span class="stat-badge"><span id="totalChannels">__TOTAL_CHANNELS__</span> Channels</span>
      <span class="stat-badge"><span id="totalGroups">__TOTAL_GROUPS__</span> Groups</span>
      <span class="stat-badge"><span id="totalPlaylists">__TOTAL_PLAYLISTS__</span> Playlists</span>
    </div>
    <div class="update-time">
      Last updated: __UPDATE_TIME__
    </div>
  </div>
</header>

<div class="container">
  <nav class="tabs">
    <button class="tab-btn active" data-tab="channels">Channels</button>
    <button class="tab-btn" data-tab="playlists">Playlists</button>
    <button class="tab-btn" data-tab="sources">Sources</button>
  </nav>

  <div class="tab-panel active" id="panel-channels">
    <div class="toolbar">
      <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search channels...">
      </div>
      <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
      <button class="filter-btn" data-filter="Folder" onclick="setFilter('Folder')">Folders</button>
      <button class="filter-btn" data-filter="Single" onclick="setFilter('Single')">Singles</button>
      <button class="filter-btn" data-filter="M3U" onclick="setFilter('M3U')">M3U</button>
      <select id="groupFilter" class="filter-btn"><option value="all">All Groups</option></select>
      <select id="resFilter" class="filter-btn">
        <option value="all">All Res</option>
        <option value="2160p">2160p</option>
        <option value="1080p">1080p</option>
        <option value="720p">720p</option>
      </select>
    </div>
    <div class="select-all-row">
      <label><input type="checkbox" id="selectAllCheckbox"> Select all</label>
      <span id="selectedCount"></span>
    </div>
    <div class="channel-grid" id="channelGrid"></div>
  </div>

  <div class="tab-panel" id="panel-playlists">
    <div class="playlist-list" id="playlistList"></div>
  </div>

  <div class="tab-panel" id="panel-sources">
    <div id="sourcesList"></div>
  </div>
</div>

<div class="download-bar" id="downloadBar">
  <div><span id="downloadCount">0</span> selected</div>
  <div>
    <button onclick="clearSelection()">Clear</button>
    <button onclick="downloadSelected()">Download M3U</button>
  </div>
</div>

<div class="modal-overlay" id="detailModal">
  <div class="modal">
    <div class="modal-header"><span>Details</span><button onclick="closeModal('detailModal')">X</button></div>
    <div class="modal-body" id="detailBody"></div>
    <div class="modal-actions" id="detailActions"></div>
  </div>
</div>

<div class="modal-overlay" id="playerModal">
  <div class="modal">
    <div class="modal-header"><span>Players</span><button onclick="closeModal('playerModal')">X</button></div>
    <div class="modal-body"><div class="player-list" id="playerList"></div></div>
  </div>
</div>

<div class="toast" id="toast"><span id="toastMsg"></span></div>

<script>
const CHANNELS = __CHANNELS_DATA__;
const PLAYLISTS = __PLAYLISTS_DATA__;
const SOURCES = __SOURCES_DATA__;

let currentFilter = 'all';
let currentGroup = 'all';
let currentRes = 'all';
let searchQuery = '';
let selectedChannels = new Set();

const PLAYERS = [
  { name: "Leone Play", package: "com.genuine.leone", scheme: "intent", desc: "Intent Playstore Player" },
  { name: "VLC", package: "org.videolan.vlc", scheme: "vlc", desc: "Standard Open Media Player" },
  { name: "MX Player", package: "com.mxtech.videoplayer.ad", scheme: "intent", desc: "Android Media Player" },
  { name: "Browser Stream", package: null, scheme: "browser", desc: "Open link directly" }
];

document.addEventListener('DOMContentLoaded', () => {
  populateGroupFilter();
  renderChannels();
  renderPlaylists();
  renderSources();
  setupEvents();
});

function setupEvents() {
  document.getElementById('searchInput').addEventListener('input', e => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderChannels();
  });
  document.getElementById('groupFilter').addEventListener('change', e => {
    currentGroup = e.target.value;
    renderChannels();
  });
  document.getElementById('resFilter').addEventListener('change', e => {
    currentRes = e.target.value;
    renderChannels();
  });
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    });
  });
  document.getElementById('selectAllCheckbox').addEventListener('change', e => {
    const visible = getFiltered();
    if(e.target.checked) visible.forEach(ch => selectedChannels.add(ch.stream_url));
    else visible.forEach(ch => selectedChannels.delete(ch.stream_url));
    renderChannels();
  });
}

function populateGroupFilter() {
  const groups = [...new Set(CHANNELS.map(c => c.group))].sort();
  const sel = document.getElementById('groupFilter');
  groups.forEach(g => {
    const opt = document.createElement('option');
    opt.value = g; opt.textContent = g; sel.appendChild(opt);
  });
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderChannels();
}

function getFiltered() {
  return CHANNELS.filter(ch => {
    if(currentFilter !== 'all' && ch.source_type !== currentFilter) return false;
    if(currentGroup !== 'all' && ch.group !== currentGroup) return false;
    if(currentRes !== 'all' && ch.resolution !== currentRes) return false;
    if(searchQuery && !ch.title.toLowerCase().includes(searchQuery)) return false;
    return true;
  });
}

function renderChannels() {
  const grid = document.getElementById('channelGrid');
  const visible = getFiltered();
  if(!visible.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;">No channels found</div>';
    return;
  }
  grid.innerHTML = visible.map((ch, i) => {
    const isSel = selectedChannels.has(ch.stream_url);
    const thumb = ch.thumbnail_url ? `<img src="${ch.thumbnail_url}" loading="lazy">` : '';
    return `
      <div class="channel-card ${isSel ? 'selected' : ''}">
        <input type="checkbox" class="card-select" ${isSel ? 'checked' : ''} onchange="toggleSelect('${ch.stream_url}')">
        <div class="card-thumb">${thumb}</div>
        <div class="card-body">
          <div class="card-title">${ch.title}</div>
          <div class="card-meta">
            <span class="card-tag tag-group">${ch.group}</span>
            <span class="card-tag tag-source">${ch.source_type}</span>
          </div>
          <div class="card-actions">
            <button class="card-btn btn-play" onclick="playChannel(${CHANNELS.indexOf(ch)})">Play</button>
            <button class="card-btn btn-copy" onclick="copyText('${ch.stream_url}')">Copy</button>
            <button class="card-btn btn-details" onclick="showDetails(${CHANNELS.indexOf(ch)})">Details</button>
          </div>
        </div>
      </div>`;
  }).join('');
  updateBar();
}

function toggleSelect(url) {
  if(selectedChannels.has(url)) selectedChannels.delete(url);
  else selectedChannels.add(url);
  renderChannels();
}

function clearSelection() {
  selectedChannels.clear();
  document.getElementById('selectAllCheckbox').checked = false;
  renderChannels();
}

function updateBar() {
  const bar = document.getElementById('downloadBar');
  document.getElementById('downloadCount').textContent = selectedChannels.size;
  bar.classList.toggle('visible', selectedChannels.size > 0);
}

function playChannel(idx) {
  const ch = CHANNELS[idx];
  const list = document.getElementById('playerList');
  list.innerHTML = PLAYERS.map((p, pi) => `
    <button class="player-option" onclick="launch(${idx}, ${pi})">
      <div><strong>${p.name}</strong><br><small>${p.desc}</small></div>
    </button>`).join('');
  openModal('playerModal');
}

function launch(chIdx, plIdx) {
  const ch = CHANNELS[chIdx];
  const player = PLAYERS[plIdx];
  const url = ch.stream_url;
  closeModal('playerModal');

  if(player.scheme === 'browser') {
    window.open(url, '_blank');
    return;
  }
  if(/android/i.test(navigator.userAgent) && player.package) {
    const intent = `intent:${url}#Intent;package=${player.package};type=video/*;S.title=${encodeURIComponent(ch.title)};end`;
    window.location.href = intent;
  } else {
    window.open(url, '_blank');
  }
}

function showDetails(idx) {
  const ch = CHANNELS[idx];
  const body = document.getElementById('detailBody');
  const act = document.getElementById('detailActions');
  body.innerHTML = `
    ${ch.thumbnail_url ? `<img class="detail-thumb" src="${ch.thumbnail_url}">` : ''}
    <div class="detail-row"><span class="detail-label">Title</span><span>${ch.title}</span></div>
    <div class="detail-row"><span class="detail-label">Group</span><span>${ch.group}</span></div>
    <div class="detail-row"><span class="detail-label">Source</span><span>${ch.source_type}</span></div>
    <div class="detail-row"><span class="detail-label">Url</span><span style="word-break:break-all;">${ch.stream_url}</span></div>
    ${ch.file_size_formatted ? `<div class="detail-row"><span class="detail-label">Size</span><span>${ch.file_size_formatted}</span></div>` : ''}
  `;
  act.innerHTML = `<button class="modal-btn primary" onclick="copyText('${ch.stream_url}')">Copy Link</button>`;
  openModal('detailModal');
}

function renderPlaylists() {
  const list = document.getElementById('playlistList');
  const items = [{name: 'All', filename: 'All.m3u', count: CHANNELS.length}, ...PLAYLISTS];
  list.innerHTML = items.map(p => `
    <div class="playlist-item">
      <div class="playlist-info"><strong>${p.name}</strong><br><small>${p.count} Channels</small></div>
      <div class="playlist-actions">
        <button onclick="downloadPlaylistFile('${p.filename}')">Download</button>
      </div>
    </div>`).join('');
}

function downloadPlaylistFile(filename) {
  let list = CHANNELS;
  if(filename !== 'All.m3u') {
    const name = filename.replace('.m3u', '');
    list = CHANNELS.filter(c => c.group === name);
  }
  let content = '#EXTM3U\\n';
  list.forEach(ch => {
    const logo = ch.thumbnail_url ? ` tvg-logo="${ch.thumbnail_url}"` : '';
    content += `#EXTINF:-1 tvg-name="${ch.title}"${logo} group-title="${ch.group_title_combined}",${ch.title}\\n${ch.stream_url}\\n`;
  });
  const blob = new Blob([content], {type: 'audio/x-mpegurl'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}

function downloadSelected() {
  const list = CHANNELS.filter(ch => selectedChannels.has(ch.stream_url));
  let content = '#EXTM3U\\n';
  list.forEach(ch => {
    content += `#EXTINF:-1 tvg-name="${ch.title}" group-title="${ch.group_title_combined}",${ch.title}\\n${ch.stream_url}\\n`;
  });
  const blob = new Blob([content], {type: 'audio/x-mpegurl'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'selected_channels.m3u';
  a.click();
}

function renderSources() {
  const list = document.getElementById('sourcesList');
  let html = '';
  ['folders', 'singles', 'playlists'].forEach(k => {
    if(!SOURCES[k].length) return;
    html += `<div class="source-section"><div class="source-section-title">${k.toUpperCase()}</div>`;
    SOURCES[k].forEach(([g, u]) => {
      html += `<div class="source-item"><span class="source-item-name">${g}</span><span class="source-item-url">${u}</span></div>`;
    });
    html += '</div>';
  });
  list.innerHTML = html || 'No sources';
}

function copyText(t) {
  navigator.clipboard.writeText(t).then(() => {
    const toast = document.getElementById('toast');
    document.getElementById('toastMsg').textContent = 'Copied!';
    toast.classList.add('visible');
    setTimeout(() => toast.classList.remove('visible'), 2000);
  });
}

function openModal(id) { document.getElementById(id).classList.add('visible'); }
function closeModal(id) { document.getElementById(id).classList.remove('visible'); }
</script>
</body>
</html>
"""

    # Bulletproof String Replacements
    template = template.replace("__CHANNELS_DATA__", json.dumps(channels_json, ensure_ascii=False))
    template = template.replace("__PLAYLISTS_DATA__", json.dumps(playlists_json, ensure_ascii=False))
    template = template.replace("__SOURCES_DATA__", json.dumps(sources_json, ensure_ascii=False))
    template = template.replace("__UPDATE_TIME__", update_time)
    template = template.replace("__TOTAL_CHANNELS__", str(total_channels))
    template = template.replace("__TOTAL_GROUPS__", str(total_groups))
    template = template.replace("__TOTAL_PLAYLISTS__", str(len(playlist_files)))

    return template


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("=" * 60)
    log.info("M3U Playlist Generator + Website — starting")
    log.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_entries: list[dict] = []
    group_entries: dict[str, list[dict]] = {}

    source_configs: dict[str, list[tuple[str, str]]] = {
        "folders": [],
        "singles": [],
        "playlists": [],
    }

    # ---- 1. Pixeldrain folders ----
    log.info("")
    log.info("[1/3] Processing Pixeldrain FOLDERS...")
    folder_sources = parse_input_file(FOLDER_TXT)
    source_configs["folders"] = folder_sources
    for group, url in folder_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_pixeldrain_folder(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error processing folder '%s': %s", group, e)

    # ---- 2. Pixeldrain singles ----
    log.info("")
    log.info("[2/3] Processing Pixeldrain SINGLE FILES...")
    single_sources = parse_input_file(FOLDER_S_TXT)
    source_configs["singles"] = single_sources
    for group, url in single_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_pixeldrain_single(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error processing single '%s': %s", group, e)

    # ---- 3. External M3U ----
    log.info("")
    log.info("[3/3] Processing EXTERNAL M3U playlists...")
    playlist_sources = parse_input_file(PLAYLIST_TXT)
    source_configs["playlists"] = playlist_sources
    for group, url in playlist_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_external_m3u(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error processing playlist '%s': %s", group, e)

    # ---- Cleanup ----
    log.info("")
    log.info("Cleaning old output files...")
    for old in OUTPUT_DIR.glob("*.m3u"):
        old.unlink()

    # ---- Write per-group M3Us ----
    log.info("")
    log.info("Writing per-group playlists...")
    playlist_files: dict[str, str] = {}
    for group_name, entries in group_entries.items():
        if not entries:
            continue
        safe_name = sanitize_filename(group_name)
        filename = f"{safe_name}.m3u"
        filepath = OUTPUT_DIR / filename
        write_m3u(filepath, entries, combined=False)
        validate_m3u(filepath)
        playlist_files[group_name] = filename

    # ---- Combined All.m3u ----
    log.info("")
    log.info("Writing combined All.m3u...")
    if all_entries:
        combined_path = OUTPUT_DIR / "All.m3u"
        write_m3u(combined_path, all_entries, combined=True)
        validate_m3u(combined_path)

    # ---- Generate website DIRECTLY TO ROOT ----
    log.info("")
    log.info("Generating website (index.html) directly to repository root...")
    html_content = generate_website(
        all_entries=all_entries,
        group_entries=group_entries,
        playlist_files=playlist_files,
        source_configs=source_configs,
        repo_base_url="",
    )
    html_path = BASE_DIR / "index.html"
    html_path.write_text(html_content, encoding="utf-8")
    log.info("  Website successfully written to %s", html_path)

    # ---- Summary ----
    log.info("")
    log.info("=" * 60)
    log.info("DONE — %d group(s), %d total entries", len(group_entries), len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        sys.exit(130)
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)

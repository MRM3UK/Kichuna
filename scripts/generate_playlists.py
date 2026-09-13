#!/usr/bin/env python3
"""
M3U Playlist Generator + Website Builder
Fetches videos from Pixeldrain and external M3U playlists,
generates M3U files and a single-page website dashboard.
"""

from __future__ import annotations

import hashlib
import html as html_module
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
    """Parse both double-quoted and single-quoted M3U attributes."""
    if not attr_str:
        return {}
    attrs = dict(_M3U_ATTR_RE.findall(attr_str))
    if not attrs:
        attrs = dict(_M3U_ATTR_SQ_RE.findall(attr_str))
    return attrs


def extract_thumbnail_from_extinf(attr_str: str, attrs: dict[str, str]) -> str:
    """
    Extract thumbnail/poster/logo from M3U EXTINF attributes.
    Checks multiple common attribute names used by various M3U generators.
    """
    # Priority order of attribute names for thumbnails
    logo_keys = [
        "tvg-logo",
        "tvg-logo-small",
        "logo",
        "poster",
        "thumbnail",
        "icon",
        "tvg-icon",
        "channel-logo",
        "cover",
        "art",
        "artwork",
    ]
    for key in logo_keys:
        val = attrs.get(key, "").strip()
        if val and val.startswith(("http://", "https://")):
            return val

    # Also try case-insensitive search through the raw string
    for key in logo_keys:
        pattern = re.compile(
            rf'{re.escape(key)}\s*=\s*"([^"]+)"', re.IGNORECASE
        )
        m = pattern.search(attr_str)
        if m:
            val = m.group(1).strip()
            if val.startswith(("http://", "https://")):
                return val
        pattern_sq = re.compile(
            rf"{re.escape(key)}\s*=\s*'([^']+)'", re.IGNORECASE
        )
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
    """Fetch and parse an external M3U with full poster/thumbnail support."""
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
        original_group = ""

        if pending_extinf:
            m = _EXTINF_FULL_RE.match(pending_extinf)
            if m:
                attr_str = m.group(2) or ""
                title = (m.group(3) or "").strip()
                attrs = parse_m3u_attributes(attr_str)

                # --- FIXED: robust thumbnail extraction ---
                thumbnail = extract_thumbnail_from_extinf(attr_str, attrs)

                if not title:
                    title = attrs.get("tvg-name", "")
                original_group = attrs.get("group-title", "")
            pending_extinf = None

        if not title:
            path = unquote(urlparse(stream_url).path)
            title = strip_extension(Path(path).name) or "Untitled"

        title = safe_title(title)
        resolution = detect_resolution(title) or detect_resolution(stream_url)

        # Check if the stream URL is from pixeldrain
        pd_id = extract_pd_file_id(stream_url)
        pd_thumb = ""
        file_size = 0
        mime_type = ""
        if pd_id:
            pd_thumb = pd_thumbnail_url(pd_id)
            if not thumbnail:
                thumbnail = pd_thumb
            # Optionally fetch metadata for pixeldrain URLs in M3U
            info = pd_api_get(f"/file/{pd_id}/info")
            if info:
                file_size = info.get("size", 0) or 0
                mime_type = info.get("mime_type", "") or ""
                fname = info.get("name", "")
                if fname and not resolution:
                    resolution = detect_resolution(fname)
                if not thumbnail:
                    thumbnail = pd_thumbnail_url(pd_id)

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
# HTML WEBSITE GENERATOR
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
    """Generate a complete single-page HTML website."""

    update_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_channels = len(all_entries)
    total_groups = len(group_entries)

    # Build JSON data for JavaScript
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

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<meta name="theme-color" content="#0f172a">
<meta name="description" content="M3U Playlist Dashboard - Browse, stream and download video playlists">
<title>Playlist Dashboard</title>
<style>
/* ===== CSS Reset & Variables ===== */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
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
}}

html {{
  font-size: 16px;
  scroll-behavior: smooth;
  -webkit-tap-highlight-color: transparent;
}}

body {{
  font-family: var(--font);
  background: var(--bg-primary);
  color: var(--text-primary);
  min-height: 100vh;
  line-height: 1.5;
  overflow-x: hidden;
}}

a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ color: var(--accent-hover); }}

button {{
  cursor: pointer;
  border: none;
  background: none;
  font-family: var(--font);
  font-size: inherit;
  color: inherit;
}}

/* ===== Icons (Pure CSS) ===== */
.icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}}

.icon svg {{
  width: 100%;
  height: 100%;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}}

/* ===== Layout ===== */
.container {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}}

/* ===== Header ===== */
.header {{
  background: linear-gradient(135deg, var(--bg-secondary) 0%, #1a2744 100%);
  border-bottom: 1px solid var(--border);
  padding: 20px 0;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(8px);
}}

.header-inner {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}}

.header-title {{
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--text-primary);
}}

.header-title .icon {{
  width: 28px;
  height: 28px;
  color: var(--accent);
}}

.header-stats {{
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}}

.stat-badge {{
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
}}

.stat-badge .icon {{ width: 14px; height: 14px; }}

.update-time {{
  font-size: 0.75rem;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  padding-top: 4px;
}}

.update-time .icon {{ width: 12px; height: 12px; }}

/* ===== Tab Navigation ===== */
.tabs {{
  display: flex;
  gap: 4px;
  padding: 12px 0;
  overflow-x: auto;
  scrollbar-width: none;
  -ms-overflow-style: none;
}}
.tabs::-webkit-scrollbar {{ display: none; }}

.tab-btn {{
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  border-radius: var(--radius-sm);
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  transition: all var(--transition);
  white-space: nowrap;
}}

.tab-btn:hover {{
  color: var(--text-primary);
  background: var(--bg-secondary);
}}

.tab-btn.active {{
  color: var(--accent);
  background: var(--accent-light);
  border-color: var(--accent);
}}

.tab-btn .icon {{ width: 16px; height: 16px; }}
.tab-count {{
  background: var(--bg-primary);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 0.7rem;
  color: var(--text-muted);
}}
.tab-btn.active .tab-count {{
  background: rgba(59,130,246,0.3);
  color: var(--accent);
}}

/* ===== Search & Filters ===== */
.toolbar {{
  display: flex;
  gap: 8px;
  padding: 8px 0;
  flex-wrap: wrap;
  align-items: center;
}}

.search-box {{
  flex: 1;
  min-width: 200px;
  position: relative;
}}

.search-box .icon {{
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  width: 16px;
  height: 16px;
  color: var(--text-muted);
  pointer-events: none;
}}

.search-box input {{
  width: 100%;
  padding: 10px 12px 10px 38px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 0.9rem;
  transition: border-color var(--transition);
  outline: none;
}}

.search-box input:focus {{
  border-color: var(--accent);
}}

.search-box input::placeholder {{
  color: var(--text-muted);
}}

.filter-btn, .action-btn {{
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 0.85rem;
  font-weight: 500;
  transition: all var(--transition);
  white-space: nowrap;
}}

.filter-btn:hover, .action-btn:hover {{
  color: var(--text-primary);
  border-color: var(--accent);
  background: var(--bg-card-hover);
}}

.filter-btn.active {{
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-light);
}}

.action-btn.primary {{
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}}
.action-btn.primary:hover {{
  background: var(--accent-hover);
}}

.filter-btn .icon, .action-btn .icon {{ width: 14px; height: 14px; }}

/* ===== Select All Checkbox ===== */
.select-all-row {{
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  color: var(--text-secondary);
  font-size: 0.85rem;
}}

.select-all-row label {{
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}}

/* ===== Channel Grid ===== */
.channel-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  padding: 8px 0 20px;
}}

/* ===== Channel Card ===== */
.channel-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: all var(--transition);
  position: relative;
}}

.channel-card:hover {{
  border-color: var(--accent);
  background: var(--bg-card-hover);
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}}

.channel-card.selected {{
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-light);
}}

.card-select {{
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 5;
  width: 20px;
  height: 20px;
  accent-color: var(--accent);
  cursor: pointer;
}}

.card-thumb {{
  position: relative;
  width: 100%;
  padding-top: 56.25%;
  background: #0c1525;
  overflow: hidden;
}}

.card-thumb img {{
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 0.3s ease;
}}

.channel-card:hover .card-thumb img {{
  transform: scale(1.05);
}}

.card-thumb-placeholder {{
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
}}

.card-thumb-placeholder .icon {{
  width: 48px;
  height: 48px;
  opacity: 0.4;
}}

.res-badge {{
  position: absolute;
  top: 8px;
  right: 8px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}

.res-badge.res-2160p {{ background: #dc2626; color: white; }}
.res-badge.res-1440p {{ background: #ea580c; color: white; }}
.res-badge.res-1080p {{ background: #16a34a; color: white; }}
.res-badge.res-720p {{ background: #2563eb; color: white; }}
.res-badge.res-480p {{ background: #7c3aed; color: white; }}
.res-badge.res-other {{ background: var(--text-muted); color: white; }}

.card-body {{
  padding: 12px;
}}

.card-title {{
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.card-meta {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}}

.card-tag {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 600;
}}

.tag-group {{
  background: rgba(59,130,246,0.15);
  color: var(--accent);
}}

.tag-source {{
  background: rgba(34,197,94,0.15);
  color: var(--success);
}}

.tag-size {{
  background: rgba(148,163,184,0.15);
  color: var(--text-secondary);
}}

.card-actions {{
  display: flex;
  gap: 6px;
}}

.card-btn {{
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px;
  border-radius: var(--radius-xs);
  font-size: 0.75rem;
  font-weight: 600;
  transition: all var(--transition);
}}

.card-btn .icon {{ width: 14px; height: 14px; }}

.btn-play {{
  background: var(--accent);
  color: white;
}}
.btn-play:hover {{ background: var(--accent-hover); }}

.btn-copy {{
  background: var(--bg-primary);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}}
.btn-copy:hover {{
  color: var(--text-primary);
  border-color: var(--accent);
}}

.btn-details {{
  background: var(--bg-primary);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}}
.btn-details:hover {{
  color: var(--text-primary);
  border-color: var(--accent);
}}

/* ===== Tab Panels ===== */
.tab-panel {{
  display: none;
}}
.tab-panel.active {{
  display: block;
}}

/* ===== Playlists Panel ===== */
.playlist-list {{
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 0;
}}

.playlist-item {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  transition: all var(--transition);
}}

.playlist-item:hover {{
  border-color: var(--accent);
  background: var(--bg-card-hover);
}}

.playlist-icon {{
  width: 40px;
  height: 40px;
  background: var(--accent-light);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  flex-shrink: 0;
}}

.playlist-info {{
  flex: 1;
  min-width: 0;
}}

.playlist-name {{
  font-weight: 600;
  font-size: 0.95rem;
  margin-bottom: 2px;
}}

.playlist-count {{
  font-size: 0.8rem;
  color: var(--text-muted);
}}

.playlist-actions {{
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}}

/* ===== Sources Panel ===== */
.source-section {{
  margin-bottom: 20px;
}}

.source-section-title {{
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 1rem;
  font-weight: 700;
  color: var(--text-primary);
  padding: 12px 0 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}}

.source-section-title .icon {{ color: var(--accent); }}

.source-item {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  margin-bottom: 6px;
}}

.source-item-name {{
  font-weight: 600;
  font-size: 0.9rem;
  min-width: 120px;
}}

.source-item-url {{
  flex: 1;
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

/* ===== Modal ===== */
.modal-overlay {{
  display: none;
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0,0,0,0.7);
  z-index: 1000;
  align-items: center;
  justify-content: center;
  padding: 16px;
  backdrop-filter: blur(4px);
}}

.modal-overlay.visible {{
  display: flex;
}}

.modal {{
  background: var(--bg-modal);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  width: 100%;
  max-width: 550px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
}}

.modal-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}}

.modal-title {{
  font-size: 1.1rem;
  font-weight: 700;
}}

.modal-close {{
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-xs);
  color: var(--text-muted);
  transition: all var(--transition);
}}

.modal-close:hover {{
  background: var(--bg-primary);
  color: var(--text-primary);
}}

.modal-body {{
  padding: 20px;
}}

.detail-thumb {{
  width: 100%;
  border-radius: var(--radius-sm);
  margin-bottom: 16px;
  aspect-ratio: 16/9;
  object-fit: cover;
  background: #0c1525;
}}

.detail-row {{
  display: flex;
  padding: 10px 0;
  border-bottom: 1px solid rgba(51,65,85,0.5);
  gap: 12px;
  align-items: flex-start;
}}

.detail-label {{
  font-size: 0.8rem;
  color: var(--text-muted);
  font-weight: 600;
  min-width: 90px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  flex-shrink: 0;
}}

.detail-value {{
  font-size: 0.85rem;
  color: var(--text-primary);
  word-break: break-all;
  flex: 1;
}}

.detail-value.mono {{
  font-family: var(--font-mono);
  font-size: 0.78rem;
  color: var(--text-secondary);
}}

.modal-actions {{
  display: flex;
  gap: 8px;
  padding: 16px 20px;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}}

.modal-btn {{
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 16px;
  border-radius: var(--radius-sm);
  font-size: 0.85rem;
  font-weight: 600;
  transition: all var(--transition);
  min-width: 100px;
}}

.modal-btn .icon {{ width: 16px; height: 16px; }}

.modal-btn.primary {{
  background: var(--accent);
  color: white;
}}
.modal-btn.primary:hover {{ background: var(--accent-hover); }}

.modal-btn.secondary {{
  background: var(--bg-primary);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}}
.modal-btn.secondary:hover {{
  color: var(--text-primary);
  border-color: var(--accent);
}}

/* ===== Player Chooser ===== */
.player-list {{
  display: flex;
  flex-direction: column;
  gap: 6px;
}}

.player-option {{
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  transition: all var(--transition);
  text-align: left;
  width: 100%;
}}

.player-option:hover {{
  border-color: var(--accent);
  background: var(--bg-card-hover);
}}

.player-option-icon {{
  width: 36px;
  height: 36px;
  background: var(--accent-light);
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  flex-shrink: 0;
}}

.player-option-info {{
  flex: 1;
}}

.player-option-name {{
  font-weight: 600;
  font-size: 0.9rem;
}}

.player-option-desc {{
  font-size: 0.75rem;
  color: var(--text-muted);
}}

/* ===== Toast ===== */
.toast {{
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%) translateY(100px);
  background: var(--bg-secondary);
  color: var(--text-primary);
  padding: 12px 24px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  font-size: 0.85rem;
  font-weight: 500;
  z-index: 2000;
  transition: transform 0.3s ease;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 90vw;
}}

.toast.visible {{
  transform: translateX(-50%) translateY(0);
}}

.toast .icon {{ width: 16px; height: 16px; color: var(--success); flex-shrink: 0; }}

/* ===== Download Bar ===== */
.download-bar {{
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--bg-secondary);
  border-top: 1px solid var(--accent);
  padding: 12px 16px;
  z-index: 200;
  display: none;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  box-shadow: 0 -4px 12px rgba(0,0,0,0.4);
}}

.download-bar.visible {{
  display: flex;
}}

.download-bar-info {{
  font-size: 0.85rem;
  color: var(--text-secondary);
}}

.download-bar-info strong {{
  color: var(--accent);
}}

.download-bar-actions {{
  display: flex;
  gap: 8px;
}}

/* ===== Empty State ===== */
.empty-state {{
  text-align: center;
  padding: 60px 20px;
  color: var(--text-muted);
}}

.empty-state .icon {{
  width: 64px;
  height: 64px;
  margin-bottom: 16px;
  opacity: 0.4;
}}

.empty-state p {{
  font-size: 1rem;
  margin-bottom: 4px;
}}

.empty-state small {{
  font-size: 0.8rem;
  color: var(--text-muted);
}}

/* ===== Loading ===== */
.loading {{
  text-align: center;
  padding: 40px;
  color: var(--text-muted);
}}

.spinner {{
  width: 32px;
  height: 32px;
  border: 3px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 12px;
}}

@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

/* ===== Responsive ===== */
@media (max-width: 640px) {{
  .header-inner {{
    flex-direction: column;
    align-items: flex-start;
  }}
  .channel-grid {{
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }}
  .toolbar {{
    flex-direction: column;
  }}
  .search-box {{
    width: 100%;
  }}
  .modal {{
    max-width: 100%;
    margin: 0;
    border-radius: var(--radius) var(--radius) 0 0;
    max-height: 95vh;
  }}
  .modal-overlay {{
    align-items: flex-end;
  }}
  .download-bar {{
    flex-direction: column;
    text-align: center;
  }}
  .playlist-item {{
    flex-wrap: wrap;
  }}
  .playlist-actions {{
    width: 100%;
  }}
  .playlist-actions .action-btn {{
    flex: 1;
  }}
}}

@media (max-width: 400px) {{
  .channel-grid {{
    grid-template-columns: 1fr;
  }}
  .card-actions {{
    flex-direction: column;
  }}
}}
</style>
</head>
<body>

<!-- ===== SVG Icon Definitions ===== -->
<svg style="display:none" xmlns="http://www.w3.org/2000/svg">
  <symbol id="i-tv" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="15" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/></symbol>
  <symbol id="i-film" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/><line x1="17" y1="17" x2="22" y2="17"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></symbol>
  <symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
  <symbol id="i-list" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></symbol>
  <symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></symbol>
  <symbol id="i-external" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></symbol>
  <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
  <symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></symbol>
  <symbol id="i-filter" viewBox="0 0 24 24"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></symbol>
  <symbol id="i-hd" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><text x="12" y="14" text-anchor="middle" font-size="7" font-weight="bold" fill="currentColor" stroke="none">HD</text></symbol>
  <symbol id="i-video" viewBox="0 0 24 24"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></symbol>
  <symbol id="i-database" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></symbol>
</svg>

<!-- ===== Header ===== -->
<header class="header">
  <div class="header-inner">
    <div class="header-title">
      <span class="icon"><svg><use href="#i-tv"/></svg></span>
      Playlist Dashboard
    </div>
    <div class="header-stats">
      <span class="stat-badge">
        <span class="icon"><svg><use href="#i-video"/></svg></span>
        <span id="totalChannels">{total_channels}</span> Channels
      </span>
      <span class="stat-badge">
        <span class="icon"><svg><use href="#i-folder"/></svg></span>
        <span id="totalGroups">{total_groups}</span> Groups
      </span>
      <span class="stat-badge">
        <span class="icon"><svg><use href="#i-list"/></svg></span>
        <span id="totalPlaylists">{len(playlist_files)}</span> Playlists
      </span>
    </div>
    <div class="update-time">
      <span class="icon"><svg><use href="#i-clock"/></svg></span>
      Last updated: {update_time}
    </div>
  </div>
</header>

<!-- ===== Main Content ===== -->
<div class="container">

  <!-- Tabs -->
  <nav class="tabs" id="tabNav">
    <button class="tab-btn active" data-tab="channels">
      <span class="icon"><svg><use href="#i-grid"/></svg></span>
      All Channels
      <span class="tab-count" id="channelTabCount">{total_channels}</span>
    </button>
    <button class="tab-btn" data-tab="playlists">
      <span class="icon"><svg><use href="#i-list"/></svg></span>
      Playlists
      <span class="tab-count">{len(playlist_files)}</span>
    </button>
    <button class="tab-btn" data-tab="sources">
      <span class="icon"><svg><use href="#i-database"/></svg></span>
      Sources
    </button>
  </nav>

  <!-- ===== Channels Panel ===== -->
  <div class="tab-panel active" id="panel-channels">
    <div class="toolbar">
      <div class="search-box">
        <span class="icon"><svg><use href="#i-search"/></svg></span>
        <input type="text" id="searchInput" placeholder="Search channels...">
      </div>
      <button class="filter-btn" id="filterAll" data-filter="all" onclick="setFilter('all')">
        <span class="icon"><svg><use href="#i-grid"/></svg></span> All
      </button>
      <button class="filter-btn" id="filterFolder" data-filter="Folder" onclick="setFilter('Folder')">
        <span class="icon"><svg><use href="#i-folder"/></svg></span> Folders
      </button>
      <button class="filter-btn" id="filterSingle" data-filter="Single" onclick="setFilter('Single')">
        <span class="icon"><svg><use href="#i-file"/></svg></span> Singles
      </button>
      <button class="filter-btn" id="filterM3U" data-filter="M3U" onclick="setFilter('M3U')">
        <span class="icon"><svg><use href="#i-globe"/></svg></span> M3U
      </button>
      <select id="groupFilter" class="filter-btn" style="appearance:auto; padding-right:24px;">
        <option value="all">All Groups</option>
      </select>
      <select id="resFilter" class="filter-btn" style="appearance:auto; padding-right:24px;">
        <option value="all">All Resolutions</option>
        <option value="2160p">2160p / 4K</option>
        <option value="1440p">1440p</option>
        <option value="1080p">1080p</option>
        <option value="720p">720p</option>
        <option value="480p">480p</option>
      </select>
    </div>

    <div class="select-all-row">
      <label>
        <input type="checkbox" id="selectAllCheckbox">
        Select all visible
      </label>
      <span id="selectedCount" style="color:var(--accent);font-weight:600;"></span>
    </div>

    <div class="channel-grid" id="channelGrid"></div>
  </div>

  <!-- ===== Playlists Panel ===== -->
  <div class="tab-panel" id="panel-playlists">
    <div class="playlist-list" id="playlistList"></div>
  </div>

  <!-- ===== Sources Panel ===== -->
  <div class="tab-panel" id="panel-sources">
    <div id="sourcesList"></div>
  </div>

</div>

<!-- ===== Download Bar ===== -->
<div class="download-bar" id="downloadBar">
  <div class="download-bar-info">
    <strong id="downloadCount">0</strong> channel(s) selected
  </div>
  <div class="download-bar-actions">
    <button class="action-btn" onclick="clearSelection()">
      <span class="icon"><svg><use href="#i-x"/></svg></span> Clear
    </button>
    <button class="action-btn primary" onclick="downloadSelected()">
      <span class="icon"><svg><use href="#i-download"/></svg></span> Download M3U
    </button>
  </div>
</div>

<!-- ===== Detail Modal ===== -->
<div class="modal-overlay" id="detailModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">Channel Details</span>
      <button class="modal-close" onclick="closeModal('detailModal')">
        <span class="icon"><svg><use href="#i-x"/></svg></span>
      </button>
    </div>
    <div class="modal-body" id="detailBody"></div>
    <div class="modal-actions" id="detailActions"></div>
  </div>
</div>

<!-- ===== Player Chooser Modal ===== -->
<div class="modal-overlay" id="playerModal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">Open with Player</span>
      <button class="modal-close" onclick="closeModal('playerModal')">
        <span class="icon"><svg><use href="#i-x"/></svg></span>
      </button>
    </div>
    <div class="modal-body">
      <div class="player-list" id="playerList"></div>
    </div>
  </div>
</div>

<!-- ===== Toast ===== -->
<div class="toast" id="toast">
  <span class="icon"><svg><use href="#i-check"/></svg></span>
  <span id="toastMsg"></span>
</div>

<script>
// ===== DATA =====
const CHANNELS = {json.dumps(channels_json, ensure_ascii=False)};
const PLAYLISTS = {json.dumps(playlists_json, ensure_ascii=False)};
const SOURCES = {json.dumps(sources_json, ensure_ascii=False)};
const UPDATE_TIME = "{update_time}";
const REPO_BASE = "{repo_base_url}";

// ===== STATE =====
let currentFilter = 'all';
let currentGroup = 'all';
let currentRes = 'all';
let searchQuery = '';
let selectedChannels = new Set();

// ===== PLAYERS =====
const PLAYERS = [
  {{
    name: "Leone Play",
    desc: "Intent-based video player",
    package: "com.genuine.leone",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=com.genuine.leone"
  }},
  {{
    name: "VLC Media Player",
    desc: "Open-source universal player",
    package: "org.videolan.vlc",
    scheme: "vlc",
    playStore: "https://play.google.com/store/apps/details?id=org.videolan.vlc"
  }},
  {{
    name: "MX Player",
    desc: "Popular Android video player",
    package: "com.mxtech.videoplayer.ad",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad"
  }},
  {{
    name: "MX Player Pro",
    desc: "Ad-free MX Player",
    package: "com.mxtech.videoplayer.pro",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.pro"
  }},
  {{
    name: "Just (Video) Player",
    desc: "Simple, clean video player",
    package: "com.brouken.player",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=com.brouken.player"
  }},
  {{
    name: "IPTV Smarters Pro",
    desc: "IPTV playlist player",
    package: "com.nst.iptvsmarterstvbox",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=com.nst.iptvsmarterstvbox"
  }},
  {{
    name: "OTT Navigator",
    desc: "IPTV/OTT player",
    package: "studio.scillarium.ottnavigator",
    scheme: "intent",
    playStore: "https://play.google.com/store/apps/details?id=studio.scillarium.ottnavigator"
  }},
  {{
    name: "Open in Browser",
    desc: "Direct link in browser",
    package: null,
    scheme: "browser",
    playStore: null
  }}
];

// ===== INIT =====
document.addEventListener('DOMContentLoaded', () => {{
  populateGroupFilter();
  renderChannels();
  renderPlaylists();
  renderSources();
  setupEventListeners();
}});

function setupEventListeners() {{
  document.getElementById('searchInput').addEventListener('input', (e) => {{
    searchQuery = e.target.value.toLowerCase().trim();
    renderChannels();
  }});

  document.getElementById('groupFilter').addEventListener('change', (e) => {{
    currentGroup = e.target.value;
    renderChannels();
  }});

  document.getElementById('resFilter').addEventListener('change', (e) => {{
    currentRes = e.target.value;
    renderChannels();
  }});

  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    }});
  }});

  document.getElementById('selectAllCheckbox').addEventListener('change', (e) => {{
    const visible = getFilteredChannels();
    if (e.target.checked) {{
      visible.forEach((_, i) => selectedChannels.add(getChannelKey(visible[i])));
    }} else {{
      visible.forEach((_, i) => selectedChannels.delete(getChannelKey(visible[i])));
    }}
    renderChannels();
    updateDownloadBar();
  }});

  document.querySelectorAll('.modal-overlay').forEach(overlay => {{
    overlay.addEventListener('click', (e) => {{
      if (e.target === overlay) closeModal(overlay.id);
    }});
  }});
}}

function getChannelKey(ch) {{
  return ch.stream_url;
}}

function populateGroupFilter() {{
  const groups = [...new Set(CHANNELS.map(c => c.group))].sort();
  const sel = document.getElementById('groupFilter');
  groups.forEach(g => {{
    const opt = document.createElement('option');
    opt.value = g;
    opt.textContent = g;
    sel.appendChild(opt);
  }});
}}

function setFilter(f) {{
  currentFilter = f;
  document.querySelectorAll('.filter-btn[data-filter]').forEach(b => {{
    b.classList.toggle('active', b.dataset.filter === f);
  }});
  renderChannels();
}}

function getFilteredChannels() {{
  return CHANNELS.filter(ch => {{
    if (currentFilter !== 'all' && ch.source_type !== currentFilter) return false;
    if (currentGroup !== 'all' && ch.group !== currentGroup) return false;
    if (currentRes !== 'all' && ch.resolution !== currentRes) return false;
    if (searchQuery && !ch.title.toLowerCase().includes(searchQuery)
        && !ch.group.toLowerCase().includes(searchQuery)) return false;
    return true;
  }});
}}

function renderChannels() {{
  const grid = document.getElementById('channelGrid');
  const filtered = getFilteredChannels();
  document.getElementById('channelTabCount').textContent = filtered.length;

  if (filtered.length === 0) {{
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <span class="icon"><svg><use href="#i-search"/></svg></span>
        <p>No channels found</p>
        <small>Try adjusting your search or filters</small>
      </div>`;
    return;
  }}

  grid.innerHTML = filtered.map((ch, i) => {{
    const key = getChannelKey(ch);
    const isSelected = selectedChannels.has(key);
    const resClass = ch.resolution ? 'res-' + ch.resolution : 'res-other';
    const thumbHtml = ch.thumbnail_url
      ? `<img src="${{escHtml(ch.thumbnail_url)}}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'card-thumb-placeholder\\'><span class=\\'icon\\'><svg><use href=\\'#i-film\\'/></svg></span></div>'">`
      : `<div class="card-thumb-placeholder"><span class="icon"><svg><use href="#i-film"/></svg></span></div>`;

    return `
    <div class="channel-card ${{isSelected ? 'selected' : ''}}" id="card-${{i}}">
      <input type="checkbox" class="card-select" ${{isSelected ? 'checked' : ''}}
        onchange="toggleSelect('${{escAttr(key)}}')">
      <div class="card-thumb">
        ${{thumbHtml}}
        ${{ch.resolution ? `<span class="res-badge ${{resClass}}">${{ch.resolution}}</span>` : ''}}
      </div>
      <div class="card-body">
        <div class="card-title" title="${{escAttr(ch.title)}}">${{escHtml(ch.title)}}</div>
        <div class="card-meta">
          <span class="card-tag tag-group">${{escHtml(ch.group)}}</span>
          <span class="card-tag tag-source">${{ch.source_type}}</span>
          ${{ch.file_size_formatted ? `<span class="card-tag tag-size">${{ch.file_size_formatted}}</span>` : ''}}
        </div>
        <div class="card-actions">
          <button class="card-btn btn-play" onclick="openPlayerChooser(${{CHANNELS.indexOf(ch)}})">
            <span class="icon"><svg><use href="#i-play"/></svg></span> Play
          </button>
          <button class="card-btn btn-copy" onclick="copyToClipboard('${{escAttr(ch.stream_url)}}')">
            <span class="icon"><svg><use href="#i-copy"/></svg></span> Copy
          </button>
          <button class="card-btn btn-details" onclick="showDetails(${{CHANNELS.indexOf(ch)}})">
            <span class="icon"><svg><use href="#i-info"/></svg></span>
          </button>
        </div>
      </div>
    </div>`;
  }}).join('');

  updateDownloadBar();
}}

function toggleSelect(key) {{
  if (selectedChannels.has(key)) {{
    selectedChannels.delete(key);
  }} else {{
    selectedChannels.add(key);
  }}
  renderChannels();
  updateDownloadBar();
}}

function clearSelection() {{
  selectedChannels.clear();
  document.getElementById('selectAllCheckbox').checked = false;
  renderChannels();
  updateDownloadBar();
}}

function updateDownloadBar() {{
  const bar = document.getElementById('downloadBar');
  const count = selectedChannels.size;
  document.getElementById('downloadCount').textContent = count;
  document.getElementById('selectedCount').textContent = count > 0 ? count + ' selected' : '';
  bar.classList.toggle('visible', count > 0);
}}

// ===== DETAILS MODAL =====
function showDetails(idx) {{
  const ch = CHANNELS[idx];
  const body = document.getElementById('detailBody');
  const actions = document.getElementById('detailActions');

  let thumbHtml = '';
  if (ch.thumbnail_url) {{
    thumbHtml = `<img class="detail-thumb" src="${{escHtml(ch.thumbnail_url)}}" alt=""
      onerror="this.style.display='none'">`;
  }}

  let rows = `
    <div class="detail-row">
      <span class="detail-label">Title</span>
      <span class="detail-value">${{escHtml(ch.title)}}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Group</span>
      <span class="detail-value">${{escHtml(ch.group)}}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Source</span>
      <span class="detail-value">${{escHtml(ch.source_type)}}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Resolution</span>
      <span class="detail-value">${{ch.resolution || 'Unknown'}}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Stream URL</span>
      <span class="detail-value mono">${{escHtml(ch.stream_url)}}</span>
    </div>`;

  if (ch.thumbnail_url) {{
    rows += `
    <div class="detail-row">
      <span class="detail-label">Thumbnail</span>
      <span class="detail-value mono">${{escHtml(ch.thumbnail_url)}}</span>
    </div>`;
  }}

  // Pixeldrain-specific details
  if (ch.pd_file_id) {{
    rows += `
    <div class="detail-row">
      <span class="detail-label">PD File ID</span>
      <span class="detail-value mono">${{escHtml(ch.pd_file_id)}}</span>
    </div>`;
    if (ch.mime_type) {{
      rows += `
    <div class="detail-row">
      <span class="detail-label">MIME Type</span>
      <span class="detail-value">${{escHtml(ch.mime_type)}}</span>
    </div>`;
    }}
    if (ch.file_size_formatted) {{
      rows += `
    <div class="detail-row">
      <span class="detail-label">File Size</span>
      <span class="detail-value">${{ch.file_size_formatted}}</span>
    </div>`;
    }}
    if (ch.original_filename) {{
      rows += `
    <div class="detail-row">
      <span class="detail-label">Filename</span>
      <span class="detail-value">${{escHtml(ch.original_filename)}}</span>
    </div>`;
    }}
    rows += `
    <div class="detail-row">
      <span class="detail-label">PD Page</span>
      <span class="detail-value"><a href="https://pixeldrain.com/u/${{ch.pd_file_id}}" target="_blank" rel="noopener">Open on Pixeldrain</a></span>
    </div>`;
  }}

  if (ch.group_title_combined) {{
    rows += `
    <div class="detail-row">
      <span class="detail-label">M3U Group</span>
      <span class="detail-value">${{escHtml(ch.group_title_combined)}}</span>
    </div>`;
  }}

  body.innerHTML = thumbHtml + rows;

  actions.innerHTML = `
    <button class="modal-btn primary" onclick="openPlayerChooser(${{idx}}); closeModal('detailModal');">
      <span class="icon"><svg><use href="#i-play"/></svg></span> Play
    </button>
    <button class="modal-btn secondary" onclick="copyToClipboard('${{escAttr(ch.stream_url)}}')">
      <span class="icon"><svg><use href="#i-copy"/></svg></span> Copy Link
    </button>
    <button class="modal-btn secondary" onclick="downloadSingleM3U(${{idx}})">
      <span class="icon"><svg><use href="#i-download"/></svg></span> Download
    </button>`;

  openModal('detailModal');
}}

// ===== PLAYER CHOOSER =====
function openPlayerChooser(idx) {{
  const ch = CHANNELS[idx];
  const list = document.getElementById('playerList');

  list.innerHTML = PLAYERS.map((p, pi) => `
    <button class="player-option" onclick="launchPlayer(${{idx}}, ${{pi}})">
      <div class="player-option-icon">
        <span class="icon"><svg><use href="#i-play"/></svg></span>
      </div>
      <div class="player-option-info">
        <div class="player-option-name">${{escHtml(p.name)}}</div>
        <div class="player-option-desc">${{escHtml(p.desc)}}</div>
      </div>
      <span class="icon" style="color:var(--text-muted)"><svg><use href="#i-external"/></svg></span>
    </button>
  `).join('');

  openModal('playerModal');
}}

function launchPlayer(chIdx, playerIdx) {{
  const ch = CHANNELS[chIdx];
  const player = PLAYERS[playerIdx];
  const url = ch.stream_url;

  if (player.scheme === 'browser') {{
    window.open(url, '_blank');
    closeModal('playerModal');
    return;
  }}

  // Android intent
  const isMobile = /android/i.test(navigator.userAgent);

  if (isMobile && player.package) {{
    // Try intent URI
    const intentUrl = `intent:${{url}}#Intent;` +
      `package=${{player.package}};` +
      `type=video/*;` +
      `S.title=${{encodeURIComponent(ch.title)}};` +
      (player.playStore ? `S.browser_fallback_url=${{encodeURIComponent(player.playStore)}};` : '') +
      `end`;
    window.location.href = intentUrl;
  }} else if (player.scheme === 'vlc') {{
    window.open(`vlc://${{url}}`, '_blank');
  }} else {{
    // Fallback: just open
    window.open(url, '_blank');
  }}

  closeModal('playerModal');
  showToast(`Opening in ${{player.name}}...`);
}}

// ===== PLAYLISTS =====
function renderPlaylists() {{
  const list = document.getElementById('playlistList');

  // Add "All" playlist
  const allItem = {{
    name: 'All',
    filename: 'All.m3u',
    count: CHANNELS.length
  }};

  const items = [allItem, ...PLAYLISTS];

  list.innerHTML = items.map(pl => `
    <div class="playlist-item">
      <div class="playlist-icon">
        <span class="icon"><svg><use href="#i-list"/></svg></span>
      </div>
      <div class="playlist-info">
        <div class="playlist-name">${{escHtml(pl.name)}}</div>
        <div class="playlist-count">${{pl.count}} channel(s)</div>
      </div>
      <div class="playlist-actions">
        <button class="action-btn" onclick="copyPlaylistLink('${{escAttr(pl.filename)}}')">
          <span class="icon"><svg><use href="#i-link"/></svg></span> Copy Link
        </button>
        <button class="action-btn" onclick="downloadPlaylistFile('${{escAttr(pl.filename)}}')">
          <span class="icon"><svg><use href="#i-download"/></svg></span> Download
        </button>
        <button class="action-btn" onclick="openPlaylistInPlayer('${{escAttr(pl.filename)}}')">
          <span class="icon"><svg><use href="#i-play"/></svg></span> Open
        </button>
      </div>
    </div>
  `).join('');
}}

function copyPlaylistLink(filename) {{
  let url;
  if (REPO_BASE) {{
    url = REPO_BASE + '/output/' + filename;
  }} else {{
    url = window.location.origin + window.location.pathname.replace(/\\/[^\\/]*$/, '') + '/output/' + filename;
  }}
  // Try raw github URL format
  const rawMatch = window.location.href.match(/github\\.io\\/([^/]+)/);
  if (rawMatch) {{
    // Construct raw URL - user needs to configure REPO_BASE
    copyToClipboard(url);
  }} else {{
    copyToClipboard(url);
  }}
}}

function downloadPlaylistFile(filename) {{
  // Build M3U content from channels matching this playlist
  let entries;
  if (filename === 'All.m3u') {{
    entries = CHANNELS;
  }} else {{
    const name = filename.replace('.m3u', '');
    entries = CHANNELS.filter(ch => ch.group === name);
  }}

  let content = '#EXTM3U\\n';
  entries.forEach(ch => {{
    const logo = ch.thumbnail_url ? ` tvg-logo="${{ch.thumbnail_url}}"` : '';
    const group = ch.group_title_combined || ch.group;
    content += `#EXTINF:-1 tvg-name="${{ch.title}}"${{logo}} group-title="${{group}}",${{ch.title}}\\n`;
    content += ch.stream_url + '\\n';
  }});

  downloadFile(filename, content, 'audio/x-mpegurl');
}}

function openPlaylistInPlayer(filename) {{
  // For mobile: generate and open
  const isMobile = /android/i.test(navigator.userAgent);
  if (isMobile) {{
    downloadPlaylistFile(filename);
  }} else {{
    downloadPlaylistFile(filename);
  }}
}}

// ===== SOURCES =====
function renderSources() {{
  const container = document.getElementById('sourcesList');
  let html = '';

  if (SOURCES.folders.length > 0) {{
    html += `<div class="source-section">
      <div class="source-section-title">
        <span class="icon"><svg><use href="#i-folder"/></svg></span>
        Pixeldrain Folders
      </div>`;
    SOURCES.folders.forEach(([g, u]) => {{
      html += `<div class="source-item">
        <span class="source-item-name">${{escHtml(g)}}</span>
        <span class="source-item-url">${{escHtml(u)}}</span>
        <button class="action-btn" onclick="copyToClipboard('${{escAttr(u)}}')">
          <span class="icon"><svg><use href="#i-copy"/></svg></span>
        </button>
      </div>`;
    }});
    html += '</div>';
  }}

  if (SOURCES.singles.length > 0) {{
    html += `<div class="source-section">
      <div class="source-section-title">
        <span class="icon"><svg><use href="#i-file"/></svg></span>
        Pixeldrain Single Files
      </div>`;
    SOURCES.singles.forEach(([g, u]) => {{
      html += `<div class="source-item">
        <span class="source-item-name">${{escHtml(g)}}</span>
        <span class="source-item-url">${{escHtml(u)}}</span>
        <button class="action-btn" onclick="copyToClipboard('${{escAttr(u)}}')">
          <span class="icon"><svg><use href="#i-copy"/></svg></span>
        </button>
      </div>`;
    }});
    html += '</div>';
  }}

  if (SOURCES.playlists.length > 0) {{
    html += `<div class="source-section">
      <div class="source-section-title">
        <span class="icon"><svg><use href="#i-globe"/></svg></span>
        External M3U Playlists
      </div>`;
    SOURCES.playlists.forEach(([g, u]) => {{
      html += `<div class="source-item">
        <span class="source-item-name">${{escHtml(g)}}</span>
        <span class="source-item-url">${{escHtml(u)}}</span>
        <button class="action-btn" onclick="copyToClipboard('${{escAttr(u)}}')">
          <span class="icon"><svg><use href="#i-copy"/></svg></span>
        </button>
      </div>`;
    }});
    html += '</div>';
  }}

  if (!html) {{
    html = '<div class="empty-state"><span class="icon"><svg><use href="#i-database"/></svg></span><p>No sources configured</p></div>';
  }}

  container.innerHTML = html;
}}

// ===== DOWNLOAD SELECTED =====
function downloadSelected() {{
  if (selectedChannels.size === 0) return;

  const entries = CHANNELS.filter(ch => selectedChannels.has(getChannelKey(ch)));
  let content = '#EXTM3U\\n';
  entries.forEach(ch => {{
    const logo = ch.thumbnail_url ? ` tvg-logo="${{ch.thumbnail_url}}"` : '';
    const group = ch.group_title_combined || ch.group;
    content += `#EXTINF:-1 tvg-name="${{ch.title}}"${{logo}} group-title="${{group}}",${{ch.title}}\\n`;
    content += ch.stream_url + '\\n';
  }});

  downloadFile('selected_channels.m3u', content, 'audio/x-mpegurl');
  showToast(`Downloaded ${{entries.length}} channel(s)`);
}}

function downloadSingleM3U(idx) {{
  const ch = CHANNELS[idx];
  const logo = ch.thumbnail_url ? ` tvg-logo="${{ch.thumbnail_url}}"` : '';
  const group = ch.group_title_combined || ch.group;
  const content = `#EXTM3U\\n#EXTINF:-1 tvg-name="${{ch.title}}"${{logo}} group-title="${{group}}",${{ch.title}}\\n${{ch.stream_url}}\\n`;
  const safeName = ch.title.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 50);
  downloadFile(safeName + '.m3u', content, 'audio/x-mpegurl');
}}

// ===== UTILITIES =====
function downloadFile(filename, content, mime) {{
  const blob = new Blob([content], {{ type: mime }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

function copyToClipboard(text) {{
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(() => {{
      showToast('Copied to clipboard');
    }}).catch(() => {{
      fallbackCopy(text);
    }});
  }} else {{
    fallbackCopy(text);
  }}
}}

function fallbackCopy(text) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
  document.body.appendChild(ta);
  ta.select();
  try {{
    document.execCommand('copy');
    showToast('Copied to clipboard');
  }} catch(e) {{
    showToast('Failed to copy');
  }}
  document.body.removeChild(ta);
}}

function showToast(msg) {{
  const toast = document.getElementById('toast');
  document.getElementById('toastMsg').textContent = msg;
  toast.classList.add('visible');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => {{
    toast.classList.remove('visible');
  }}, 2500);
}}

function openModal(id) {{
  document.getElementById(id).classList.add('visible');
  document.body.style.overflow = 'hidden';
}}

function closeModal(id) {{
  document.getElementById(id).classList.remove('visible');
  document.body.style.overflow = '';
}}

function escHtml(str) {{
  if (!str) return '';
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}}

function escAttr(str) {{
  if (!str) return '';
  return str.replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'").replace(/"/g, '&quot;');
}}
</script>
</body>
</html>'''

    return html


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

    # Store source configs for the website
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
    old_html = OUTPUT_DIR / "index.html"
    if old_html.exists():
        old_html.unlink()

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

    # ---- Generate website ----
    log.info("")
    log.info("Generating website (index.html)...")
    html_content = generate_website(
        all_entries=all_entries,
        group_entries=group_entries,
        playlist_files=playlist_files,
        source_configs=source_configs,
        repo_base_url="",
    )
    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(html_content, encoding="utf-8")
    log.info("  Website written to %s", html_path)

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

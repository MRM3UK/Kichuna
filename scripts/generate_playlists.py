#!/usr/bin/env python3
"""
M3U Playlist Generator + Website Builder
Fetches videos from Pixeldrain and external M3U playlists,
generates M3U files and a premium single-page website dashboard.
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


_EXTINF_FULL_RE = re.compile(r'#EXTINF\s*:\s*(-?\d+)\s*(.*?)\s*,\s*(.*)', re.DOTALL)
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

    template = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<meta name="theme-color" content="#0a0e1a">
<title>Stream Dashboard</title>
<style>
/*===RESET===*/
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
--bg:#0a0e1a;--bg2:#111827;--bg3:#1f2937;--bg4:#283348;
--glass:rgba(17,24,39,0.75);--glass2:rgba(31,41,55,0.6);
--t1:#f9fafb;--t2:#d1d5db;--t3:#9ca3af;--t4:#6b7280;
--acc:#6366f1;--acc2:#818cf8;--accg:linear-gradient(135deg,#6366f1,#8b5cf6);
--accgl:rgba(99,102,241,0.12);
--green:#10b981;--red:#ef4444;--amber:#f59e0b;--sky:#0ea5e9;
--bdr:rgba(75,85,99,0.4);--bdr2:rgba(99,102,241,0.3);
--r:16px;--r2:12px;--r3:8px;--r4:6px;
--sh:0 8px 32px rgba(0,0,0,0.4);--sh2:0 4px 16px rgba(0,0,0,0.3);
--tr:all .25s cubic-bezier(.4,0,.2,1);
--font:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
--mono:'JetBrains Mono','SF Mono',Consolas,monospace;
}
html{font-size:16px;scroll-behavior:smooth;-webkit-tap-highlight-color:transparent}
body{font-family:var(--font);background:var(--bg);color:var(--t1);min-height:100vh;line-height:1.6;overflow-x:hidden}
body::before{content:'';position:fixed;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(circle at 30% 20%,rgba(99,102,241,0.06),transparent 50%),radial-gradient(circle at 70% 80%,rgba(139,92,246,0.04),transparent 50%);pointer-events:none;z-index:0}
a{color:var(--acc2);text-decoration:none;transition:color .2s}
a:hover{color:var(--acc)}
button{cursor:pointer;border:none;background:none;font-family:var(--font);color:inherit}
input,select{font-family:var(--font)}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}

/*===ICONS===*/
.i{display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.i svg{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.i-16 svg{width:16px;height:16px} .i-18 svg{width:18px;height:18px}
.i-20 svg{width:20px;height:20px} .i-24 svg{width:24px;height:24px}
.i-32 svg{width:32px;height:32px} .i-48 svg{width:48px;height:48px}

/*===LAYOUT===*/
.wrap{max-width:1280px;margin:0 auto;padding:0 16px;position:relative;z-index:1}

/*===HEADER===*/
.hdr{background:var(--glass);backdrop-filter:blur(20px) saturate(1.5);border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:100;padding:16px 0}
.hdr-in{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.hdr-brand{display:flex;align-items:center;gap:10px}
.hdr-logo{width:40px;height:40px;background:var(--accg);border-radius:var(--r3);display:flex;align-items:center;justify-content:center;color:white;box-shadow:0 0 20px rgba(99,102,241,0.3)}
.hdr-t{font-size:1.25rem;font-weight:800;letter-spacing:-0.5px;background:var(--accg);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hdr-sub{font-size:.7rem;color:var(--t4);display:flex;align-items:center;gap:4px;margin-top:2px}
.hdr-stats{display:flex;gap:8px;flex-wrap:wrap}
.st{display:flex;align-items:center;gap:6px;padding:8px 14px;background:var(--glass2);border:1px solid var(--bdr);border-radius:24px;backdrop-filter:blur(8px)}
.st-n{font-size:1.1rem;font-weight:800;color:var(--acc2)}
.st-l{font-size:.7rem;color:var(--t4);text-transform:uppercase;letter-spacing:.5px;font-weight:600}

/*===TABS===*/
.tabs{display:flex;gap:4px;padding:16px 0 8px;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tb{display:flex;align-items:center;gap:8px;padding:10px 18px;border-radius:var(--r2);font-size:.85rem;font-weight:600;color:var(--t3);transition:var(--tr);white-space:nowrap;position:relative;border:1px solid transparent}
.tb:hover{color:var(--t1);background:var(--bg3)}
.tb.on{color:var(--acc2);background:var(--accgl);border-color:var(--bdr2)}
.tb.on::after{content:'';position:absolute;bottom:-1px;left:20%;right:20%;height:2px;background:var(--accg);border-radius:1px}
.tb-c{background:var(--bg);padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:700;margin-left:2px}
.tb.on .tb-c{background:rgba(99,102,241,0.2);color:var(--acc2)}

/*===TOOLBAR===*/
.tbar{display:flex;gap:8px;padding:8px 0 12px;flex-wrap:wrap;align-items:stretch}
.sbox{flex:1;min-width:220px;position:relative}
.sbox .i{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--t4);pointer-events:none}
.sbox input{width:100%;padding:11px 14px 11px 42px;background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r2);color:var(--t1);font-size:.9rem;outline:none;transition:var(--tr)}
.sbox input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--accgl)}
.sbox input::placeholder{color:var(--t4)}
.fbtn{display:flex;align-items:center;gap:6px;padding:10px 14px;background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r2);color:var(--t3);font-size:.82rem;font-weight:600;transition:var(--tr);white-space:nowrap}
.fbtn:hover{color:var(--t1);border-color:var(--acc);background:var(--bg3)}
.fbtn.on{color:var(--acc2);border-color:var(--bdr2);background:var(--accgl)}
.abtn{display:flex;align-items:center;gap:6px;padding:10px 16px;border-radius:var(--r2);font-size:.82rem;font-weight:700;transition:var(--tr);white-space:nowrap}
.abtn-p{background:var(--accg);color:white;box-shadow:0 4px 12px rgba(99,102,241,0.3)}
.abtn-p:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(99,102,241,0.4)}
.abtn-s{background:var(--bg2);color:var(--t2);border:1px solid var(--bdr)}
.abtn-s:hover{border-color:var(--acc);color:var(--t1)}
select.fbtn{appearance:none;padding-right:32px;background-image:url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%239ca3af' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center}

/*===SELECT===*/
.sel-row{display:flex;align-items:center;gap:10px;padding:4px 0 8px;font-size:.82rem;color:var(--t3)}
.sel-row label{display:flex;align-items:center;gap:6px;cursor:pointer}
.sel-row input{accent-color:var(--acc);width:16px;height:16px}
.sel-cnt{color:var(--acc2);font-weight:700}

/*===GRID===*/
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding-bottom:24px;animation:fadeUp .4s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}

/*===CARD===*/
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r);overflow:hidden;transition:var(--tr);position:relative}
.card:hover{border-color:var(--bdr2);transform:translateY(-3px);box-shadow:var(--sh)}
.card.picked{border-color:var(--acc);box-shadow:0 0 0 2px var(--accgl)}
.card-ck{position:absolute;top:10px;left:10px;z-index:5;width:18px;height:18px;accent-color:var(--acc);cursor:pointer;opacity:.7;transition:opacity .2s}
.card:hover .card-ck,.card.picked .card-ck{opacity:1}
.card-img{position:relative;width:100%;padding-top:56.25%;background:linear-gradient(135deg,#0c1222,#141c2e);overflow:hidden}
.card-img img{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;transition:transform .4s ease,opacity .3s}
.card:hover .card-img img{transform:scale(1.06)}
.card-img .ph{position:absolute;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--t4)}
.card-img .ph .i svg{opacity:.3}
.res{position:absolute;top:10px;right:10px;padding:3px 10px;border-radius:4px;font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.5px;backdrop-filter:blur(4px)}
.res-2160p{background:rgba(239,68,68,.85);color:white}
.res-1440p{background:rgba(234,88,12,.85);color:white}
.res-1080p{background:rgba(16,185,129,.85);color:white}
.res-720p{background:rgba(14,165,233,.85);color:white}
.res-480p{background:rgba(139,92,246,.85);color:white}
.res-def{background:rgba(75,85,99,.85);color:white}
.card-play{position:absolute;bottom:10px;right:10px;width:40px;height:40px;background:var(--accg);border-radius:50%;display:flex;align-items:center;justify-content:center;color:white;box-shadow:0 4px 16px rgba(99,102,241,0.4);opacity:0;transform:scale(0.8);transition:all .3s ease}
.card:hover .card-play{opacity:1;transform:scale(1)}
.card-play:hover{transform:scale(1.1)!important}
.card-body{padding:14px}
.card-t{font-size:.88rem;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:8px}
.card-meta{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tag{padding:3px 8px;border-radius:4px;font-size:.68rem;font-weight:700;letter-spacing:.3px}
.tg-grp{background:var(--accgl);color:var(--acc2)}
.tg-src{background:rgba(16,185,129,.12);color:var(--green)}
.tg-sz{background:rgba(156,163,175,.12);color:var(--t3)}
.tg-res{background:rgba(245,158,11,.12);color:var(--amber)}
.card-acts{display:flex;gap:5px}
.cb{flex:1;display:flex;align-items:center;justify-content:center;gap:5px;padding:9px;border-radius:var(--r4);font-size:.73rem;font-weight:700;transition:var(--tr)}
.cb-play{background:var(--accg);color:white}
.cb-play:hover{box-shadow:0 4px 12px rgba(99,102,241,0.3)}
.cb-copy{background:var(--bg);border:1px solid var(--bdr);color:var(--t2)}
.cb-copy:hover{border-color:var(--acc);color:var(--acc2)}
.cb-info{background:var(--bg);border:1px solid var(--bdr);color:var(--t2)}
.cb-info:hover{border-color:var(--acc);color:var(--acc2)}

/*===PANELS===*/
.pan{display:none;animation:fadeUp .3s ease}
.pan.on{display:block}

/*===PLAYLISTS===*/
.pl-list{display:flex;flex-direction:column;gap:10px;padding:16px 0}
.pl-item{display:flex;align-items:center;gap:14px;padding:16px 18px;background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r);transition:var(--tr)}
.pl-item:hover{border-color:var(--bdr2);box-shadow:var(--sh2)}
.pl-ico{width:48px;height:48px;background:var(--accg);border-radius:var(--r2);display:flex;align-items:center;justify-content:center;color:white;flex-shrink:0}
.pl-info{flex:1;min-width:0}
.pl-name{font-weight:700;font-size:.95rem;margin-bottom:2px}
.pl-cnt{font-size:.78rem;color:var(--t4)}
.pl-acts{display:flex;gap:6px;flex-wrap:wrap}

/*===SOURCES===*/
.src-sec{margin-bottom:24px}
.src-hd{display:flex;align-items:center;gap:10px;font-size:1rem;font-weight:800;color:var(--t1);padding:14px 0 10px;border-bottom:1px solid var(--bdr);margin-bottom:10px}
.src-hd .i{color:var(--acc2)}
.src-item{display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r3);margin-bottom:6px;transition:var(--tr)}
.src-item:hover{border-color:var(--bdr2)}
.src-n{font-weight:700;font-size:.88rem;min-width:130px;flex-shrink:0}
.src-u{flex:1;font-family:var(--mono);font-size:.72rem;color:var(--t4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/*===MODAL===*/
.ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:1000;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(6px);animation:fadeIn .2s ease}
.ov.vis{display:flex}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.mdl{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r);width:100%;max-width:560px;max-height:90vh;display:flex;flex-direction:column;box-shadow:var(--sh);animation:slideUp .3s ease}
@keyframes slideUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
.mdl-hd{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--bdr)}
.mdl-t{font-size:1.05rem;font-weight:800}
.mdl-x{width:36px;height:36px;display:flex;align-items:center;justify-content:center;border-radius:var(--r3);color:var(--t4);transition:var(--tr)}
.mdl-x:hover{background:var(--bg);color:var(--t1)}
.mdl-bd{padding:22px;overflow-y:auto;flex:1}
.d-thumb{width:100%;border-radius:var(--r2);aspect-ratio:16/9;object-fit:cover;margin-bottom:18px;background:var(--bg)}
.d-row{display:flex;padding:10px 0;border-bottom:1px solid rgba(75,85,99,.25);gap:14px;align-items:flex-start}
.d-row:last-child{border-bottom:none}
.d-lbl{font-size:.72rem;color:var(--t4);font-weight:700;min-width:100px;text-transform:uppercase;letter-spacing:.6px;flex-shrink:0;padding-top:2px}
.d-val{font-size:.85rem;color:var(--t1);word-break:break-all;flex:1;line-height:1.5}
.d-val.mono{font-family:var(--mono);font-size:.76rem;color:var(--t3)}
.mdl-ft{display:flex;gap:8px;padding:16px 22px;border-top:1px solid var(--bdr);flex-wrap:wrap}
.mb{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:11px 18px;border-radius:var(--r3);font-size:.85rem;font-weight:700;transition:var(--tr);min-width:90px}

/*===PLAYER===*/
.pls{display:flex;flex-direction:column;gap:8px}
.po{display:flex;align-items:center;gap:12px;width:100%;padding:14px 16px;background:var(--bg);border:1px solid var(--bdr);border-radius:var(--r2);transition:var(--tr);text-align:left}
.po:hover{border-color:var(--acc);background:var(--bg3);transform:translateX(4px)}
.po-ico{width:44px;height:44px;background:var(--accgl);border-radius:var(--r3);display:flex;align-items:center;justify-content:center;color:var(--acc2);flex-shrink:0}
.po-inf{flex:1}
.po-n{font-weight:700;font-size:.9rem}
.po-d{font-size:.73rem;color:var(--t4)}
.po-arr{color:var(--t4);transition:var(--tr)}
.po:hover .po-arr{color:var(--acc2);transform:translateX(2px)}

/*===TOAST===*/
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--bg3);color:var(--t1);padding:14px 24px;border-radius:var(--r2);border:1px solid var(--bdr);box-shadow:var(--sh);z-index:2000;transition:transform .35s cubic-bezier(.4,0,.2,1);display:flex;align-items:center;gap:10px;max-width:90vw;backdrop-filter:blur(12px)}
.toast.vis{transform:translateX(-50%) translateY(0)}
.toast .i{color:var(--green);flex-shrink:0}

/*===DLBAR===*/
.dlbar{position:fixed;bottom:0;left:0;right:0;background:var(--glass);backdrop-filter:blur(20px) saturate(1.5);border-top:1px solid var(--bdr2);padding:14px 20px;z-index:200;display:none;align-items:center;justify-content:space-between;gap:14px;box-shadow:0 -8px 32px rgba(0,0,0,0.4)}
.dlbar.vis{display:flex}
.dlbar-info{font-size:.88rem;color:var(--t2)}
.dlbar-info strong{color:var(--acc2);font-size:1.1rem}
.dlbar-acts{display:flex;gap:8px}

/*===EMPTY===*/
.empty{text-align:center;padding:60px 20px;color:var(--t4)}
.empty .i{margin-bottom:16px;opacity:.3}
.empty p{font-size:1rem;font-weight:600;color:var(--t3);margin-bottom:6px}
.empty small{font-size:.82rem}

/*===RESPONSIVE===*/
@media(max-width:768px){
  .hdr-in{flex-direction:column;align-items:flex-start}
  .grid{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
  .tbar{flex-direction:column;align-items:stretch}
  .sbox{min-width:100%}
  .mdl{max-width:100%;border-radius:var(--r) var(--r) 0 0;max-height:92vh}
  .ov{align-items:flex-end;padding:0}
  .dlbar{flex-direction:column;text-align:center;gap:10px}
  .pl-item{flex-wrap:wrap}
  .pl-acts{width:100%}
  .pl-acts .abtn{flex:1}
}
@media(max-width:400px){
  .grid{grid-template-columns:1fr}
  .card-acts{flex-wrap:wrap}
}
</style>
</head>
<body>

<svg style="display:none" xmlns="http://www.w3.org/2000/svg">
<symbol id="s-tv" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="15" rx="2"/><polyline points="17 2 12 7 7 2"/></symbol>
<symbol id="s-play" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21"/></symbol>
<symbol id="s-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></symbol>
<symbol id="s-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></symbol>
<symbol id="s-srch" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></symbol>
<symbol id="s-dl" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
<symbol id="s-list" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></symbol>
<symbol id="s-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
<symbol id="s-file" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></symbol>
<symbol id="s-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10A15.3 15.3 0 0 1 12 2z"/></symbol>
<symbol id="s-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></symbol>
<symbol id="s-x" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></symbol>
<symbol id="s-check" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></symbol>
<symbol id="s-ext" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></symbol>
<symbol id="s-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
<symbol id="s-film" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/></symbol>
<symbol id="s-db" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></symbol>
<symbol id="s-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></symbol>
<symbol id="s-chevr" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></symbol>
<symbol id="s-star" viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></symbol>
</svg>

<header class="hdr">
  <div class="wrap">
    <div class="hdr-in">
      <div class="hdr-brand">
        <div class="hdr-logo"><span class="i i-24"><svg><use href="#s-tv"/></svg></span></div>
        <div>
          <div class="hdr-t">Stream Dashboard</div>
          <div class="hdr-sub">
            <span class="i i-16"><svg><use href="#s-clock"/></svg></span>
            __UPDATE_TIME__
          </div>
        </div>
      </div>
      <div class="hdr-stats">
        <div class="st"><div><div class="st-n" id="statCh">__TOTAL_CHANNELS__</div><div class="st-l">Channels</div></div></div>
        <div class="st"><div><div class="st-n" id="statGr">__TOTAL_GROUPS__</div><div class="st-l">Groups</div></div></div>
        <div class="st"><div><div class="st-n" id="statPl">__TOTAL_PLAYLISTS__</div><div class="st-l">Playlists</div></div></div>
      </div>
    </div>
  </div>
</header>

<div class="wrap">
  <nav class="tabs" id="navTabs">
    <button class="tb on" data-tab="channels"><span class="i i-16"><svg><use href="#s-grid"/></svg></span>Channels<span class="tb-c" id="chCnt">__TOTAL_CHANNELS__</span></button>
    <button class="tb" data-tab="playlists"><span class="i i-16"><svg><use href="#s-list"/></svg></span>Playlists<span class="tb-c">__TOTAL_PLAYLISTS__</span></button>
    <button class="tb" data-tab="sources"><span class="i i-16"><svg><use href="#s-db"/></svg></span>Sources</button>
  </nav>

  <div class="pan on" id="p-channels">
    <div class="tbar">
      <div class="sbox"><span class="i i-18"><svg><use href="#s-srch"/></svg></span><input type="text" id="inSearch" placeholder="Search channels, groups..."></div>
      <button class="fbtn on" data-f="all" onclick="sf('all')"><span class="i i-16"><svg><use href="#s-grid"/></svg></span>All</button>
      <button class="fbtn" data-f="Folder" onclick="sf('Folder')"><span class="i i-16"><svg><use href="#s-folder"/></svg></span>Folders</button>
      <button class="fbtn" data-f="Single" onclick="sf('Single')"><span class="i i-16"><svg><use href="#s-file"/></svg></span>Singles</button>
      <button class="fbtn" data-f="M3U" onclick="sf('M3U')"><span class="i i-16"><svg><use href="#s-globe"/></svg></span>M3U</button>
      <select id="selGroup" class="fbtn"><option value="all">All Groups</option></select>
      <select id="selRes" class="fbtn"><option value="all">All Quality</option><option value="2160p">4K / 2160p</option><option value="1440p">1440p</option><option value="1080p">1080p</option><option value="720p">720p</option><option value="480p">480p</option></select>
    </div>
    <div class="sel-row">
      <label><input type="checkbox" id="ckAll"> Select all visible</label>
      <span class="sel-cnt" id="selCnt"></span>
    </div>
    <div class="grid" id="chGrid"></div>
  </div>

  <div class="pan" id="p-playlists"><div class="pl-list" id="plList"></div></div>
  <div class="pan" id="p-sources"><div id="srcList"></div></div>
</div>

<div class="dlbar" id="dlBar">
  <div class="dlbar-info"><strong id="dlCnt">0</strong> channel(s) selected</div>
  <div class="dlbar-acts">
    <button class="abtn abtn-s" onclick="clearSel()"><span class="i i-16"><svg><use href="#s-x"/></svg></span>Clear</button>
    <button class="abtn abtn-p" onclick="dlSelected()"><span class="i i-16"><svg><use href="#s-dl"/></svg></span>Download M3U</button>
  </div>
</div>

<div class="ov" id="mDetail">
  <div class="mdl">
    <div class="mdl-hd"><span class="mdl-t">Channel Details</span><button class="mdl-x" onclick="cm('mDetail')"><span class="i i-20"><svg><use href="#s-x"/></svg></span></button></div>
    <div class="mdl-bd" id="dBody"></div>
    <div class="mdl-ft" id="dFoot"></div>
  </div>
</div>

<div class="ov" id="mPlayer">
  <div class="mdl">
    <div class="mdl-hd"><span class="mdl-t">Choose Player</span><button class="mdl-x" onclick="cm('mPlayer')"><span class="i i-20"><svg><use href="#s-x"/></svg></span></button></div>
    <div class="mdl-bd"><div class="pls" id="plrList"></div></div>
  </div>
</div>

<div class="toast" id="toast"><span class="i i-18"><svg><use href="#s-check"/></svg></span><span id="toastMsg"></span></div>

<script>
var CH=__CHANNELS_DATA__,
    PL=__PLAYLISTS_DATA__,
    SR=__SOURCES_DATA__;
var cFilt='all',cGrp='all',cRes='all',q='',sel=new Set();

var PLAYERS=[
{name:"Leone Play",pkg:"com.genuine.leone",desc:"Feature-rich streaming player",ps:"https://play.google.com/store/apps/details?id=com.genuine.leone"},
{name:"VLC Media Player",pkg:"org.videolan.vlc",desc:"Universal open-source player",ps:"https://play.google.com/store/apps/details?id=org.videolan.vlc"},
{name:"MX Player",pkg:"com.mxtech.videoplayer.ad",desc:"Popular Android video player",ps:"https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad"},
{name:"MX Player Pro",pkg:"com.mxtech.videoplayer.pro",desc:"Ad-free MX Player",ps:"https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.pro"},
{name:"Just (Video) Player",pkg:"com.brouken.player",desc:"Simple & clean player",ps:"https://play.google.com/store/apps/details?id=com.brouken.player"},
{name:"IPTV Smarters Pro",pkg:"com.nst.iptvsmarterstvbox",desc:"IPTV playlist player",ps:"https://play.google.com/store/apps/details?id=com.nst.iptvsmarterstvbox"},
{name:"OTT Navigator",pkg:"studio.scillarium.ottnavigator",desc:"Advanced IPTV/OTT player",ps:"https://play.google.com/store/apps/details?id=studio.scillarium.ottnavigator"},
{name:"Open in Browser",pkg:null,desc:"Stream directly in browser",ps:null}
];

document.addEventListener('DOMContentLoaded',function(){
  fillGroups();render();renderPL();renderSR();
  document.getElementById('inSearch').addEventListener('input',function(e){q=e.target.value.toLowerCase().trim();render()});
  document.getElementById('selGroup').addEventListener('change',function(e){cGrp=e.target.value;render()});
  document.getElementById('selRes').addEventListener('change',function(e){cRes=e.target.value;render()});
  document.querySelectorAll('.tb').forEach(function(b){b.addEventListener('click',function(){
    document.querySelectorAll('.tb').forEach(function(x){x.classList.remove('on')});
    document.querySelectorAll('.pan').forEach(function(x){x.classList.remove('on')});
    b.classList.add('on');document.getElementById('p-'+b.dataset.tab).classList.add('on');
  })});
  document.getElementById('ckAll').addEventListener('change',function(e){
    var v=gf();if(e.target.checked)v.forEach(function(c){sel.add(c.stream_url)});
    else v.forEach(function(c){sel.delete(c.stream_url)});render();
  });
  document.querySelectorAll('.ov').forEach(function(o){o.addEventListener('click',function(e){if(e.target===o)cm(o.id)})});
});

function fillGroups(){
  var gs=[],seen={};CH.forEach(function(c){if(!seen[c.group]){seen[c.group]=1;gs.push(c.group)}});
  gs.sort();var s=document.getElementById('selGroup');
  gs.forEach(function(g){var o=document.createElement('option');o.value=g;o.textContent=g;s.appendChild(o)});
}

function sf(f){cFilt=f;document.querySelectorAll('.fbtn[data-f]').forEach(function(b){b.classList.toggle('on',b.dataset.f===f)});render()}

function gf(){
  return CH.filter(function(c){
    if(cFilt!=='all'&&c.source_type!==cFilt)return false;
    if(cGrp!=='all'&&c.group!==cGrp)return false;
    if(cRes!=='all'&&c.resolution!==cRes)return false;
    if(q&&c.title.toLowerCase().indexOf(q)===-1&&c.group.toLowerCase().indexOf(q)===-1)return false;
    return true;
  });
}

function esc(s){if(!s)return'';var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function escA(s){return(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}

function render(){
  var g=document.getElementById('chGrid'),v=gf();
  document.getElementById('chCnt').textContent=v.length;
  if(!v.length){g.innerHTML='<div class="empty" style="grid-column:1/-1"><span class="i i-48"><svg><use href="#s-srch"/></svg></span><p>No channels found</p><small>Try different search or filters</small></div>';ubar();return}
  var h='';
  v.forEach(function(c){
    var idx=CH.indexOf(c),isSel=sel.has(c.stream_url);
    var rc=c.resolution?'res-'+c.resolution:'res-def';
    var th=c.thumbnail_url?'<img src="'+esc(c.thumbnail_url)+'" loading="lazy" onerror="this.parentElement.innerHTML=\'<div class=ph><span class=\\\'i i-48\\\'><svg><use href=\\\'#s-film\\\'/></svg></span></div>\'">':'<div class="ph"><span class="i i-48"><svg><use href="#s-film"/></svg></span></div>';
    h+='<div class="card'+(isSel?' picked':'')+'">';
    h+='<input type="checkbox" class="card-ck"'+(isSel?' checked':'')+' onchange="tgl(\''+escA(c.stream_url)+'\')">';
    h+='<div class="card-img">'+th;
    if(c.resolution)h+='<span class="res '+rc+'">'+esc(c.resolution)+'</span>';
    h+='<button class="card-play" onclick="opl('+idx+')"><span class="i i-20"><svg><use href="#s-play"/></svg></span></button>';
    h+='</div><div class="card-body">';
    h+='<div class="card-t" title="'+esc(c.title)+'">'+esc(c.title)+'</div>';
    h+='<div class="card-meta"><span class="tag tg-grp">'+esc(c.group)+'</span><span class="tag tg-src">'+esc(c.source_type)+'</span>';
    if(c.file_size_formatted)h+='<span class="tag tg-sz">'+esc(c.file_size_formatted)+'</span>';
    if(c.resolution)h+='<span class="tag tg-res">'+esc(c.resolution)+'</span>';
    h+='</div><div class="card-acts">';
    h+='<button class="cb cb-play" onclick="opl('+idx+')"><span class="i i-16"><svg><use href="#s-play"/></svg></span>Play</button>';
    h+='<button class="cb cb-copy" onclick="cpx(\''+escA(c.stream_url)+'\')"><span class="i i-16"><svg><use href="#s-copy"/></svg></span>Copy</button>';
    h+='<button class="cb cb-info" onclick="det('+idx+')"><span class="i i-16"><svg><use href="#s-info"/></svg></span>Info</button>';
    h+='</div></div></div>';
  });
  g.innerHTML=h;ubar();
}

function tgl(u){if(sel.has(u))sel.delete(u);else sel.add(u);render()}
function clearSel(){sel.clear();document.getElementById('ckAll').checked=false;render()}
function ubar(){var b=document.getElementById('dlBar');document.getElementById('dlCnt').textContent=sel.size;
  document.getElementById('selCnt').textContent=sel.size?sel.size+' selected':'';
  b.classList.toggle('vis',sel.size>0)}

function det(i){
  var c=CH[i],b=document.getElementById('dBody'),f=document.getElementById('dFoot');
  var h='';
  if(c.thumbnail_url)h+='<img class="d-thumb" src="'+esc(c.thumbnail_url)+'" onerror="this.style.display=\'none\'">';
  h+='<div class="d-row"><span class="d-lbl">Title</span><span class="d-val">'+esc(c.title)+'</span></div>';
  h+='<div class="d-row"><span class="d-lbl">Group</span><span class="d-val">'+esc(c.group)+'</span></div>';
  h+='<div class="d-row"><span class="d-lbl">Source</span><span class="d-val">'+esc(c.source_type)+'</span></div>';
  h+='<div class="d-row"><span class="d-lbl">Resolution</span><span class="d-val">'+(c.resolution||'Unknown')+'</span></div>';
  h+='<div class="d-row"><span class="d-lbl">Stream URL</span><span class="d-val mono">'+esc(c.stream_url)+'</span></div>';
  if(c.file_size_formatted)h+='<div class="d-row"><span class="d-lbl">File Size</span><span class="d-val">'+c.file_size_formatted+'</span></div>';
  if(c.mime_type)h+='<div class="d-row"><span class="d-lbl">MIME Type</span><span class="d-val">'+esc(c.mime_type)+'</span></div>';
  if(c.original_filename)h+='<div class="d-row"><span class="d-lbl">Filename</span><span class="d-val">'+esc(c.original_filename)+'</span></div>';
  if(c.pd_file_id){
    h+='<div class="d-row"><span class="d-lbl">Pixeldrain</span><span class="d-val"><a href="https://pixeldrain.com/u/'+esc(c.pd_file_id)+'" target="_blank" rel="noopener">Open on Pixeldrain <span class="i i-16" style="display:inline"><svg><use href="#s-ext"/></svg></span></a></span></div>';
    h+='<div class="d-row"><span class="d-lbl">File ID</span><span class="d-val mono">'+esc(c.pd_file_id)+'</span></div>';
  }
  if(c.thumbnail_url)h+='<div class="d-row"><span class="d-lbl">Thumbnail</span><span class="d-val mono">'+esc(c.thumbnail_url)+'</span></div>';
  b.innerHTML=h;
  f.innerHTML='<button class="mb abtn-p" onclick="opl('+i+');cm(\'mDetail\')"><span class="i i-16"><svg><use href="#s-play"/></svg></span>Play</button><button class="mb abtn-s" onclick="cpx(\''+escA(c.stream_url)+'\')"><span class="i i-16"><svg><use href="#s-copy"/></svg></span>Copy Link</button><button class="mb abtn-s" onclick="dlOne('+i+')"><span class="i i-16"><svg><use href="#s-dl"/></svg></span>Download</button>';
  om('mDetail');
}

function opl(i){
  var c=CH[i],l=document.getElementById('plrList');
  var h='';
  PLAYERS.forEach(function(p,pi){
    h+='<button class="po" onclick="lnch('+i+','+pi+')">';
    h+='<div class="po-ico"><span class="i i-20"><svg><use href="#s-play"/></svg></span></div>';
    h+='<div class="po-inf"><div class="po-n">'+esc(p.name)+'</div><div class="po-d">'+esc(p.desc)+'</div></div>';
    h+='<span class="i i-16 po-arr"><svg><use href="#s-chevr"/></svg></span></button>';
  });
  l.innerHTML=h;om('mPlayer');
}

function lnch(ci,pi){
  var c=CH[ci],p=PLAYERS[pi],u=c.stream_url;
  cm('mPlayer');
  if(!p.pkg){window.open(u,'_blank');return}
  if(/android/i.test(navigator.userAgent)){
    var intent='intent:'+u+'#Intent;package='+p.pkg+';type=video/*;S.title='+encodeURIComponent(c.title)+';';
    if(p.ps)intent+='S.browser_fallback_url='+encodeURIComponent(p.ps)+';';
    intent+='end';
    window.location.href=intent;
  }else{window.open(u,'_blank')}
  toast('Opening in '+p.name+'...');
}

function renderPL(){
  var l=document.getElementById('plList'),items=[{name:'All',filename:'All.m3u',count:CH.length}].concat(PL);
  var h='';
  items.forEach(function(p){
    h+='<div class="pl-item">';
    h+='<div class="pl-ico"><span class="i i-24"><svg><use href="#s-list"/></svg></span></div>';
    h+='<div class="pl-info"><div class="pl-name">'+esc(p.name)+'</div><div class="pl-cnt">'+p.count+' channel(s)</div></div>';
    h+='<div class="pl-acts">';
    h+='<button class="abtn abtn-s" onclick="cpPL(\''+escA(p.filename)+'\')"><span class="i i-16"><svg><use href="#s-link"/></svg></span>Copy Link</button>';
    h+='<button class="abtn abtn-p" onclick="dlPL(\''+escA(p.filename)+'\')"><span class="i i-16"><svg><use href="#s-dl"/></svg></span>Download</button>';
    h+='</div></div>';
  });
  l.innerHTML=h;
}

function renderSR(){
  var c=document.getElementById('srcList'),h='';
  var map={folders:{icon:'s-folder',title:'Pixeldrain Folders'},singles:{icon:'s-file',title:'Single Files'},playlists:{icon:'s-globe',title:'External M3U Playlists'}};
  ['folders','singles','playlists'].forEach(function(k){
    if(!SR[k]||!SR[k].length)return;
    var m=map[k];
    h+='<div class="src-sec"><div class="src-hd"><span class="i i-20"><svg><use href="#'+m.icon+'"/></svg></span>'+m.title+'</div>';
    SR[k].forEach(function(s){
      h+='<div class="src-item"><span class="src-n">'+esc(s[0])+'</span><span class="src-u">'+esc(s[1])+'</span>';
      h+='<button class="abtn abtn-s" style="padding:6px 10px" onclick="cpx(\''+escA(s[1])+'\')"><span class="i i-16"><svg><use href="#s-copy"/></svg></span></button></div>';
    });
    h+='</div>';
  });
  c.innerHTML=h||'<div class="empty"><p>No sources configured</p></div>';
}

function dlPL(fn){
  var list=CH;
  if(fn!=='All.m3u'){var nm=fn.replace('.m3u','');list=CH.filter(function(c){return c.group===nm})}
  var t='#EXTM3U\n';
  list.forEach(function(c){
    var lg=c.thumbnail_url?' tvg-logo="'+c.thumbnail_url+'"':'';
    t+='#EXTINF:-1 tvg-name="'+c.title+'"'+lg+' group-title="'+c.group_title_combined+'",'+c.title+'\n'+c.stream_url+'\n';
  });
  dlFile(fn,t);
}

function cpPL(fn){
  var base=window.location.origin+window.location.pathname.replace(/\/[^\/]*$/,'')+'/output/'+fn;
  cpx(base);
}

function dlSelected(){
  if(!sel.size)return;
  var list=CH.filter(function(c){return sel.has(c.stream_url)});
  var t='#EXTM3U\n';
  list.forEach(function(c){
    var lg=c.thumbnail_url?' tvg-logo="'+c.thumbnail_url+'"':'';
    t+='#EXTINF:-1 tvg-name="'+c.title+'"'+lg+' group-title="'+c.group_title_combined+'",'+c.title+'\n'+c.stream_url+'\n';
  });
  dlFile('selected_'+sel.size+'_channels.m3u',t);
  toast('Downloaded '+sel.size+' channel(s)');
}

function dlOne(i){
  var c=CH[i];
  var lg=c.thumbnail_url?' tvg-logo="'+c.thumbnail_url+'"':'';
  var t='#EXTM3U\n#EXTINF:-1 tvg-name="'+c.title+'"'+lg+' group-title="'+c.group_title_combined+'",'+c.title+'\n'+c.stream_url+'\n';
  dlFile(c.title.replace(/[^a-zA-Z0-9]/g,'_').substring(0,60)+'.m3u',t);
}

function dlFile(fn,content){
  var b=new Blob([content],{type:'audio/x-mpegurl'});
  var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=fn;a.click();URL.revokeObjectURL(a.href);
}

function cpx(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){toast('Copied to clipboard')}).catch(function(){fbCopy(t)});
  }else{fbCopy(t)}
}

function fbCopy(t){
  var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';
  document.body.appendChild(a);a.select();
  try{document.execCommand('copy');toast('Copied to clipboard')}catch(e){toast('Copy failed')}
  document.body.removeChild(a);
}

function toast(m){
  var t=document.getElementById('toast');document.getElementById('toastMsg').textContent=m;
  t.classList.add('vis');clearTimeout(window._tt);window._tt=setTimeout(function(){t.classList.remove('vis')},2500);
}

function om(id){document.getElementById(id).classList.add('vis');document.body.style.overflow='hidden'}
function cm(id){document.getElementById(id).classList.remove('vis');document.body.style.overflow=''}
</script>
</body>
</html>"""

    template = template.replace("__CHANNELS_DATA__", json.dumps(channels_json, ensure_ascii=False))
    template = template.replace("__PLAYLISTS_DATA__", json.dumps(playlists_json, ensure_ascii=False))
    template = template.replace("__SOURCES_DATA__", json.dumps(sources_json, ensure_ascii=False))
    template = template.replace("__UPDATE_TIME__", update_time)
    template = template.replace("__TOTAL_CHANNELS__", str(total_channels))
    template = template.replace("__TOTAL_GROUPS__", str(total_groups))
    template = template.replace("__TOTAL_PLAYLISTS__", str(len(playlist_files)))

    return template


def main() -> None:
    log.info("=" * 60)
    log.info("M3U Playlist Generator + Website")
    log.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_entries: list[dict] = []
    group_entries: dict[str, list[dict]] = {}
    source_configs: dict[str, list[tuple[str, str]]] = {
        "folders": [], "singles": [], "playlists": [],
    }

    log.info("")
    log.info("[1/3] Pixeldrain FOLDERS...")
    folder_sources = parse_input_file(FOLDER_TXT)
    source_configs["folders"] = folder_sources
    for group, url in folder_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_pixeldrain_folder(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error: %s", e)

    log.info("")
    log.info("[2/3] Pixeldrain SINGLES...")
    single_sources = parse_input_file(FOLDER_S_TXT)
    source_configs["singles"] = single_sources
    for group, url in single_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_pixeldrain_single(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error: %s", e)

    log.info("")
    log.info("[3/3] External M3U...")
    playlist_sources = parse_input_file(PLAYLIST_TXT)
    source_configs["playlists"] = playlist_sources
    for group, url in playlist_sources:
        log.info("  -> %s :: %s", group, url)
        try:
            entries = process_external_m3u(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("Error: %s", e)

    log.info("")
    log.info("Cleaning old output...")
    for old in OUTPUT_DIR.glob("*.m3u"):
        old.unlink()

    log.info("Writing playlists...")
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

    if all_entries:
        write_m3u(OUTPUT_DIR / "All.m3u", all_entries, combined=True)

    log.info("Generating website...")
    html = generate_website(all_entries, group_entries, playlist_files, source_configs)
    (BASE_DIR / "index.html").write_text(html, encoding="utf-8")
    log.info("Website written to index.html")

    log.info("=" * 60)
    log.info("DONE — %d groups, %d entries", len(group_entries), len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)

#!/usr/bin/env python3
"""
============================================================
  M3U Playlist Generator
  Fetches videos from Pixeldrain folders, single files, and
  external M3U playlists, then generates organized M3U files.
============================================================
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
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

# Anything with vertical resolution > 1420 goes into [2160P] group
HIGH_RES_THRESHOLD = 1420

VIDEO_MIME_PREFIXES = ("video/",)

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".vob", ".3gp", ".3g2",
    ".mts", ".m2ts", ".divx", ".ogv", ".asf", ".f4v", ".rm",
    ".rmvb", ".mxf",
}

NON_VIDEO_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
    ".ico", ".tiff", ".tif", ".heic", ".heif",
    # Audio
    ".mp3", ".aac", ".flac", ".ogg", ".wav", ".wma", ".m4a",
    ".opus", ".ape",
    # Archives
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    # Documents
    ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls",
    ".xlsx", ".ppt", ".pptx", ".epub",
    # Executables / other
    ".exe", ".msi", ".apk", ".iso", ".dmg", ".deb", ".rpm",
}

# ============================================================
# LOGGING
# ============================================================

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
    """Create a requests session with retry/backoff configured."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (M3U-Playlist-Generator/2.0)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


SESSION = build_session()


# ============================================================
# RESOLUTION DETECTION
# ============================================================

_RES_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:3840\s*[x×]\s*2160|4096\s*[x×]\s*2160|7680\s*[x×]\s*4320)", re.I), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])8[kK](?:[\s._\-\])]|$)"), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])4[kK](?:[\s._\-\])]|$)"), "2160p"),
    (re.compile(r"(?:^|[\s._\-\[(])UHD(?:[\s._\-\])]|$)", re.I), "2160p"),
    (re.compile(r"\b4320[pPiI]\b"), "2160p"),
    (re.compile(r"\b2160[pPiI]\b"), "2160p"),
    (re.compile(r"2560\s*[x×]\s*1440", re.I), "1440p"),
    (re.compile(r"\b1440[pPiI]\b"), "1440p"),
    (re.compile(r"(?:^|[\s._\-\[(])2[kK](?:[\s._\-\])]|$)"), "1440p"),
    (re.compile(r"1920\s*[x×]\s*1080", re.I), "1080p"),
    (re.compile(r"\b1080[pPiI]\b"), "1080p"),
    (re.compile(r"(?:^|[\s._\-\[(])FHD(?:[\s._\-\])]|$)", re.I), "1080p"),
    (re.compile(r"1280\s*[x×]\s*720", re.I), "720p"),
    (re.compile(r"\b720[pPiI]\b"), "720p"),
    (re.compile(r"(?:^|[\s._\-\[(])HD(?:[\s._\-\])]|$)"), "720p"),
    (re.compile(r"854\s*[x×]\s*480", re.I), "480p"),
    (re.compile(r"\b480[pPiI]\b"), "480p"),
    (re.compile(r"\b360[pPiI]\b"), "360p"),
    (re.compile(r"\b240[pPiI]\b"), "240p"),
]


def detect_resolution(text: str) -> str:
    """Return normalized resolution like '2160p' or '' if not detected."""
    if not text:
        return ""
    for pattern, label in _RES_PATTERNS:
        if pattern.search(text):
            return label
    return ""


def resolution_pixels(res: str) -> int:
    """Convert '2160p' → 2160."""
    m = re.match(r"(\d+)", res or "")
    return int(m.group(1)) if m else 0


def is_high_res(resolution: str) -> bool:
    """True if resolution > 1420p (i.e., 1440p, 2160p, etc.)."""
    return resolution_pixels(resolution) > HIGH_RES_THRESHOLD


# ============================================================
# STRING / FILE HELPERS
# ============================================================

_UNSAFE_TITLE_RE = re.compile(r"[\x00-\x1f\x7f\r\n]")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(name: str) -> str:
    """Clean up title so it does not break the M3U line."""
    if not name:
        return ""
    cleaned = _UNSAFE_TITLE_RE.sub("", name)
    cleaned = cleaned.replace('"', "'").replace(",", " ")
    return cleaned.strip()


def sanitize_filename(name: str) -> str:
    """Make a safe filename from a group name."""
    safe = _UNSAFE_FILENAME_RE.sub("_", name).strip(". ")
    return safe or "Unnamed"


def strip_extension(filename: str) -> str:
    """Return filename without its extension."""
    return Path(filename).stem if filename else ""


def is_video_by_extension(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in VIDEO_EXTENSIONS


def is_video_by_mime(mime: str) -> bool:
    if not mime:
        return False
    return any(mime.lower().startswith(p) for p in VIDEO_MIME_PREFIXES)


def is_definitely_not_video(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in NON_VIDEO_EXTENSIONS


def unique_key(stream_url: str) -> str:
    return hashlib.sha256(stream_url.strip().lower().encode()).hexdigest()


# ============================================================
# ENTRY DATA STRUCTURE
# ============================================================

def make_entry(
    title: str,
    stream_url: str,
    thumbnail_url: str = "",
    group: str = "",
    resolution: str = "",
    source_type: str = "",
) -> dict:
    return {
        "title": title,
        "stream_url": stream_url,
        "thumbnail_url": thumbnail_url,
        "group": group,
        "resolution": resolution,
        "source_type": source_type,
    }


# ============================================================
# INPUT FILE PARSER
# ============================================================

def parse_input_file(path: Path) -> list[tuple[str, str]]:
    """
    Parse a TXT file with lines like:
        GROUP NAME - URL
    Split on the FIRST ' - ' (space-dash-space).
    Ignore blank lines and lines starting with '#'.
    """
    entries: list[tuple[str, str]] = []
    if not path.exists():
        log.warning("⚠️  Input file missing: %s", path)
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
            log.warning("  ↳ %s:%d — missing ' - ' separator: %s", path.name, lineno, line)
            continue

        group = line[:idx].strip()
        url = line[idx + 3:].strip()
        if not group or not url:
            log.warning("  ↳ %s:%d — empty group or URL", path.name, lineno)
            continue

        entries.append((group, url))

    log.info("📄 Parsed %d source(s) from %s", len(entries), path.name)
    return entries


# ============================================================
# PIXELDRAIN HELPERS
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
    """GET a Pixeldrain API endpoint, returning parsed JSON or None."""
    url = f"{PIXELDRAIN_API}{endpoint}"
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            log.warning("  ↳ 404 Not Found: %s", url)
            return None
        if resp.status_code != 200:
            log.warning("  ↳ HTTP %d for %s", resp.status_code, url)
            return None
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("  ↳ ⏱️  Timeout: %s", url)
    except requests.exceptions.ConnectionError as e:
        log.error("  ↳ 🔌 Connection error: %s (%s)", url, e)
    except requests.exceptions.JSONDecodeError:
        log.error("  ↳ 📄 Invalid JSON response: %s", url)
    except requests.RequestException as e:
        log.error("  ↳ ❌ Request failed: %s (%s)", url, e)
    except Exception as e:
        log.error("  ↳ ❌ Unexpected error: %s (%s)", url, e)
    finally:
        time.sleep(RATE_LIMIT_DELAY)
    return None


def pd_file_to_entry(file_info: dict, group: str, source_type: str) -> Optional[dict]:
    """Convert a Pixeldrain file info dict into an M3U entry."""
    if not file_info:
        return None

    name = file_info.get("name", "") or ""
    mime = file_info.get("mime_type", "") or ""
    file_id = file_info.get("id", "")

    if not file_id:
        return None

    # Skip obvious non-video extensions
    if is_definitely_not_video(name):
        log.debug("  ↳ Skip (non-video ext): %s", name)
        return None

    # Accept video by mime or by extension
    if not (is_video_by_mime(mime) or is_video_by_extension(name)):
        log.debug("  ↳ Skip (not video): %s [mime=%s]", name, mime)
        return None

    title = safe_title(strip_extension(name)) or file_id
    resolution = detect_resolution(name)

    return make_entry(
        title=title,
        stream_url=pd_stream_url(file_id),
        thumbnail_url=pd_thumbnail_url(file_id),
        group=group,
        resolution=resolution,
        source_type=source_type,
    )


# ============================================================
# SOURCE PROCESSORS
# ============================================================

def process_pixeldrain_folder(group: str, url: str) -> list[dict]:
    """Fetch all video files from a Pixeldrain folder."""
    folder_id = extract_pd_folder_id(url)
    if not folder_id:
        log.error("❌ Invalid Pixeldrain folder URL: %s", url)
        return []

    data = pd_api_get(f"/list/{folder_id}")
    if not data:
        return []

    files = data.get("files", [])
    log.info("  📁 Folder [%s]: %d file(s) in list", folder_id, len(files))

    entries: list[dict] = []
    for f in files:
        entry = pd_file_to_entry(f, group, source_type="Folder")
        if entry:
            entries.append(entry)

    log.info("  ✅ '%s' → %d video entrie(s)", group, len(entries))
    return entries


def process_pixeldrain_single(group: str, url: str) -> list[dict]:
    """Fetch a single Pixeldrain file."""
    file_id = extract_pd_file_id(url)
    if not file_id:
        log.error("❌ Invalid Pixeldrain file URL: %s", url)
        return []

    data = pd_api_get(f"/file/{file_id}/info")
    if not data:
        return []

    entry = pd_file_to_entry(data, group, source_type="Single")
    if entry:
        log.info("  ✅ '%s' → %s", group, entry["title"])
        return [entry]

    log.info("  ⏭️  '%s' → not a video, skipped", group)
    return []


# ============================================================
# EXTERNAL M3U PARSER
# ============================================================

_EXTINF_RE = re.compile(r"#EXTINF\s*:\s*(-?\d+)(?:\s+(.+?))?\s*,\s*(.+)", re.DOTALL)
_M3U_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


def parse_m3u_attributes(attr_str: str) -> dict[str, str]:
    return dict(_M3U_ATTR_RE.findall(attr_str or ""))


def looks_like_video_url(url: str) -> bool:
    """Heuristic filter: reject obvious non-video URLs."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url.lower())
    ext = Path(parsed.path).suffix
    if ext in NON_VIDEO_EXTENSIONS:
        return False
    return True


def process_external_m3u(group: str, url: str) -> list[dict]:
    """Fetch and parse an external M3U playlist."""
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content = resp.text
    except requests.RequestException as e:
        log.error("❌ Failed to fetch M3U '%s': %s", url, e)
        return []

    lines = content.splitlines()
    entries: list[dict] = []
    pending_extinf: Optional[str] = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        upper = line.upper()
        if upper.startswith("#EXTM3U"):
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
            m = _EXTINF_RE.match(pending_extinf)
            if m:
                attr_str = m.group(2) or ""
                title = (m.group(3) or "").strip()
                attrs = parse_m3u_attributes(attr_str)
                thumbnail = attrs.get("tvg-logo", "")
                if not title:
                    title = attrs.get("tvg-name", "")
            pending_extinf = None

        if not title:
            path = unquote(urlparse(stream_url).path)
            title = strip_extension(Path(path).name) or "Untitled"

        title = safe_title(title)
        resolution = detect_resolution(title) or detect_resolution(stream_url)

        entries.append(make_entry(
            title=title,
            stream_url=stream_url,
            thumbnail_url=thumbnail,
            group=group,
            resolution=resolution,
            source_type="M3U",
        ))

    log.info("  ✅ '%s' → %d video entrie(s) from external M3U", group, len(entries))
    return entries


# ============================================================
# M3U WRITER
# ============================================================

def build_group_title(entry: dict, combined: bool = False) -> str:
    """Build the group-title string for an entry."""
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
    """Return the two M3U lines for one entry."""
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
    """Write entries to an M3U file. Returns unique entries written."""
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
    log.info("  💾 Wrote %-3d entries → %s", len(seen), filepath.name)
    return len(seen)


def validate_m3u(filepath: Path) -> bool:
    """Basic M3U validation."""
    if not filepath.exists():
        return False
    text = filepath.read_text(encoding="utf-8").strip()
    if not text.startswith("#EXTM3U"):
        log.error("  ⚠️  Invalid M3U (no #EXTM3U): %s", filepath.name)
        return False

    lines = [l for l in text.splitlines() if l.strip()]
    i, count = 1, 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            if i + 1 >= len(lines) or lines[i + 1].startswith("#"):
                log.error("  ⚠️  EXTINF without URL at line %d in %s", i + 1, filepath.name)
                return False
            count += 1
            i += 2
        else:
            i += 1
    return True


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("=" * 60)
    log.info("🎬 M3U Playlist Generator — starting")
    log.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_entries: list[dict] = []
    group_entries: dict[str, list[dict]] = {}

    # ---------------- Pixeldrain folders ----------------
    log.info("")
    log.info("📂 [1/3] Processing Pixeldrain FOLDERS...")
    for group, url in parse_input_file(FOLDER_TXT):
        log.info("→ %s :: %s", group, url)
        try:
            entries = process_pixeldrain_folder(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("❌ Error processing folder '%s': %s", group, e)

    # ---------------- Pixeldrain single files ----------------
    log.info("")
    log.info("📄 [2/3] Processing Pixeldrain SINGLE FILES...")
    for group, url in parse_input_file(FOLDER_S_TXT):
        log.info("→ %s :: %s", group, url)
        try:
            entries = process_pixeldrain_single(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("❌ Error processing single '%s': %s", group, e)

    # ---------------- External M3U playlists ----------------
    log.info("")
    log.info("🌐 [3/3] Processing EXTERNAL M3U playlists...")
    for group, url in parse_input_file(PLAYLIST_TXT):
        log.info("→ %s :: %s", group, url)
        try:
            entries = process_external_m3u(group, url)
            all_entries.extend(entries)
            group_entries.setdefault(group, []).extend(entries)
        except Exception as e:
            log.error("❌ Error processing playlist '%s': %s", group, e)

    # ---------------- Cleanup old files ----------------
    log.info("")
    log.info("🧹 Cleaning old output files...")
    for old in OUTPUT_DIR.glob("*.m3u"):
        old.unlink()

    # ---------------- Write per-group playlists ----------------
    log.info("")
    log.info("📝 Writing per-group playlists...")
    total_written = 0
    for group_name, entries in group_entries.items():
        if not entries:
            continue
        safe_name = sanitize_filename(group_name)
        filepath = OUTPUT_DIR / f"{safe_name}.m3u"
        count = write_m3u(filepath, entries, combined=False)
        validate_m3u(filepath)
        total_written += count

    # ---------------- Combined All.m3u ----------------
    log.info("")
    log.info("📝 Writing combined All.m3u...")
    if all_entries:
        combined_path = OUTPUT_DIR / "All.m3u"
        write_m3u(combined_path, all_entries, combined=True)
        validate_m3u(combined_path)
    else:
        log.warning("⚠️  No entries — All.m3u not created")

    # ---------------- Summary ----------------
    log.info("")
    log.info("=" * 60)
    log.info("✅ DONE — %d group(s), %d total entrie(s)", len(group_entries), len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("⚠️  Interrupted by user")
        sys.exit(130)
    except Exception:
        log.exception("💥 Fatal error")
        sys.exit(1)

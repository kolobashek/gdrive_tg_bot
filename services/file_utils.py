"""
File utilities: caption generation with hashtags, large-file splitting.
"""

import os
import re
import zipfile

from config import config

# ─── Size limits ──────────────────────────────────────────────────────────────

def max_upload_bytes() -> int:
    """Effective Telegram upload limit in bytes (configurable via MAX_UPLOAD_MB)."""
    return config.MAX_UPLOAD_MB * 1024 * 1024


def premium_limit_bytes() -> int:
    return (4 * 1024 if config.TELEGRAM_PREMIUM else 2 * 1024) * 1024 * 1024


# ─── Caption / hashtags ───────────────────────────────────────────────────────

_MIME_TAGS = {
    "image/":                        ("📷", "#фото #photo"),
    "video/":                        ("🎬", "#видео #video"),
    "audio/":                        ("🎵", "#аудио #audio"),
    "application/pdf":               ("📄", "#pdf #документ"),
    "application/vnd.ms-excel":      ("📊", "#таблица #excel"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml": ("📊", "#таблица #xlsx"),
    "application/msword":            ("📝", "#документ #doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml": ("📝", "#документ #docx"),
    "application/zip":               ("🗜", "#архив #zip"),
    "application/x-rar":             ("🗜", "#архив #rar"),
    "application/x-7z-compressed":   ("🗜", "#архив #7z"),
}


def _mime_meta(mime: str) -> tuple[str, str]:
    for prefix, (emoji, tags) in _MIME_TAGS.items():
        if mime.startswith(prefix):
            return emoji, tags
    return "📎", "#файл"


def _folder_tag(name: str) -> str:
    """Convert a folder name to a safe hashtag."""
    tag = re.sub(r"[^\w\u0400-\u04FF]", "_", name).strip("_")
    tag = re.sub(r"_+", "_", tag)
    return f"#{tag}" if tag else ""


def generate_caption(file: dict, folder_name: str = "") -> str:
    """
    Build a rich Telegram caption with hashtags for search.

    Hashtags included:
      - category  (#фото, #видео, #документ …)
      - file ext  (#jpg, #mp4 …)
      - year      (#2026)
      - month     (#april)
      - folder    (#MyAlbum)  ← great for Telegram Premium hashtag search
    """
    name = file.get("name", "file")
    mime = file.get("mimeType", "")
    modified = file.get("modifiedTime", "")  # "2026-04-09T12:34:56.000Z"
    size_bytes = int(file.get("size", 0))

    emoji, category_tags = _mime_meta(mime)

    # Extension tag
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ext_tag = f"#{ext}" if ext and len(ext) <= 5 else ""

    # Date tags
    date_tags = ""
    if len(modified) >= 10:
        year = modified[:4]
        month_num = int(modified[5:7])
        months_ru = ["январь","февраль","март","апрель","май","июнь",
                     "июль","август","сентябрь","октябрь","ноябрь","декабрь"]
        months_en = ["january","february","march","april","may","june",
                     "july","august","september","october","november","december"]
        month_ru = months_ru[month_num - 1]
        month_en = months_en[month_num - 1]
        date_tags = f"#{year} #{month_ru} #{month_en}"

    folder_tag = _folder_tag(folder_name) if folder_name else ""

    # Size display
    if size_bytes >= 1024 ** 3:
        size_str = f"{size_bytes / 1024**3:.1f} GB"
    elif size_bytes >= 1024 ** 2:
        size_str = f"{size_bytes / 1024**2:.1f} MB"
    elif size_bytes > 0:
        size_str = f"{size_bytes // 1024} KB"
    else:
        size_str = ""

    parts = [f"{emoji} <b>{name}</b>"]
    if size_str:
        parts.append(f"📦 {size_str}")

    tags = " ".join(filter(None, [category_tags, ext_tag, date_tags, folder_tag]))
    if tags:
        parts.append(tags)

    return "\n".join(parts)


# ─── File splitting ───────────────────────────────────────────────────────────

def split_file(local_path: str, part_size: int, temp_dir: str) -> list[str]:
    """
    Split a file into binary parts of at most `part_size` bytes.
    Returns list of part file paths (e.g. filename.part1, .part2 …).
    """
    base_name = os.path.basename(local_path)
    parts: list[str] = []
    with open(local_path, "rb") as f:
        part_num = 1
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            part_path = os.path.join(temp_dir, f"{base_name}.part{part_num}")
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            parts.append(part_path)
            part_num += 1
    return parts


def zip_file(local_path: str, temp_dir: str) -> str:
    """Wrap a single file into a zip archive. Returns zip path."""
    base_name = os.path.basename(local_path)
    zip_path = os.path.join(temp_dir, base_name + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.write(local_path, base_name)
    return zip_path


def split_into_zips(local_path: str, part_size: int, temp_dir: str) -> list[str]:
    """
    Split a large file into zip archives, each at most `part_size` bytes.
    Strategy: split raw file first, then zip each part.
    Returns list of zip file paths.
    """
    parts = split_file(local_path, part_size, temp_dir)
    zips: list[str] = []
    try:
        for part_path in parts:
            zips.append(zip_file(part_path, temp_dir))
    finally:
        for p in parts:
            if os.path.exists(p):
                os.remove(p)
    return zips

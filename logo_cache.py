#!/usr/bin/env python3
"""Persistent channel logo mapping for the Rutube playlist releases.

Logos are keyed by a stable Rutube tvg-id, never by channel order or
programme-provider ID. Existing mappings take precedence over upstream changes.
"""
from __future__ import annotations

import json
import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import urlsplit

REGISTRY = Path("docs/logos.json")
ID = re.compile(r'\btvg-id="(rtv\.[0-9a-f]{32})"')
KEY = re.compile(r'^rtv\.[0-9a-f]{32}$')
LOGO = re.compile(r'\s+tvg-logo="([^"]*)"')


def safe_logo_url(value):
    if not isinstance(value, str) or not value or len(value) > 1000:
        return False
    if any(char in value for char in ('"', "\r", "\n", "<", ">")):
        return False
    try:
        parts = urlsplit(unescape(value))
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and not parts.username


def read_registry():
    if not REGISTRY.exists():
        return {}
    entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if not isinstance(entries, dict):
        raise ValueError("Logo registry must be an object")
    if any(not KEY.fullmatch(k) or not safe_logo_url(v)
           for k, v in entries.items()):
        raise ValueError("Invalid logo registry item")
    return entries


def merge_logos(upstream, playlist_paths):
    """Keep previously confirmed mappings, discover only unknown stream IDs."""
    result = read_registry()
    existing_count = len(result)
    for filename in playlist_paths:
        path = Path(filename)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#EXTINF:"):
                continue
            match = ID.search(line)
            logo = LOGO.search(line)
            if match and logo and safe_logo_url(logo.group(1)):
                result.setdefault(match.group(1), unescape(logo.group(1)))
    for channel_id, url in upstream.items():
        if KEY.fullmatch(channel_id) and safe_logo_url(url):
            result.setdefault(channel_id, unescape(url))
    return mirror_logos(result, upstream), existing_count


def add_logos_to_playlist(filename, logos):
    """Add one deterministic logo attribute to every channel with a known URL."""
    path = Path(filename)
    lines = path.read_text(encoding="utf-8").splitlines()
    assigned = 0
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue
        channel = ID.search(line)
        if not channel:
            raise ValueError("Playlist entry lacks a stable channel ID")
        url = logos.get(channel.group(1))
        if not url:
            continue
        head, sep, name = line.partition(",")
        if not sep:
            raise ValueError("Invalid EXTINF entry")
        head = LOGO.sub("", head)
        head += ' tvg-logo="' + escape(url, quote=True) + '"'
        lines[i] = head + "," + name
        assigned += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return assigned


def save_registry(logos):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(
        json.dumps(dict(sorted(logos.items())), ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8"
    )


# Keep the actual image, not only its provider URL. Existing local assets
# are immutable and remain usable during upstream outages.
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from urllib.request import Request, urlopen
from PIL import Image

ASSETS = Path("docs/logos")
MAX_IMAGE_BYTES = 2 * 1024 * 1024
ALLOWED_HOSTS = {"iptvx.one", "epg.iptvx.one", "epg.one", "www.epg.one"}


def local_url(channel_id):
    repo = "rutube-auto" if Path("docs/rutube.m3u").exists() else "iptv-direct"
    return f"https://trufan-hub.github.io/{repo}/logos/{channel_id}.png"


def asset_path(channel_id):
    if not KEY.fullmatch(channel_id):
        raise ValueError("Invalid logo channel ID")
    return ASSETS / (channel_id + ".png")


def valid_asset(path):
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or not (0 < image.width <= 512 and 0 < image.height <= 512):
                return False
            image.verify()
        with Image.open(path) as image:
            image.load()
        return True
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def download_logo(url):
    if not safe_logo_url(url) or urlsplit(url).hostname not in ALLOWED_HOSTS:
        raise ValueError("Unapproved logo source")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
    with urlopen(req, timeout=6) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
            raise ValueError("Unapproved logo redirect")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Logo exceeds size limit")
    with Image.open(BytesIO(data)) as image:
        if not (0 < image.width <= 4096 and 0 < image.height <= 4096):
            raise ValueError("Unexpected image dimensions")
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()


def mirror_logos(registry, upstream):
    ASSETS.mkdir(parents=True, exist_ok=True)

    def mirror(item):
        ident, old = item
        path = asset_path(ident)
        if path.exists():
            if not valid_asset(path):
                raise ValueError("Invalid saved logo: " + ident)
            return ident, local_url(ident), True
        if old == local_url(ident):
            # Never downgrade a previously mirrored logo to an external URL.
            raise ValueError("Previously mirrored logo missing: " + ident)
        for source in dict.fromkeys((old, upstream.get(ident))):
            if not source:
                continue
            try:
                data = download_logo(source)
            except Exception as exc:
                print(f"Logo {ident}: source unavailable ({type(exc).__name__})")
                continue
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            if not valid_asset(temporary):
                temporary.unlink(missing_ok=True)
                continue
            temporary.replace(path)
            return ident, local_url(ident), True
        # Preserve the old mapping on failure; try again on the next update.
        return ident, old, False

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(mirror, sorted(registry.items())))
    print(f"Locally stored logos: {sum(x[2] for x in results)}/{len(results)}")
    return {ident: url for ident, url, _ in results}


def validate_local_registry(registry):
    for ident, url in registry.items():
        path = asset_path(ident)
        if url == local_url(ident) or path.exists():
            if url != local_url(ident) or not valid_asset(path):
                raise ValueError("Local logo missing, corrupt or mismatched: " + ident)

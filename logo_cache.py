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
    if any(not ID.fullmatch(k) or not safe_logo_url(v)
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
        if ID.fullmatch(channel_id) and safe_logo_url(url):
            result.setdefault(channel_id, unescape(url))
    return result, existing_count


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

#!/usr/bin/env python3
"""Fail closed before committing or deploying an IPTV update.

Protects stable M3U URLs, all 169+/160+ catalogue entries, XMLTV channel IDs,
guide freshness/coverage, compressed vs plain guide, and every cached logo.
No network access required; Pillow validates cached images.
"""
from __future__ import annotations
import gzip
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

from logo_cache import read_registry, safe_logo_url, validate_local_registry

ROOT = Path("docs")
PLAYLISTS = (
    ["rutube.m3u"] if (ROOT / "rutube.m3u").exists()
    else ["direct.m3u", "verified.m3u"]
)
HOME = ("rutube-auto" if PLAYLISTS[0] == "rutube.m3u"
        else "iptv-direct")
EPG_URL = "https://trufan-hub.github.io/" + HOME + "/guide.xml"
ID = re.compile(r'\btvg-id="(rtv\.[0-9a-f]{32})"')
LOGO = re.compile(r'\btvg-logo="([^"]+)"')


def require(condition, reason):
    if not condition:
        raise RuntimeError("IPTV release rejected: " + reason)


def parse_playlist(name, logos):
    rows = (ROOT / name).read_text(encoding="utf-8-sig").splitlines()
    require(bool(rows), name + " is empty")
    require(
        rows[0] == '#EXTM3U url-tvg="' + EPG_URL + '"',
        name + " has unexpected EPG URL/header"
    )
    channels = {}
    for row in rows:
        if not row.startswith("#EXTINF:"):
            continue
        matched = ID.search(row)
        require(matched is not None, name + " has channel without stable ID")
        ident = matched.group(1)
        require(ident not in channels, name + " has duplicate ID " + ident)
        logo_match = LOGO.search(row)
        address = unescape(logo_match.group(1)) if logo_match else None
        require(address is None or safe_logo_url(address),
                name + " contains invalid logo")
        known = logos.get(ident)
        if known:
            require(address == known,
                    name + " lost or changed cached logo for " + ident)
        channels[ident] = address
    minimum = 150 if name != "verified.m3u" else 80
    require(len(channels) >= minimum,
            name + " fell below " + str(minimum) + " channels")
    return channels


def main():
    logo_registry = read_registry()
    validate_local_registry(logo_registry)
    require(len(logo_registry) >= 80, "Logo registry contains too few entries")
    playlists = {name: parse_playlist(name, logo_registry) for name in PLAYLISTS}
    primary = playlists[PLAYLISTS[0]]
    if "verified.m3u" in playlists:
        require(set(playlists["verified.m3u"]).issubset(primary),
                "Verified playlist contains unknown channels")

    xml = (ROOT / "guide.xml").read_bytes()
    packed = (ROOT / "guide.xml.gz").read_bytes()
    require(gzip.decompress(packed) == xml,
            "Compressed and plain EPG do not match")
    root = ET.fromstring(xml)
    require(root.tag == "tv", "Invalid XMLTV root")
    xml_ids = [ch.get("id") for ch in root.findall("channel")]
    require(len(xml_ids) == len(set(xml_ids)),
            "Duplicate XMLTV IDs")
    require(set(primary) == set(xml_ids),
            "M3U and XMLTV channel IDs differ")

    icons = {
        c.get("id"): c.find("icon").get("src")
        for c in root.findall("channel")
        if c.find("icon") is not None
    }
    for ident, logo in primary.items():
        if logo:
            require(icons.get(ident) == logo,
                    "M3U/XMLTV logo mismatch for " + ident)

    programmes = root.findall("programme")
    require(len(programmes) >= 100, "XMLTV contains too few programmes")
    active = set()
    for item in programmes:
        ident = item.get("channel")
        require(ident in primary, "Programme refers to unknown channel")
        require(bool(item.get("start") and item.get("stop")
                     and item.findtext("title")), "Incomplete programme")
        active.add(ident)
    require(len(active) >= 50, "EPG has fewer than 50 scheduled channels")
    description_count = sum(
        1 for item in programmes if (item.findtext("desc") or "").strip()
    )
    # A loss of descriptions is a regression, not a valid playlist update.
    require(
        description_count >= max(100, len(programmes) // 5),
        "Descriptions disappeared or sharply decreased from EPG"
    )

    status = json.loads((ROOT / "epg_status.json").read_text(encoding="utf-8"))
    require(status.get("catalog_channels") == len(primary),
            "EPG report has different catalogue size")
    require(status.get("guide_channels") == len(active),
            "EPG report has different programme channel count")
    require(status.get("programmes") == len(programmes),
            "EPG report has different programme count")
    require(status.get("descriptions") == description_count,
            "EPG report does not match description count")
    updated_at = datetime.fromisoformat(status["updated_at"])
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    require(0 <= age < 3 * 3600, "Generated EPG is not current")
    print(json.dumps({
        "result": "PASS", "project": HOME,
        "channels": len(primary), "guide_channels": len(active),
        "programmes": len(programmes),
        "descriptions": description_count,
        "logos_in_registry": len(logo_registry),
        "logos_in_playlist": sum(bool(x) for x in primary.values()),
        "EPG": EPG_URL
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

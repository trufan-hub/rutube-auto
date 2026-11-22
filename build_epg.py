#!/usr/bin/env python3
"""Build a playlist-specific XMLTV guide published by this repository.

Keep titles, synopses, sub-titles and genres when supplied by XMLTV feeds.
No EPG data is fabricated. An invalid or empty guide fails the workflow
before publication, preserving the last successful GitHub Pages deployment.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from logo_cache import merge_logos, add_logos_to_playlist, save_registry

CATALOG = Path("docs/rutube.m3u")
MATCH_REPORT = Path("docs/epg_report.csv")
OUTPUT = Path("docs/guide.xml.gz")
STATUS = Path("docs/epg_status.json")
FEEDS = (
    ("IPTVX", ("https://iptvx.one/EPG",
                "https://iptvx.one/epg/epg.xml.gz")),
    ("EPG.ONE", ("https://epg.one/epg2.xml.gz",
                  "http://epg.one/epg2.xml.gz")),
)
UA = "Mozilla/5.0 (compatible; TruFan-IPTV-Guide/1.0)"
QUALITY = re.compile(r"\s+(?:hd|fhd|uhd|4k|full\s+hd|sd)$", re.I)



def load_catalog():
    """Correlate current playlist entries and EPG matching report by position."""
    with MATCH_REPORT.open(encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))
    if not rows:
        raise ValueError("Missing EPG matching report")
    entries = []
    current_name = None
    for line in CATALOG.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("#EXTINF:"):
            current_name = line.split(",", 1)[-1].strip()
        elif current_name is not None and line.startswith("https://"):
            match = re.search(r"/livestream/([0-9a-f]{32})/", line)
            if not match:
                raise ValueError("Missing Rutube ID in playlist for " + current_name)
            entries.append((current_name, match.group(1)))
            current_name = None
    if len(entries) != len(rows) or len(entries) < 100:
        raise ValueError(
            "Playlist/report length mismatch: "
            + str(len(entries)) + "/" + str(len(rows))
        )
    result = []
    for (name, stream_id), row in zip(entries, rows):
        if name != row["channel"]:
            raise ValueError("Playlist/report channel order mismatch at " + name)
        method = row.get("method", "")
        source = method.split(":", 1)[0] if ":" in method else ""
        result.append({
            "name": name,
            "rutube_id": stream_id,
            "tvg_id": row.get("tvg_id", ""),
            "epg_source": source,
        })
    return result


def normalize(value):
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = text.replace("ё", "е").replace("&", "и")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()
    return QUALITY.sub("", text).strip()


def own_id(channel):
    return "rtv." + channel["rutube_id"]


class PrefixReader:
    """Allow a short magic-header read without buffering a huge XMLTV file."""
    def __init__(self, prefix, source):
        self.prefix = prefix
        self.source = source
        self.bytes_read = 0

    def read(self, size=-1):
        if size < 0:
            size = 65536
        head = self.prefix[:size]
        self.prefix = self.prefix[len(head):]
        body = self.source.read(size - len(head)) if len(head) < size else b""
        self.bytes_read += len(head) + len(body)
        if self.bytes_read > 350_000_000:
            raise ValueError("EPG download exceeds 350 MB")
        return head + body


def parse_url(url, channels, utc_date, provider):
    request = Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/xml,application/gzip,*/*"
    })
    with urlopen(request, timeout=70) as response:
        head = response.read(2)
        stream = PrefixReader(head, response)
        if head == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=stream) as xml:
                return parse_provider(xml, channels, utc_date, provider)
        if head not in (b"<?", b"<t", b"\xef\xbb"):
            raise ValueError("Unexpected XMLTV response header")
        return parse_provider(stream, channels, utc_date, provider)


def parse_provider(stream, channels, utc_date, provider):
    """Read XMLTV incrementally; retain only matched channels and programmes."""
    by_epg = defaultdict(list)
    by_name = defaultdict(list)
    for ch in channels:
        if ch.get("tvg_id"):
            by_epg[ch["tvg_id"]].append(ch)
        by_name[normalize(ch["name"])].append(ch)

    start_day = (utc_date - timedelta(days=1)).strftime("%Y%m%d")
    end_day = (utc_date + timedelta(days=4)).strftime("%Y%m%d")
    selected_source_ids = {}
    found_programmes = defaultdict(list)
    found_icons = {}
    total_channels = 0
    context = ET.iterparse(stream, events=("start", "end"))
    event, root = next(context)
    if event != "start" or root.tag != "tv":
        raise ValueError("Not an XMLTV <tv> root")
    for event, el in context:
        if event != "end":
            continue
        if el.tag == "channel":
            total_channels += 1
            cid = (el.get("id") or "").strip()
            direct = [ch for ch in by_epg.get(cid, [])
                      if ch.get("epg_source") == provider]
            if len(direct) == 1:
                selected_source_ids[cid] = own_id(direct[0])
            elif cid:
                candidates = {}
                for tag in el.findall("display-name"):
                    for ch in by_name.get(normalize(tag.text), []):
                        candidates[own_id(ch)] = ch
                if len(candidates) == 1:
                    selected_source_ids[cid] = next(iter(candidates))
            target = selected_source_ids.get(cid)
            if target:
                for ico in el.findall("icon"):
                    address = (ico.get("src") or "").strip()
                    parsed = urlsplit(address)
                    if (parsed.scheme == "https" and parsed.hostname
                            and not parsed.username and len(address) < 1000):
                        found_icons[target] = address
                        break
            el.clear()
            root.clear()
        elif el.tag == "programme":
            cid = (el.get("channel") or "").strip()
            target = selected_source_ids.get(cid)
            beginning = (el.get("start") or "").strip()
            finish = (el.get("stop") or "").strip()
            if target and start_day <= beginning[:8] <= end_day and finish:
                node = el.find("title")
                name = ("".join(node.itertext()).strip()
                        if node is not None else "")
                if name and len(name) <= 500:
                    # Preserve actual programme metadata; never invent synopses.
                    desc_node = el.find("desc")
                    description = (
                        re.sub(r"\s+", " ", "".join(desc_node.itertext())).strip()[:5000]
                        if desc_node is not None else ""
                    )
                    subtitle_node = el.find("sub-title")
                    subtitle = (
                        re.sub(r"\s+", " ", "".join(subtitle_node.itertext())).strip()[:500]
                        if subtitle_node is not None else ""
                    )
                    categories = tuple(
                        (re.sub(r"\s+", " ", "".join(cat.itertext())).strip()[:150],
                         cat.get("lang") or "")
                        for cat in el.findall("category")[:5]
                        if "".join(cat.itertext()).strip()
                    )
                    found_programmes[target].append((
                        beginning, finish, name, node.get("lang") or "",
                        description, desc_node.get("lang") or "" if desc_node is not None else "",
                        subtitle, subtitle_node.get("lang") or "" if subtitle_node is not None else "",
                        categories
                    ))
            el.clear()
            root.clear()
    return found_programmes, total_channels, found_icons


def main():
    channels = load_catalog()
    now = datetime.now(timezone.utc)
    combined = {}
    logos = {}
    sources = {}
    errors = []
    for label, mirrors in FEEDS:
        for url in mirrors:
            try:
                programmes, count, feed_icons = parse_url(url, channels, now.date(), label)
                if count < 50:
                    raise ValueError("Too few channels in provider XMLTV")
                for cid, image_url in feed_icons.items():
                    logos.setdefault(cid, image_url)
                added = 0
                for cid, entries in programmes.items():
                    if cid not in combined and entries:
                        combined[cid] = entries
                        added += 1
                    elif cid in combined and entries:
                        # Supplement missing descriptions from another feed only
                        # when BOTH airtime and title agree; never mismatch shows.
                        by_airing = defaultdict(list)
                        for candidate in entries:
                            by_airing[(candidate[0][:14], normalize(candidate[2]))].append(candidate)
                        for index, existing in enumerate(combined[cid]):
                            if existing[4]:
                                continue
                            matches = by_airing.get(
                                (existing[0][:14], normalize(existing[2])), []
                            )
                            if len(matches) == 1 and matches[0][4]:
                                supplement = matches[0]
                                combined[cid][index] = (
                                    existing[:4] + supplement[4:6]
                                    + (existing[6] or supplement[6],
                                       existing[7] or supplement[7],
                                       existing[8] or supplement[8])
                                )
                sources[label] = {
                    "url": url, "source_channels": count,
                    "matched_channels": len(programmes),
                    "new_channels": added
                }
                print(label + ": " + json.dumps(sources[label]))
                break
            except (OSError, ValueError, ET.ParseError, EOFError,
                    HTTPError, URLError) as exc:
                error = f"{label} {url}: {type(exc).__name__}: {exc}"
                errors.append(error)
                print("WARNING: " + error)
    total_events = sum(len(p) for p in combined.values())
    # Prevent a transient upstream outage from publishing a sharply reduced EPG.
    if STATUS.exists():
        previous = json.loads(STATUS.read_text(encoding="utf-8"))
        previous_channels = int(previous.get("guide_channels", 0))
        if previous_channels >= 30 and len(combined) * 100 < previous_channels * 80:
            raise RuntimeError(
                "Guide coverage regression: "
                + str(previous_channels) + " -> " + str(len(combined))
            )

    provider_logo_count = len(logos)
    logos, cached_logo_count = merge_logos(logos, ["docs/rutube.m3u"])
    if len(combined) < 20 or total_events < 100:
        raise RuntimeError(
            f"Refusing to publish insufficient guide: "
            f"{len(combined)} channels / {total_events} programmes. "
            + " | ".join(errors)
        )

    tv = ET.Element("tv", {
        "generator-info-name": "TruFan Rutube Auto",
        "generator-info-url": "https://github.com/trufan-hub/rutube-auto"
    })
    for channel in channels:
        ident = own_id(channel)
        el = ET.SubElement(tv, "channel", {"id": ident})
        name = ET.SubElement(el, "display-name", {"lang": "ru"})
        name.text = channel["name"]
        if ident in logos:
            ET.SubElement(el, "icon", {"src": logos[ident]})
    for cid, entries in combined.items():
        seen = set()
        for (start, stop, title, lang, description, desc_lang,
             subtitle, subtitle_lang, categories) in sorted(
                entries, key=lambda row: (row[0], row[1], row[2])):
            key = (start, stop, title)
            if key in seen:
                continue
            seen.add(key)
            el = ET.SubElement(tv, "programme", {
                "start": start, "stop": stop, "channel": cid
            })
            title_el = ET.SubElement(
                el, "title", {"lang": lang or "ru"}
            )
            title_el.text = title
            if subtitle:
                ET.SubElement(
                    el, "sub-title", {"lang": subtitle_lang or lang or "ru"}
                ).text = subtitle
            if description:
                ET.SubElement(
                    el, "desc", {"lang": desc_lang or lang or "ru"}
                ).text = description
            for category, cat_lang in categories:
                ET.SubElement(
                    el, "category", {"lang": cat_lang or lang or "ru"}
                ).text = category
    document = ET.tostring(tv, encoding="utf-8", xml_declaration=True)
    # Validate the exact bytes which will be served as XMLTV.
    parsed = ET.fromstring(document)
    if parsed.tag != "tv" or len(parsed.findall("programme")) < 100:
        raise RuntimeError("Generated XMLTV failed validation")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Publish both XMLTV transports: plain XML maximizes TV-player compatibility.
    Path("docs/guide.xml").write_bytes(document)
    OUTPUT.write_bytes(gzip.compress(document, compresslevel=8, mtime=0))
    # Persistent IDs prevent provider or EPG changes from removing old logos.
    logo_counts = {}
    for filename in ["docs/rutube.m3u"]:
        logo_counts[filename] = add_logos_to_playlist(filename, logos)
    save_registry(logos)
    report = {
        "updated_at": now.isoformat(),
        "catalog_channels": len(channels),
        "guide_channels": len(combined),
        "programmes": len(parsed.findall("programme")),
        "descriptions": len(parsed.findall("programme/desc")),
        "subtitles": len(parsed.findall("programme/sub-title")),
        "genres": len(parsed.findall("programme/category")),
        "provider_logo_count": provider_logo_count,
        "cached_logo_count": cached_logo_count,
        "logo_registry_count": len(logos),
        "added_to_m3u": logo_counts["docs/rutube.m3u"],
        "compressed_bytes": OUTPUT.stat().st_size,
        "uncompressed_bytes": Path("docs/guide.xml").stat().st_size,
        "feeds": sources,
        "warnings": errors,
    }
    STATUS.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

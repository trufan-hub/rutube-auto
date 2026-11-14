#!/usr/bin/env python3
"""Build a Rutube-only M3U from Dimonovich, matching XMLTV IDs when possible.

Standard-library only. Can be run locally or every 6h in GitHub Actions.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

SOURCE = 'https://raw.githubusercontent.com/Dimonovich/TV/Dimonovich/FREE/TV'
EPG_URL = 'https://epg.one/ru.xml.gz'
EPG_ALTERNATES = (
    'https://epg.one/ru.xml.gz',
    'http://epg.one/ru.xml.gz',
    'https://epg.one/epg2.xml.gz',
)
UA = 'Mozilla/5.0 (compatible; RUTUBE-M3U-Updater/1.0)'
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
TRAILING_QUALITY = re.compile(r'\s+(?:hd|fhd|full\s*hd|uhd|4k|sd)\s*$', re.I)

# Only explicit, well-known branding variants; we do not guess EPG IDs.
BRAND_ALIASES = {
    'тв3': 'тв 3', 'тв 3': 'тв 3',
    'музтв': 'муз тв', 'муз тв': 'муз тв',
    'москва24': 'москва 24', 'москва 24': 'москва 24',
    'пятница': 'пятница',
    'тнт4': 'тнт 4', 'тнт 4': 'тнт 4',
    'ру тв': 'ru tv', 'rutv': 'ru tv', 'ru tv': 'ru tv',
    'мир24': 'мир 24', 'мир 24': 'мир 24',
}


def get_bytes(url: str, retries: int = 3, max_size: int = 150_000_000) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
            with urlopen(req, timeout=65) as response:
                data = response.read(max_size + 1)
                if len(data) > max_size:
                    raise ValueError('Remote file exceeds size limit')
                if not data:
                    raise ValueError('Empty response')
                return data
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'Unable to fetch {url}: {last}')


def parse_entries(text: str) -> list[dict]:
    entries = []
    header = None
    options = []
    for raw in text.replace('\ufeff', '').splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            header = line
            options = []
        elif not line:
            continue
        elif line.startswith('#'):
            if header is not None:
                options.append(line)
        elif header is not None:
            meta, comma, title = header.partition(',')
            if comma:
                attrs = dict(ATTR.findall(meta))
                entries.append({'name': title.strip(), 'attrs': attrs,
                                'options': options[:], 'url': line})
            header, options = None, []
    return entries


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    value = re.sub(r'\s*\((?:hd|fhd|sd|uhd|4k)\)\s*$', '', value)
    while TRAILING_QUALITY.search(value):
        value = TRAILING_QUALITY.sub('', value)
    value = value.replace('&', ' и ')
    value = re.sub(r'[^\w\s+]', ' ', value, flags=re.UNICODE)
    value = re.sub(r'\s+', ' ', value).strip()
    return BRAND_ALIASES.get(value, value)


def make_index(epg_gz: bytes) -> tuple[dict[str, set[str]], dict[str, str], set[str]]:
    # Read channel metadata only. Preserve IDs exactly as provider publishes them.
    index = defaultdict(set)
    display = {}
    valid = set()
    stream = gzip.GzipFile(fileobj=io.BytesIO(epg_gz)) if epg_gz[:2] == b'\x1f\x8b' else io.BytesIO(epg_gz)
    with stream:
        context = ET.iterparse(stream, events=('start', 'end'))
        root = None
        for event, el in context:
            if root is None and event == 'start':
                root = el
            if event == 'end' and el.tag == 'channel':
                cid = el.get('id')
                if cid:
                    valid.add(cid)
                    for d in el.findall('display-name'):
                        label = (d.text or '').strip()
                        key = normalize(label)
                        if key:
                            index[key].add(cid)
                            display.setdefault(cid, label)
                    key = normalize(cid)
                    if key:
                        index[key].add(cid)
                el.clear()
                if root is not None:
                    root.clear()
            elif event == 'end' and el.tag == 'programme':
                # Channel list is usually before program listings; avoid retaining programme data.
                el.clear()
                if root is not None:
                    root.clear()
    return index, display, valid


def epg_match(name: str, original: dict, index: dict[str, set[str]], valid: set[str], cached: dict[str, str]):
    upstream_id = original.get('tvg-id', '').strip()
    if upstream_id and upstream_id in valid:
        return upstream_id, 'original_id'
    queries = [name, original.get('tvg-name', '')]
    for q in queries:
        k = normalize(q)
        ids = index.get(k, set()) if k else set()
        if len(ids) == 1:
            return next(iter(ids)), 'name_exact'
    # If EPG service is down, re-use only IDs verified in earlier successful runs.
    if not valid and normalize(name) in cached:
        return cached[normalize(name)], 'cached_verified_id'
    # Do not arbitrarily select between ambiguous channels or different time zones.
    return None, 'no_verified_match'


def quote_attr(s: str) -> str:
    return s.replace('&', '&amp;').replace('"', '&quot;').replace('\r', '').replace('\n', ' ')


def generate(source: str, index: dict, valid: set, epg_url: str, cached: dict | None = None):
    cached = cached or {}
    parsed = parse_entries(source)
    chosen = [e for e in parsed if 'rutube' in e['attrs'].get('group-title', '').casefold()]
    if not chosen:
        raise ValueError('No group containing Rutube found in upstream source; refusing to overwrite working playlist')
    out = [f'#EXTM3U url-tvg="{quote_attr(epg_url)}" x-tvg-url="{quote_attr(epg_url)}"',
           f'# Generated (UTC): {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}; source: Dimonovich/TV',
           '# Note: channel EPG only works if the EPG provider actually has its schedule.']
    report = []
    seen = defaultdict(int)
    count_matched = 0
    for e in chosen:
        name = e['name']
        if not name:
            continue
        seen[normalize(name)] += 1
        ids, status = epg_match(name, e['attrs'], index, valid, cached)
        if ids:
            count_matched += 1
        # Every channel gets a deterministic ID, including custom thematic channels.
        # For channels absent from XMLTV it is a *local* ID, not a fake guide association.
        fallback = 'rutube-' + hashlib.sha1(
            (normalize(name) + f'#{seen[normalize(name)]}').encode('utf-8')
        ).hexdigest()[:12]
        effective_id = ids or fallback
        logo = e['attrs'].get('tvg-logo', '')
        fields = [f'tvg-id="{quote_attr(effective_id)}"',
                  f'tvg-name="{quote_attr(name)}"',
                  f'group-title="Rutube"']
        if logo:
            fields.append(f'tvg-logo="{quote_attr(logo)}"')
        out.append('#EXTINF:-1 ' + ' '.join(fields) + ',' + name)
        out.extend(e['options'])
        out.append(e['url'])
        report.append({'channel': name, 'tvg_id': effective_id,
                       'epg_matched': 'yes' if ids else 'no', 'method': status})
    return '\n'.join(out) + '\n', report, len(chosen), count_matched


def main():
    ap = argparse.ArgumentParser(description='Refresh Rutube M3U from Dimonovich and map real EPG.ONE IDs')
    ap.add_argument('--source-file', help='Local copy of upstream M3U (for offline testing)')
    ap.add_argument('--epg-file', help='Local gzipped XMLTV (for offline testing)')
    ap.add_argument('--output', default='docs/rutube.m3u')
    ap.add_argument('--report', default='docs/epg_report.csv')
    args = ap.parse_args()
    source = (Path(args.source_file).read_text(encoding='utf-8-sig') if args.source_file else
              get_bytes(SOURCE, max_size=12_000_000).decode('utf-8-sig', errors='replace'))
    index, valid = {}, set()
    epg_problem = None
    if args.epg_file:
        index, _, valid = make_index(Path(args.epg_file).read_bytes())
    else:
        for url in EPG_ALTERNATES:
            try:
                index, _, valid = make_index(get_bytes(url))
                if valid:
                    break
            except (OSError, RuntimeError, ET.ParseError, ValueError) as exc:
                epg_problem = str(exc)
                print(f'EPG source failed: {url}: {exc}', file=sys.stderr)
    cached = {}
    if not valid:
        print('WARNING: EPG currently unavailable; retaining previously verified IDs (if any)', file=sys.stderr)
        old_report = Path(args.report)
        if old_report.exists():
            with old_report.open('r', encoding='utf-8-sig', newline='') as handle:
                for row in csv.DictReader(handle):
                    if row.get('epg_matched') == 'yes' and row.get('tvg_id'):
                        cached[normalize(row['channel'])] = row['tvg_id']
    m3u, report, total, matched = generate(source, index, valid, EPG_URL, cached)
    outfile = Path(args.output)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(m3u, encoding='utf-8')
    csvfile = Path(args.report)
    csvfile.parent.mkdir(parents=True, exist_ok=True)
    with csvfile.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, ['channel', 'tvg_id', 'epg_matched', 'method'])
        writer.writeheader()
        writer.writerows(report)
    summary = {'source': SOURCE, 'channels': total, 'matched_epg': matched,
               'no_epg': total - matched, 'generated_utc': datetime.now(timezone.utc).isoformat()}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()


#!/usr/bin/env python3
"""Build a Rutube-only M3U from Dimonovich, matching XMLTV IDs when possible.

Standard-library only. Can be run locally or every 6h in GitHub Actions.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

SOURCE = 'https://raw.githubusercontent.com/Dimonovich/TV/Dimonovich/FREE/TV'

# Two EPG sources confirmed working by the owner in Lazy IPTV.
# Include BOTH URLs in the generated M3U to make TV-side EPG discovery automatic.
EPG_URLS = (
    'https://iptvx.one/EPG',
    'http://epg.one/epg2.xml.gz',
)
EPG_URL = ','.join(EPG_URLS)
# Alternate transport/endpoint URLs are used for fetching only.
EPG_FEEDS = (
    ('IPTVX', ('https://iptvx.one/EPG',
               'https://iptvx.one/epg/epg.xml.gz')),
    ('EPG.ONE', ('http://epg.one/epg2.xml.gz',
                 'https://epg.one/epg2.xml.gz')),
)

UA = 'Mozilla/5.0 (compatible; RUTUBE-M3U-Updater/1.0)'
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
TRAILING_QUALITY = re.compile(
    r'\s+(?:hd|fhd|full\s*hd|uhd|4k|sd)\s*$', re.I
)

BRAND_ALIASES = {
    'тв3': 'тв 3',
    'тв 3': 'тв 3',
    'музтв': 'муз тв',
    'муз тв': 'муз тв',
    'москва24': 'москва 24',
    'москва 24': 'москва 24',
    'пятница': 'пятница',
    'тнт4': 'тнт 4',
    'тнт 4': 'тнт 4',
    'ру тв': 'ru tv',
    'rutv': 'ru tv',
    'ru tv': 'ru tv',
    'мир24': 'мир 24',
    'мир 24': 'мир 24',
}

# Provider-specific IDs: applied only when present
# in XMLTV and associated with current programmes.
PREFERRED_IPTVX_IDS = {
    'первый канал': 'pervy',
    'нтв': 'ntv',
    'тнт': 'tnt',
    'тнт 4': 'tnt4',
    'пятница': 'piatnica',
    'суббота': 'super',
}

# EPG.ONE uses numeric channel IDs, unlike IPTVX.
# All preferred IDs must pass verification against programmes in that exact feed.
PREFERRED_EPG_ONE_IDS = {
    'тнт': '353',
    'тнт 4': '1649',
    'первый канал': '146',
    'нтв': '162',
    'пятница': '1003',
}



def get_bytes(
    url: str,
    retries: int = 3,
    max_size: int = 150_000_000
) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = Request(
                url,
                headers={
                    'User-Agent': UA,
                    'Accept': '*/*'
                }
            )
            with urlopen(req, timeout=65) as response:
                data = response.read(max_size + 1)
                if len(data) > max_size:
                    raise ValueError('Remote file exceeds size limit')
                if not data:
                    raise ValueError('Empty response')
                return data
        except (
            HTTPError, URLError, TimeoutError,
            ValueError, OSError
        ) as exc:
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
                entries.append({
                    'name': title.strip(),
                    'attrs': attrs,
                    'options': options[:],
                    'url': line
                })

            header, options = None, []

    return entries


def normalize(value: str) -> str:
    value = unicodedata.normalize(
        'NFKC', value
    ).casefold().replace('ё', 'е')

    value = re.sub(
        r'\s*\((?:hd|fhd|sd|uhd|4k)\)\s*$',
        '',
        value
    )

    while TRAILING_QUALITY.search(value):
        value = TRAILING_QUALITY.sub('', value)

    value = value.replace('&', ' и ')
    value = re.sub(
        r'[^\w\s+]', ' ', value, flags=re.UNICODE
    )
    value = re.sub(r'\s+', ' ', value).strip()

    return BRAND_ALIASES.get(value, value)


def make_index(
    epg_gz: bytes
) -> tuple[dict[str, set[str]], dict[str, str], set[str]]:
    """Index only channels with programmes on current days."""

    index = defaultdict(set)
    display = {}
    channel_names = {}
    with_programmes = set()

    today = datetime.now(timezone.utc).date()

    first_day = (
        today - timedelta(days=1)
    ).strftime('%Y%m%d')

    last_day = (
        today + timedelta(days=3)
    ).strftime('%Y%m%d')

    if epg_gz[:2] == b'\x1f\x8b':
        stream = gzip.GzipFile(fileobj=io.BytesIO(epg_gz))
    else:
        stream = io.BytesIO(epg_gz)

    with stream:
        context = ET.iterparse(
            stream, events=('start', 'end')
        )
        root = None

        for event, el in context:
            if root is None and event == 'start':
                root = el

            if event != 'end':
                continue

            if el.tag == 'channel':
                cid = (el.get('id') or '').strip()

                if cid:
                    names = set()

                    for d in el.findall('display-name'):
                        label = (d.text or '').strip()
                        key = normalize(label)

                        if key:
                            names.add(key)
                            display.setdefault(cid, label)

                    names.add(normalize(cid))
                    channel_names[cid] = names

                el.clear()

                if root is not None:
                    root.clear()

            elif el.tag == 'programme':
                cid = (
                    el.get('channel') or ''
                ).strip()

                start = el.get('start', '')[:8]

                if cid and first_day <= start <= last_day:
                    with_programmes.add(cid)

                el.clear()

                if root is not None:
                    root.clear()

    for cid in with_programmes:
        for label in channel_names.get(cid, ()):
            if label:
                index[label].add(cid)

    return index, display, with_programmes


def epg_match(
    name: str,
    original: dict,
    guides: list,
    cached: dict[str, str]
):
    """Match to an ID with current programmes in one of the advertised EPGs.

    IDs are NOT interchangeable between providers: each candidate is validated
    against the exact source's <programme channel="..."> values.
    """
    key = normalize(name)
    for provider, index, scheduled in guides:
        upstream_id = original.get('tvg-id', '').strip()
        if upstream_id and upstream_id in scheduled:
            return upstream_id, provider + ':original_id'
        preferences = (
            PREFERRED_IPTVX_IDS if provider == 'IPTVX'
            else PREFERRED_EPG_ONE_IDS
        )
        preferred = preferences.get(key, '')
        if preferred and preferred in scheduled:
            return preferred, provider + ':verified_id'
        for query in [name, original.get('tvg-name', '')]:
            matches = index.get(normalize(query), set())
            if len(matches) == 1:
                return next(iter(matches)), provider + ':exact_name'
    # When a feed is temporarily unreachable, keep an already verified ID.
    prior = cached.get(key, '')
    if prior:
        return prior, 'cached_previous_match'
    return None, 'no_current_programme'


def quote_attr(s: str) -> str:
    return (
        s.replace('&', '&amp;')
         .replace('"', '&quot;')
         .replace('\r', '')
         .replace('\n', ' ')
    )


def generate(
    source: str,
    guides: list,
    epg_url: str,
    cached: dict | None = None
):
    cached = cached or {}
    parsed = parse_entries(source)

    chosen = [
        e for e in parsed
        if 'rutube' in e['attrs'].get(
            'group-title', ''
        ).casefold()
    ]

    if not chosen:
        raise ValueError(
            'No Rutube group found; refusing to overwrite playlist'
        )

    out = [
        f'#EXTM3U url-tvg="{quote_attr(epg_url)}"',
        '# Generated (UTC): '
        + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
        + '; source: Dimonovich/TV',
        '# EPG requires a matching programme in the provider feed.'
    ]

    report = []
    seen = defaultdict(int)
    count_matched = 0

    for e in chosen:
        name = e['name']

        if not name:
            continue

        seen[normalize(name)] += 1

        ids, status = epg_match(
            name, e['attrs'], guides, cached
        )

        if ids:
            count_matched += 1

        effective_id = ids or ''
        logo = e['attrs'].get('tvg-logo', '')

        fields = [
            f'tvg-id="{quote_attr(effective_id)}"',
            f'tvg-name="{quote_attr(name)}"',
            'group-title="Rutube"'
        ]

        if logo:
            fields.append(
                f'tvg-logo="{quote_attr(logo)}"'
            )

        out.append(
            '#EXTINF:-1 '
            + ' '.join(fields)
            + ',' + name
        )

        out.extend(e['options'])
        out.append(e['url'])

        report.append({
            'channel': name,
            'tvg_id': effective_id,
            'epg_matched': 'yes' if ids else 'no',
            'method': status
        })

    return (
        '\n'.join(out) + '\n',
        report,
        len(chosen),
        count_matched
    )


def main():
    ap = argparse.ArgumentParser(
        description='Refresh Rutube M3U with IPTVX EPG'
    )

    ap.add_argument('--source-file')
    ap.add_argument('--epg-file')
    ap.add_argument(
        '--output', default='docs/rutube.m3u'
    )
    ap.add_argument(
        '--report', default='docs/epg_report.csv'
    )

    args = ap.parse_args()

    if args.source_file:
        source = Path(
            args.source_file
        ).read_text(encoding='utf-8-sig')
    else:
        source = get_bytes(
            SOURCE, max_size=12_000_000
        ).decode('utf-8-sig', errors='replace')

    guides = []
    errors = []
    for provider, urls in EPG_FEEDS:
        for url in urls:
            try:
                payload = (
                    Path(args.epg_file).read_bytes()
                    if args.epg_file and provider == 'IPTVX'
                    else get_bytes(url)
                )
                index, _, scheduled = make_index(payload)
                if len(scheduled) < 50:
                    raise ValueError(
                        f'Only {len(scheduled)} channels with current programmes'
                    )
                guides.append((provider, index, scheduled))
                print(f'{provider} EPG OK: {len(scheduled)} scheduled IDs via {url}')
                break
            except (OSError, RuntimeError, ET.ParseError, ValueError) as exc:
                errors.append(f'{provider} {url}: {exc}')
                print(f'EPG warning: {provider} {url}: {exc}', file=sys.stderr)
            if args.epg_file and provider == 'IPTVX':
                break

    # Stop on total EPG failure, avoiding a bad playlist replacing the last
    # published version. If one provider is down, the other still works.
    if not guides:
        raise RuntimeError('All EPG sources unavailable: ' + ' | '.join(errors))

    cached = {}
    old_report = Path(args.report)
    if old_report.exists():
        with old_report.open('r', encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                if row.get('epg_matched') == 'yes' and row.get('tvg_id'):
                    cached[normalize(row['channel'])] = row['tvg_id']

    m3u, report, total, matched = generate(
        source, guides, EPG_URL, cached
    )

    outfile = Path(args.output)
    outfile.parent.mkdir(
        parents=True, exist_ok=True
    )
    outfile.write_text(m3u, encoding='utf-8')

    csvfile = Path(args.report)
    csvfile.parent.mkdir(
        parents=True, exist_ok=True
    )

    with csvfile.open(
        'w', encoding='utf-8-sig', newline=''
    ) as handle:
        writer = csv.DictWriter(
            handle,
            ['channel', 'tvg_id', 'epg_matched', 'method']
        )
        writer.writeheader()
        writer.writerows(report)

    summary = {
        'source': SOURCE,
        'channels': total,
        'matched_epg': matched,
        'no_epg': total - matched,
        'epg_urls': list(EPG_URLS),
        'generated_utc': datetime.now(
            timezone.utc
        ).isoformat()
    }

    print(json.dumps(
        summary, ensure_ascii=False, indent=2
    ))


if __name__ == '__main__':
    main()

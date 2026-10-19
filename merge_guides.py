#!/usr/bin/env python3
"""Build one TV guide (guide.xml.gz) for the playlists in this repo.

GitHub Actions runs this every 6 hours (see .github/workflows/update-guide.yml).
You never need to run it yourself.

What it does:
  1. Reads every tvg-id in the .m3u playlists and looks it up in guide-map.csv,
     which says which source guide carries that channel.
  2. Downloads each source guide, keeps only those channels, and only listings
     from 6 hours ago to 3 days ahead.
  3. Writes out/guide.xml.gz and out/report.md.
  4. Rewrites the first line of each playlist so TiViMate finds the guide link.
     If a source failed to download, its own link is added there too, so those
     channels still get a guide straight from the source.

Only Python's standard library is used.
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import gzip
import io
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SOURCES = {
    "US2": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "CA2": "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "IN1": "https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz",
    "IN4": "https://epgshare01.online/epgshare01/epg_ripper_IN4.xml.gz",
    "pluto_us": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PlutoTV/us.xml.gz",
    "pluto_ca": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PlutoTV/ca.xml.gz",
    "samsung_us": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/SamsungTVPlus/us.xml.gz",
    "samsung_ca": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/SamsungTVPlus/ca.xml.gz",
    "samsung_in": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/SamsungTVPlus/in.xml.gz",
    "plex_us": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/Plex/us.xml.gz",
    "plex_ca": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/Plex/ca.xml.gz",
    "roku": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/Roku/all.xml.gz",
    "pbs": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PBS/all.xml.gz",
    "metv": "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/MeTV/epg.xml.gz",
    "tubi": "https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/main/tubi_epg.xml",
    "xumo": "https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_epg.xml.gz",
    "lg": "https://raw.githubusercontent.com/mchardage-sys/EPG-LG-Channels/main/lg_epg_us.xml",
    "epgtalk_locals": "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_local_guide.xml.gz",
    "epgtalk_uslite": "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_lite.xml.gz",
    "in_jiotv": "https://raw.githubusercontent.com/jasonramg/iptv-epg/main/epg/jiotv.xml.gz",
    "in_dishtv": "https://raw.githubusercontent.com/jasonramg/iptv-epg/main/epg/dishtv.xml.gz",
    "in_yupptv": "https://raw.githubusercontent.com/jasonramg/iptv-epg/main/epg/yupptv.xml.gz",
    "in_airtel": "https://raw.githubusercontent.com/jasonramg/iptv-epg/main/epg/airtel.xml.gz",
}

KEEP_PAST = dt.timedelta(hours=6)
KEEP_AHEAD = dt.timedelta(days=3)
UA = "Mozilla/5.0 (guide merger; +https://github.com)"


def log(msg):
    print(msg, flush=True)


def parse_time(s):
    m = re.match(r"^(\d{14})\s*([+-]\d{4})?", s or "")
    if not m:
        return None
    t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    off = m.group(2) or "+0000"
    sign = -1 if off[0] == "-" else 1
    tz = dt.timezone(sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
    return t.replace(tzinfo=tz).astimezone(dt.timezone.utc)


def download(url, tries=3):
    """Fetch a guide; epgshare01 suggests plain http:// when https:// is refused."""
    urls = [url] + ([url.replace("https://", "http://", 1)] if "epgshare01" in url else [])
    last = None
    for i in range(tries * len(urls)):
        u = urls[i % len(urls)]
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            if b"<tv" not in data[:4096]:
                raise ValueError("not an XMLTV file")
            return data
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            time.sleep(5)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def read_source(data, wanted, now):
    """Return {channel_id: (channel_xml, [programme_xml, ...])} for wanted ids."""
    chans, progs = {}, collections.defaultdict(list)
    lo, hi = now - KEEP_PAST, now + KEEP_AHEAD
    it = ET.iterparse(io.BytesIO(data), events=("start", "end"))
    _, root = next(it)
    for ev, el in it:
        if ev != "end":
            continue
        if el.tag == "channel":
            cid = el.get("id")
            if cid in wanted and cid not in chans:
                chans[cid] = ET.tostring(el, encoding="unicode").strip()
            root.clear()
        elif el.tag == "programme":
            cid = el.get("channel")
            if cid in wanted:
                start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
                if start and stop and stop > lo and start < hi:
                    progs[cid].append((start, ET.tostring(el, encoding="unicode").strip()))
            root.clear()
    return {cid: (chans.get(cid), progs[cid]) for cid in progs}


def playlist_ids(path):
    ids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#EXTINF"):
                m = re.search(r'tvg-id="([^"]*)"', line)
                if m and m.group(1):
                    ids.append(m.group(1))
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name, e.g. from $GITHUB_REPOSITORY")
    ap.add_argument("--branch", default="guide", help="branch the guide is published to")
    ap.add_argument("--out", default="out")
    ap.add_argument("--map", default="guide-map.csv")
    ap.add_argument("--playlists", default=".")
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    os.makedirs(a.out, exist_ok=True)
    guide_url = f"https://raw.githubusercontent.com/{a.repo}/{a.branch}/guide.xml.gz"

    gmap = {}
    with open(a.map, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gmap[row["tvg_id"]] = [s for s in row["sources"].split("|") if s in SOURCES]

    playlists = sorted(glob.glob(os.path.join(a.playlists, "*.m3u")))
    pl_ids = {p: playlist_ids(p) for p in playlists}
    all_ids = {i for ids in pl_ids.values() for i in ids if i in gmap}

    wanted = collections.defaultdict(set)
    for tid in all_ids:
        for s in gmap[tid]:
            wanted[s].add(tid)

    got, status = {}, {}
    for key in sorted(wanted, key=list(SOURCES).index):
        t0 = time.time()
        try:
            data = download(SOURCES[key])
            got[key] = read_source(data, wanted[key], now)
            status[key] = f"ok ({len(got[key])} channels, {time.time() - t0:.0f}s)"
        except Exception as exc:  # noqa: BLE001
            status[key] = f"FAILED: {exc}"
        log(f"{key:15} {status[key]}")

    # pick each channel from the first source in its list that has listings
    chosen = {}
    for tid in sorted(all_ids):
        for s in gmap[tid]:
            if s in got and tid in got[s] and got[s][tid][1]:
                chosen[tid] = s
                break

    with gzip.open(os.path.join(a.out, "guide.xml.gz"), "wt", encoding="utf-8", compresslevel=9) as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="merge_guides.py">\n')
        for tid, s in chosen.items():
            ch = got[s][tid][0]
            if ch:
                f.write(ch + "\n")
            else:
                f.write(f"<channel id={quoteattr(tid)}><display-name>{escape(tid)}</display-name></channel>\n")
        for tid, s in chosen.items():
            for _, p in sorted(got[s][tid][1], key=lambda x: x[0]):
                f.write(p + "\n")
        f.write("</tv>\n")

    # playlist headers: merged guide first, then any failed source those channels need
    failed = [k for k in SOURCES if k in wanted and k not in got]
    rows = []
    for p in playlists:
        ids = pl_ids[p]
        mapped = [i for i in ids if i in gmap]
        need_fallback = [k for k in failed if any(k in gmap[i] and i not in chosen for i in mapped)]
        urls = [guide_url] + [SOURCES[k] for k in need_fallback]
        head = f'#EXTM3U url-tvg="{",".join(urls)}" x-tvg-url="{",".join(urls)}"'
        with open(p, encoding="utf-8") as f:
            lines = f.read().split("\n")
        if lines and lines[0].startswith("#EXTM3U"):
            lines[0] = head
        else:
            lines.insert(0, head)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        have = sum(1 for i in ids if i in chosen)
        rows.append((os.path.basename(p), len(ids), have, len(ids) - have, ", ".join(need_fallback) or "-"))

    size = os.path.getsize(os.path.join(a.out, "guide.xml.gz"))
    rep = [f"# Guide build {now:%Y-%m-%d %H:%M} UTC", "",
           f"Guide link: {guide_url}  ({size / 1e6:.1f} MB, {len(chosen)} channels)", "",
           "| Playlist | Channels | With guide | No guide | Extra links needed |", "|---|---|---|---|---|"]
    rep += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |" for r in rows]
    rep += ["", "| Source | Result |", "|---|---|"] + [f"| {k} | {v} |" for k, v in status.items()]
    text = "\n".join(rep) + "\n"
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    log(text)
    if not chosen:
        sys.exit("no guide data at all - not publishing")


if __name__ == "__main__":
    main()

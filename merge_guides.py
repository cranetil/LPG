#!/usr/bin/env python3
"""Build one TV guide (guide.xml.gz) for the playlists in this repo.

GitHub Actions runs this every 6 hours (see .github/workflows/update-guide.yml).
You never need to run it yourself.

Two commands:

  grab-list   Writes channel lists for iptv-org's guide grabber (one file per
              site): every playlist channel that has no guide in guide-map.csv
              but that iptv-org knows how to fetch from a working TV-listings site.

  build       1. Channels in guide-map.csv: taken from their source guide.
              2. Channels the iptv-org grabber fetched (same IDs as the playlists).
              3. Links you add to extra-guides.txt: channels that still have no
                 guide are matched by ID, then by exact channel name. Every match
                 is listed in the run summary. Put a tvg-id in skip-matches.txt to
                 stop a wrong match.
              4. With --placeholders, anything still blank gets 2-hour blocks
                 titled with the channel's own name (not a real schedule).
              Listings are kept from 6 hours ago to 3 days ahead. Writes
              out/guide.xml.gz and out/report.md, and rewrites the first line of
              each playlist so TiViMate finds the guide link.

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
import unicodedata
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

# iptv-org grabber: sites never to use (their config crashed the grabber on GitHub)
SITE_EXCLUDE = {"tv.mail.ru"}
# iptv-org grabber: which site to use when a channel is listed on several working ones
SITE_PREFERENCE = ["tvpassport.com", "tvguide.com", "i.mjh.nz", "plex.tv", "pluto.tv", "xumo.tv", "distro.tv",
                   "watch.whaletvplus.com", "dishtv.in", "airtelxstream.in", "tataplay.com", "watchyour.tv",
                   "gatotv.com", "mi.tv", "tvhebdo.com"]

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


def fmt_time(t):
    return t.strftime("%Y%m%d%H%M%S +0000")


def unpack(data):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if b"<tv" not in data[:4096]:
        raise ValueError("not an XMLTV file")
    return data


def download(url, tries=3):
    """Fetch a guide; epgshare01 suggests plain http:// when https:// is refused."""
    urls = [url] + ([url.replace("https://", "http://", 1)] if "epgshare01" in url else [])
    last = None
    for i in range(tries * len(urls)):
        u = urls[i % len(urls)]
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                return unpack(r.read())
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            time.sleep(5)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def iter_elements(data):
    it = ET.iterparse(io.BytesIO(data), events=("start", "end"))
    _, root = next(it)
    for ev, el in it:
        if ev == "end" and el.tag in ("channel", "programme"):
            yield el
            root.clear()


def channel_names(data):
    """{guide channel id: [display names]}"""
    out = {}
    for el in iter_elements(data):
        if el.tag == "channel":
            out[el.get("id")] = [(d.text or "").strip() for d in el.findall("display-name") if (d.text or "").strip()]
    return out


def read_source(data, wanted, now):
    """wanted = {guide channel id: [playlist ids]} ->
    {playlist id: (channel_xml or None, [(start, programme_xml), ...])}, re-keyed to the playlist ids."""
    chans, progs = {}, collections.defaultdict(list)
    lo, hi = now - KEEP_PAST, now + KEEP_AHEAD
    for el in iter_elements(data):
        if el.tag == "channel":
            for target in wanted.get(el.get("id"), []):
                if target not in chans:
                    el.set("id", target)
                    chans[target] = ET.tostring(el, encoding="unicode").strip()
        else:
            targets = wanted.get(el.get("channel"))
            if targets:
                start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
                if start and stop and stop > lo and start < hi:
                    for target in targets:
                        el.set("channel", target)
                        progs[target].append((start, ET.tostring(el, encoding="unicode").strip()))
    return {t: (chans.get(t), progs[t]) for t in progs}


def playlist_channels(path):
    """[(tvg-id, channel name, logo)] in playlist order."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#EXTINF"):
                m = re.search(r'tvg-id="([^"]*)"', line)
                if m and m.group(1):
                    name = re.sub(r'^#EXTINF:[^,"]*(?:"[^"]*"[^,"]*)*,', "", line.strip())
                    name = re.sub(r"\s*\(\d{3,4}[pi]\)|\s*\[[^\]]*\]", "", name).strip()
                    logo = re.search(r'tvg-logo="([^"]*)"', line)
                    out.append((m.group(1), name, logo.group(1) if logo else ""))
    return out


def norm_name(s):
    s = re.sub(r"^\s*[A-Z]{2,3}\s*[-:|]\s+", "", s)            # "US - Name", "CA | Name"
    s = re.sub(r"\.(us|ca|in|uk)\s*$", "", s, flags=re.I)      # "Name.us"
    s = re.sub(r"\(\d+\)", " ", s)                               # "Name (840)" channel numbers
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()
    s = re.sub(r"\((pluto tv|samsung tv plus|plex|tubi|roku|xumo|united states|usa|us|canada|india)\)", " ", s)
    s = s.replace("&", " and ").replace("+", " plus ").replace("'", "").replace("’", "")
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", s).split() if w not in {"hd", "sd", "fhd", "uhd"}]
    return "".join(words)


COUNTRY_CODES = {"us", "ca", "in", "uk", "gb", "au", "mx", "es", "fr", "de", "pk", "bd", "lk", "ae", "br", "ar"}
ALLOWED = {"us": {"us"}, "ca": {"ca", "us"}, "in": {"in"}}


def guide_country(gid, names):
    """Country a guide channel is marked for ("US - X", "X.us", id "X.us"), or None."""
    for text in [gid] + names[:1]:
        m = re.match(r"^\s*([A-Za-z]{2})\s*[-:|]\s+", text) or re.search(r"\.([a-z]{2})\s*$", text, re.I)
        if m and m.group(1).lower() in COUNTRY_CODES:
            return m.group(1).lower()
    return None


def playlist_country(path):
    n = os.path.basename(path).lower()
    if "-" in n:  # combined playlist
        return None
    return "in" if "india" in n else "ca" if "canada" in n else "us" if ("usa" in n or n.startswith("us")) else None


def read_lines(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.split("#", 1)[0].strip() for l in f if l.split("#", 1)[0].strip()]


def files_in(path, pattern):
    if not path or not os.path.exists(path):
        return []
    return sorted(glob.glob(os.path.join(path, pattern))) if os.path.isdir(path) else [path]


def load_map(path):
    gmap = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gmap[row["tvg_id"]] = [s for s in row["sources"].split("|") if s in SOURCES]
    return gmap


# ---------------------------------------------------------------- grab-list
def cmd_grab_list(a):
    gmap = load_map(a.map)
    ids = {cid for p in sorted(glob.glob(os.path.join(a.playlists, "*.m3u"))) for cid, _, _ in playlist_channels(p)}
    blank = {i for i in ids if i not in gmap}

    working = set()
    sites_md = os.path.join(a.epg, "SITES.md")
    if os.path.exists(sites_md):
        for m in re.finditer(r'<a href="sites/([^"]+)">[^<]+</a></td><td[^>]*>\d+</td><td[^>]*>([^<]*)</td>',
                             open(sites_md, encoding="utf-8").read()):
            if "\U0001F7E2" in m.group(2):  # green circle = site works
                working.add(m.group(1))

    offers = collections.defaultdict(dict)  # tvg-id -> {site: channel element}
    for path in sorted(glob.glob(os.path.join(a.epg, "sites", "*", "*.channels.xml"))):
        site = path.split(os.sep)[-2]
        if (working and site not in working) or site in SITE_EXCLUDE:
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for c in root.iter("channel"):
            x = c.get("xmltv_id")
            if x in blank and site not in offers[x] and c.get("site_id"):
                offers[x][site] = c

    # one list per site, so one broken site can't stop the others
    rank = {s: i for i, s in enumerate(SITE_PREFERENCE)}
    by_site = collections.defaultdict(list)
    for tid in sorted(offers):
        site = min(offers[tid], key=lambda s: (rank.get(s, len(rank)), s))
        by_site[site].append("  " + ET.tostring(offers[tid][site], encoding="unicode").strip())
    os.makedirs(a.out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(a.out_dir, "*.channels.xml")):
        os.remove(old)
    for site, lines in by_site.items():
        with open(os.path.join(a.out_dir, f"{site}.channels.xml"), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + "\n".join(lines) + "\n</channels>\n")
    log(f"{len(blank)} channels without a guide; iptv-org can fetch {len(offers)} of them")
    log(", ".join(f"{s}: {len(v)}" for s, v in sorted(by_site.items(), key=lambda x: -len(x[1]))))


# ---------------------------------------------------------------- build
def cmd_build(a):
    now = dt.datetime.now(dt.timezone.utc)
    os.makedirs(a.out, exist_ok=True)
    guide_url = f"https://raw.githubusercontent.com/{a.repo}/{a.branch}/guide.xml.gz"
    gmap = load_map(a.map)

    playlists = sorted(glob.glob(os.path.join(a.playlists, "*.m3u")))
    pl = {p: playlist_channels(p) for p in playlists}
    info, country = {}, collections.defaultdict(set)
    for p, chans in pl.items():
        for cid, name, logo in chans:
            info.setdefault(cid, (name, logo))
            country[cid].add(playlist_country(p))
    all_ids = set(info)

    chosen = {}   # playlist id -> (label, channel_xml, [(start, xml)])
    status = collections.OrderedDict()

    # 1. mapped sources
    wanted = collections.defaultdict(lambda: collections.defaultdict(list))
    for tid in all_ids:
        for s in gmap.get(tid, []):
            wanted[s][tid].append(tid)
    got = {}
    for key in [k for k in SOURCES if k in wanted]:
        t0 = time.time()
        try:
            got[key] = read_source(download(SOURCES[key]), wanted[key], now)
            status[key] = f"ok ({len(got[key])} channels, {time.time() - t0:.0f}s)"
        except Exception as exc:  # noqa: BLE001
            status[key] = f"FAILED: {exc}"
        log(f"{key:15} {status[key]}")
    for tid in sorted(all_ids):
        for s in gmap.get(tid, []):
            if s in got and tid in got[s] and got[s][tid][1]:
                chosen[tid] = (s, *got[s][tid])
                break
    # channels whose own source failed this run: leave them out of the merged guide and
    # point the playlist at that source's link instead (see the header step below)
    failed = [k for k in SOURCES if k in wanted and k not in got]
    pending = {t for t in all_ids if t not in chosen and any(k in failed for k in gmap.get(t, []))}

    # 2. iptv-org grabber output (already uses the playlist IDs); one file per site
    grab_sites, site_of, asked = collections.Counter(), {}, collections.Counter()
    for lst in files_in(a.grab_list, "*.channels.xml"):
        try:
            for c in ET.parse(lst).getroot().iter("channel"):
                site_of[c.get("xmltv_id")] = c.get("site")
                asked[c.get("site")] += 1
        except ET.ParseError:
            pass
    grabbed = files_in(a.grabbed, "*.xml")
    for path in grabbed:
        try:
            data = unpack(open(path, "rb").read())
        except Exception:  # noqa: BLE001 - empty or broken output from a failed site
            continue
        todo = {t: [t] for t in all_ids if t not in chosen and t not in pending}
        for tid, (ch, progs) in read_source(data, todo, now).items():
            if progs:
                chosen[tid] = (f"iptv-org: {site_of.get(tid, '?')}", ch, progs)
                grab_sites[site_of.get(tid, "?")] += 1
    if a.grabbed:
        status["iptv-org grabber"] = (f"ok ({sum(grab_sites.values())} channels with listings)" if grabbed
                                      else "FAILED: no output")
    for site, n in asked.most_common():
        status[f"  grab: {site}"] = f"{grab_sites.get(site, 0)} of {n} channels"

    # 3. extra guides added by hand
    skip = set(read_lines(a.skip))
    extra_matches = []
    for url in read_lines(a.extra):
        todo = [t for t in sorted(all_ids) if t not in chosen and t not in skip and t not in pending]
        if not todo:
            break
        try:
            data = download(url)
        except Exception as exc:  # noqa: BLE001
            status[url] = f"FAILED: {exc}"
            continue
        names = channel_names(data)
        by_key = collections.defaultdict(set)
        for gid, ns in names.items():
            for n in ns:
                k = norm_name(n)
                if len(k) >= 3:
                    by_key[k].add(gid)
        wanted_x, how = collections.defaultdict(list), {}
        for tid in todo:
            if tid in names:
                wanted_x[tid].append(tid)
                how[tid] = (tid, "same ID")
                continue
            cands = by_key.get(norm_name(info[tid][0]), set())
            if len(cands) == 1:
                gid = next(iter(cands))
                gc = guide_country(gid, names.get(gid, []))
                if gc and None not in country[tid] and not any(gc in ALLOWED[c] for c in country[tid]):
                    continue  # e.g. an India channel must not take a US guide
                wanted_x[gid].append(tid)
                how[tid] = (gid, "same name")
        res = read_source(data, wanted_x, now)
        n = 0
        for tid, (ch, progs) in res.items():
            if progs and tid not in chosen:
                chosen[tid] = (f"extra: {url}", ch, progs)
                gid, why = how[tid]
                gname = (names.get(gid) or [gid])[0]
                extra_matches.append((info[tid][0], tid, gname, why, url))
                n += 1
        status[url] = f"ok ({n} new channels)"
        log(f"extra guide {url}: {n} new channels")

    # 4. placeholders
    placeholders = 0
    if a.placeholders:
        start = (now - KEEP_PAST).replace(minute=0, second=0, microsecond=0)
        start -= dt.timedelta(hours=start.hour % 2)
        for tid in sorted(all_ids):
            if tid in chosen or tid in pending:
                continue
            name, logo = info[tid]
            ch = f"<channel id={quoteattr(tid)}><display-name>{escape(name)}</display-name>"
            ch += (f"<icon src={quoteattr(logo)} />" if logo else "") + "</channel>"
            progs, t = [], start
            while t < now + KEEP_AHEAD:
                u = t + dt.timedelta(hours=2)
                progs.append((t, f'<programme start="{fmt_time(t)}" stop="{fmt_time(u)}" channel={quoteattr(tid)}>'
                                 f"<title>{escape(name)}</title>"
                                 "<desc>No schedule is published for this channel.</desc></programme>"))
                t = u
            chosen[tid] = ("placeholder", ch, progs)
            placeholders += 1

    # write the guide
    with gzip.open(os.path.join(a.out, "guide.xml.gz"), "wt", encoding="utf-8", compresslevel=9) as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="merge_guides.py">\n')
        for tid in sorted(chosen):
            ch = chosen[tid][1] or (f"<channel id={quoteattr(tid)}>"
                                    f"<display-name>{escape(info[tid][0])}</display-name></channel>")
            f.write(ch + "\n")
        for tid in sorted(chosen):
            for _, p in sorted(chosen[tid][2], key=lambda x: x[0]):
                f.write(p + "\n")
        f.write("</tv>\n")

    # playlist headers: merged guide first, then any failed source those channels still need
    rows = []
    for p in playlists:
        ids = [cid for cid, _, _ in pl[p]]
        need = [k for k in failed if any(i in pending and k in gmap.get(i, []) for i in ids)]
        urls = [guide_url] + [SOURCES[k] for k in need]
        head = f'#EXTM3U url-tvg="{",".join(urls)}" x-tvg-url="{",".join(urls)}"'
        with open(p, encoding="utf-8") as f:
            lines = f.read().split("\n")
        if lines and lines[0].startswith("#EXTM3U"):
            lines[0] = head
        else:
            lines.insert(0, head)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        kinds = collections.Counter(
            "placeholder" if chosen[i][0] == "placeholder" else "real" for i in ids if i in chosen)
        rows.append((os.path.basename(p), len(ids), kinds["real"], kinds["placeholder"],
                     len(ids) - kinds["real"] - kinds["placeholder"], ", ".join(need) or "-"))

    size = os.path.getsize(os.path.join(a.out, "guide.xml.gz"))
    real = sum(1 for v in chosen.values() if v[0] != "placeholder")
    rep = [f"# Guide build {now:%Y-%m-%d %H:%M} UTC", "",
           f"Guide link: {guide_url}  ({size / 1e6:.1f} MB, {real} channels with real listings"
           + (f", {placeholders} with placeholders" if a.placeholders else "") + ")", "",
           "| Playlist | Channels | Real guide | Placeholder | No guide | Extra links needed |",
           "|---|---|---|---|---|---|"]
    rep += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |" for r in rows]
    rep += ["", "| Source | Result |", "|---|---|"] + [f"| {k} | {v} |" for k, v in status.items()]
    if extra_matches:
        rep += ["", "## Matches from extra-guides.txt",
                "Check these. To undo one, add its tvg-id to skip-matches.txt.", "",
                "| Channel | tvg-id | Matched to | How | Guide |", "|---|---|---|---|---|"]
        rep += [f"| {m[0]} | {m[1]} | {m[2]} | {m[3]} | {m[4]} |" for m in extra_matches]
    text = "\n".join(rep) + "\n"
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    log(text)
    if not real:
        sys.exit("no guide data at all - not publishing")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grab-list", help="channel list for iptv-org's grabber")
    g.add_argument("--epg", required=True, help="folder with a copy of github.com/iptv-org/epg")
    g.add_argument("--out-dir", required=True, help="folder for one <site>.channels.xml per site")
    g.add_argument("--map", default="guide-map.csv")
    g.add_argument("--playlists", default=".")
    b = sub.add_parser("build", help="build guide.xml.gz")
    b.add_argument("--repo", required=True, help="owner/name, e.g. from $GITHUB_REPOSITORY")
    b.add_argument("--branch", default="guide", help="branch the guide is published to")
    b.add_argument("--out", default="out")
    b.add_argument("--map", default="guide-map.csv")
    b.add_argument("--playlists", default=".")
    b.add_argument("--grabbed", help="folder (or file) with the guides iptv-org's grabber wrote")
    b.add_argument("--grab-list", help="folder (or file) with the channel lists given to the grabber")
    b.add_argument("--extra", default="extra-guides.txt")
    b.add_argument("--skip", default="skip-matches.txt")
    b.add_argument("--placeholders", action="store_true")
    a = ap.parse_args()
    cmd_grab_list(a) if a.cmd == "grab-list" else cmd_build(a)


if __name__ == "__main__":
    main()

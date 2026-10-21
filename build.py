#!/usr/bin/env python3
"""Build per-station departure-pattern tables ("patroontijden") from the OVapi GTFS feed.

Usage:
    python3 build.py [--gtfs gtfs-nl.zip] [--week-start YYYY-MM-DD] [--out data]

The reference period is one or more whole weeks (Sunday..Saturday) starting at --start.
Default start is the start of the new timetable (the second Sunday of December) when the
feed covers it, otherwise the first whole week in the feed; default length is every whole week the feed covers (max 8). For each weekday
only departures that run on the majority of those dates are kept, so engineering works and
one-off extras drop out.
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import pickle
import re
import statistics
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict

GTFS_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
USER_AGENT = "patroontijden/0.1 (personal timetable pattern tool)"

DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]  # index = date.weekday()
LOCAL_TYPES = {"Sprinter", "Stoptrein"}
AGENCY_SHORT = {
    "NS International": "NSI",
    "Blauwnet Arriva": "Arriva",
    "Blauwnet Keolis": "Keolis",
    "RRReis Arriva": "Arriva",
    "RRReis Keolis": "Keolis",
    "R-net NS": "NS",
    "R-net Qbuzz": "Qbuzz",
}
MAJOR_MIN_KM = 7.0  # a "via" station must be at least this far away to name a direction


def second_sunday_of_december(year):
    d = dt.date(year, 12, 1)
    first_sunday = d + dt.timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + dt.timedelta(days=7)


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def read_csv(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def hms_to_min(s):
    h, m, _ = s.split(":")
    return int(h) * 60 + int(m)


def hour_from(minutes):
    """Start of the hour a departure falls in: 9.43 -> '9:00'."""
    return f"{minutes // 60 % 24}:00"


def hour_until(minutes):
    """End of the hour a departure falls in: 20.43 -> '21:00'."""
    h = minutes // 60 + 1
    return "24:00" if h == 24 else f"{h % 24}:00"


def fmt_time(minutes):
    minutes %= 1440
    return f"{minutes // 60}.{minutes % 60:02d}"


def serie_of(trip_short_name, agency):
    if not trip_short_name.isdigit():
        return trip_short_name
    n = int(trip_short_name)
    if n < 1000 and agency not in ("NS", "Arriva", "Keolis", "Qbuzz"):
        return n // 10 * 10  # international: ICE 120, Nightjet 420, ...
    return n // 100 * 100


# ---------------------------------------------------------------------------
# Phase 1: extract rail trips for the reference period (cached)
# ---------------------------------------------------------------------------

def feed_info(gtfs_path):
    with zipfile.ZipFile(gtfs_path) as zf:
        return next(read_csv(zf, "feed_info.txt"))


def extract(gtfs_path, dates, cache_path):
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) > os.path.getmtime(gtfs_path):
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    week_keys = {d.strftime("%Y%m%d"): d for d in dates}
    zf = zipfile.ZipFile(gtfs_path)
    feed = next(read_csv(zf, "feed_info.txt"))

    agencies = {a["agency_id"]: a["agency_name"] for a in read_csv(zf, "agency.txt")}
    routes = {}
    for r in read_csv(zf, "routes.txt"):
        if r["route_type"] == "2" or r["route_type"].startswith("1") and len(r["route_type"]) == 3:
            name = agencies.get(r["agency_id"], r["agency_id"])
            routes[r["route_id"]] = AGENCY_SHORT.get(name, name)

    service_days = defaultdict(list)
    for c in read_csv(zf, "calendar_dates.txt"):
        if c["exception_type"] == "1" and c["date"] in week_keys:
            service_days[c["service_id"]].append(week_keys[c["date"]])

    trips = {}
    for t in read_csv(zf, "trips.txt"):
        if t["route_id"] in routes and t["service_id"] in service_days and "bus" not in t["trip_long_name"].lower():
            trips[t["trip_id"]] = {
                "num": t["trip_short_name"],
                "headsign": t["trip_headsign"],
                "type": t["trip_long_name"],
                "agency": routes[t["route_id"]],
                "dates": sorted(service_days[t["service_id"]]),
                "stops": [],
            }
    print(f"{len(trips)} rail trips in reference period", file=sys.stderr)

    with zf.open("stop_times.txt") as f:
        lines = io.TextIOWrapper(f, encoding="utf-8-sig")
        header = next(lines).strip().split(",")
        i_trip, i_seq, i_stop = header.index("trip_id"), header.index("stop_sequence"), header.index("stop_id")
        i_arr, i_dep = header.index("arrival_time"), header.index("departure_time")
        i_pick, i_drop = header.index("pickup_type"), header.index("drop_off_type")
        used_stops = set()
        for line in lines:
            trip_id = line[: line.index(",")]
            trip = trips.get(trip_id)
            if trip is None:
                continue
            p = line.rstrip("\n").split(",")
            trip["stops"].append((int(p[i_seq]), p[i_stop], hms_to_min(p[i_arr]), hms_to_min(p[i_dep]),
                                  p[i_pick] or "0", p[i_drop] or "0"))
            used_stops.add(p[i_stop])
    for trip in trips.values():
        trip["stops"].sort()

    stops = {}
    for s in read_csv(zf, "stops.txt"):
        if s["stop_id"] in used_stops:
            code = s["zone_id"][4:] if s["zone_id"].startswith("IFF:") else s["stop_id"]
            stops[s["stop_id"]] = {
                "station": code,
                "name": s["stop_name"],
                "platform": s["platform_code"],
                "pos": (float(s["stop_lat"]), float(s["stop_lon"])),
            }

    data = {"trips": trips, "stops": stops, "dates": dates, "feed": feed}
    with open(cache_path, "wb") as f:
        pickle.dump(data, f)
    return data


# ---------------------------------------------------------------------------
# Phase 2: patterns
# ---------------------------------------------------------------------------

def day_ranges(days, night=False):
    """[0,1,2,3,4] -> 'ma-vr'; [0,2,4] -> 'ma + wo + vr'; all -> 'dagelijks'."""
    days = sorted(set(days))
    if len(days) == 7:
        return "dagelijks" if not night else "elke nacht"
    runs, cur = [], [days[0]]
    for d in days[1:]:
        if d == cur[-1] + 1:
            cur.append(d)
        else:
            runs.append(cur)
            cur = [d]
    runs.append(cur)
    # wrap zo -> ma (e.g. vr-ma for night services)
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == 6:
        runs[0] = runs.pop() + runs[0]
    parts = []
    for r in runs:
        if len(r) == 1:
            parts.append(DAYS[r[0]])
        elif len(r) == 2 and r != [5, 6]:
            parts.append(f"{DAYS[r[0]]} + {DAYS[r[1]]}")
        else:
            parts.append(f"{DAYS[r[0]]}-{DAYS[r[-1]]}")
    if night:
        if len(parts) == 2 and "-" not in parts[0] + parts[1]:
            return f"{parts[0]}- en {parts[1]}-nacht"
        return " + ".join(parts) + "-nacht"
    return " + ".join(parts)


def headway_of(times_by_day):
    diffs = []
    for ts in times_by_day.values():
        ts = sorted(set(ts))
        diffs += [b - a for a, b in zip(ts, ts[1:])]
    if not diffs:
        return 60
    return max(30, statistics.median(diffs))


def describe(own_by_day, union_by_day, ref_by_day, night):
    """Turn {weekday: [minutes]} into a Dutch validity remark ('' when it runs all day, every day).

    own_by_day: departures of this destination line; union_by_day: all departures of the row
    (used so a single train with another destination does not split a span).
    """
    hw = headway_of(union_by_day)
    tol = hw * 1.5
    desc = {}
    for d, ts in own_by_day.items():
        ts = sorted(set(ts), key=night_sort if night else None)
        diffs = {b - a for a, b in zip(ts, ts[1:])}
        if not night and len(ts) <= 6 and (len(ts) <= 2 or min(diffs) >= 110):
            # a handful of departures that never run in consecutive hours: just list them
            desc[d] = "om " + ", ".join(fmt_time(t) for t in ts)
            continue
        if night:
            desc[d] = (night_sort(ts[0]), night_sort(ts[-1]), len(ts) == 1)
            continue
        union = sorted(set(union_by_day[d]))
        spans, cur = [], [ts[0]]
        for t in ts[1:]:
            gap = t - cur[-1]
            others = sum(1 for u in union if cur[-1] < u < t)
            if gap > tol and not (others == 1 and gap <= hw * 2.5):
                spans.append(cur)
                cur = [t]
            else:
                cur.append(t)
        spans.append(cur)
        # a lone early or late extra train is not part of the hourly pattern
        if any(len(sp) > 1 for sp in spans):
            if len(spans[0]) == 1:
                spans.pop(0)
            if len(spans[-1]) == 1:
                spans.pop()
        first, last = ref_by_day[d]
        if len(spans) > 3:
            desc[d] = f"{hour_from(ts[0])}-{hour_until(ts[-1])} ({len(ts)}x)"
            continue
        parts = []
        for sp in spans:
            at_start = sp[0] - first <= tol
            at_end = last - sp[-1] <= tol
            if len(sp) == 1:
                parts.append(f"om {fmt_time(sp[0])}")
            elif at_start and at_end:
                parts.append("")
            elif at_start:
                parts.append(f"tot {hour_until(sp[-1])}")
            elif at_end:
                parts.append(f"na {hour_from(sp[0])}")
            else:
                parts.append(f"{hour_from(sp[0])}-{hour_until(sp[-1])}")
        if len(parts) > 1 and all(p.startswith("om ") for p in parts):
            parts = ["om " + ", ".join(p[3:] for p in parts)]
        desc[d] = " & ".join(p for p in parts if p)

    if night:
        # nights whose first/last trains differ by a few minutes are the same pattern
        clusters = []
        for d, (lo, hi, single) in sorted(desc.items()):
            for c in clusters:
                if c["lo"] // 60 == lo // 60 and c["hi"] // 60 == hi // 60 and c["single"] == single:
                    c["days"].append(d)
                    c["lo"], c["hi"] = min(c["lo"], lo), max(c["hi"], hi)
                    break
            else:
                clusters.append({"lo": lo, "hi": hi, "single": single, "days": [d]})
        desc = {}
        for c in clusters:
            text = f"om {fmt_time(c['lo'])}" if c["single"] else f"{hour_from(c['lo'])}-{hour_until(c['hi'])}"
            for d in c["days"]:
                desc[d] = text

    groups = defaultdict(list)
    for d, text in desc.items():
        groups[text].append(d)
    if not night and len(groups) == 1 and "" in groups and len(desc) == 7:
        remark = ""
    else:
        parts = []
        for text, ds in sorted(groups.items(), key=lambda kv: min(kv[1])):
            label = day_ranges(ds, night)
            parts.append(text if label == "dagelijks" and text else f"{label} {text}".strip())
        remark = "; ".join(parts)
    if hw >= 110 and not night and not remark.startswith(("om ", "1x")) and " om " not in remark \
            and statistics.median(len(v) for v in own_by_day.values()) > 2:
        remark = "1x/2 uur" + ("; " + remark if remark else "")
    return remark


def plat_join(platforms):
    def key(p):
        m = re.match(r"(\d+)(.*)", p)
        return (int(m.group(1)), m.group(2)) if m else (999, p)

    counts = Counter(p for p in platforms if p)
    total = sum(counts.values())
    ps = {p for p, c in counts.items() if c / total >= 0.1} or set(counts)
    # "4" next to "4a"/"4b" adds nothing
    ps = {p for p in ps if not (p.isdigit() and any(q != p and re.match(rf"{p}[a-z]$", q) for q in ps))}
    out, prev_num = [], None
    for p in sorted(ps, key=key):
        m = re.match(r"(\d+)([a-z]+)$", p)
        if m and m.group(1) == prev_num:
            out.append(m.group(2))
        else:
            out.append(p)
            prev_num = m.group(1) if m else None
    return "/".join(out)


def build_station_index(data):
    trips, stops = data["trips"], data["stops"]
    station_pos = defaultdict(list)
    station_name = {}
    long_distance_calls = Counter()
    for trip in trips.values():
        for _, sid, *_ in trip["stops"]:
            st = stops[sid]["station"]
            station_name.setdefault(st, stops[sid]["name"])
            if trip["type"] not in LOCAL_TYPES:
                long_distance_calls[st] += len(trip["dates"])
    for s in stops.values():
        station_pos[s["station"]].append(s["pos"])
    pos = {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for k, v in station_pos.items()}
    weeks = len(data["dates"]) / 7
    major = {st for st, n in long_distance_calls.items() if n / weeks >= 50}
    return station_name, pos, major


class Network:
    """Graph of consecutive calls of local trains, used to find stations a fast train passes through."""

    def __init__(self, data, pos):
        self.pos = pos
        self.adj = defaultdict(set)
        stops = data["stops"]
        for trip in data["trips"].values():
            if trip["type"] not in LOCAL_TYPES:
                continue
            seq = [stops[s[1]]["station"] for s in trip["stops"]]
            for a, b in zip(seq, seq[1:]):
                if a != b:
                    self.adj[a].add(b)
                    self.adj[b].add(a)
        self.cache = {}

    def dist(self, a, b):
        return haversine_km(self.pos[a], self.pos[b])

    def between(self, a, b):
        """Stations passed between consecutive calls a and b (empty when unknown or adjacent)."""
        key = (a, b)
        if key in self.cache:
            return self.cache[key]
        result = []
        if b not in self.adj[a] and a in self.adj and b in self.adj:
            direct = self.dist(a, b)
            limit = direct * 1.2 + 2
            best = {a: 0.0}
            prev = {}
            heap = [(0.0, a)]
            import heapq
            while heap:
                d, u = heapq.heappop(heap)
                if u == b:
                    break
                if d > best.get(u, 1e9) or d > limit:
                    continue
                for v in self.adj[u]:
                    nd = d + self.dist(u, v)
                    # prune: must stay roughly on the way to b
                    if nd + self.dist(v, b) > limit:
                        continue
                    if nd < best.get(v, 1e9):
                        best[v] = nd
                        prev[v] = u
                        heapq.heappush(heap, (nd, v))
            if b in prev:
                node = prev[b]
                while node != a:
                    result.append(node)
                    node = prev[node]
                result.reverse()
        self.cache[key] = result
        return result


def build(data, out_dir):
    trips, stops = data["trips"], data["stops"]
    station_name, pos, major = build_station_index(data)
    net = Network(data, pos)

    # how many dates of each weekday are in the period
    per_weekday = Counter(d.weekday() for d in data["dates"])

    # station -> (train number, departure, destination) -> occurrences
    agg = defaultdict(lambda: defaultdict(list))
    for trip in trips.values():
        calls = [(stops[s[1]]["station"], s) for s in trip["stops"]]
        origin_dep = calls[0][1][3] % 1440
        # full path including passed-through stations
        path = []
        for i, (st, _) in enumerate(calls):
            if i:
                path += net.between(calls[i - 1][0], st)
            path.append(st)
        call_idx_in_path = []
        j = 0
        for st, _ in calls:
            while path[j] != st:
                j += 1
            call_idx_in_path.append(j)
        for i, (st, (_, sid, arr, dep, pick, _)) in enumerate(calls[:-1]):
            if pick == "1":
                continue
            downstream = []
            for st2 in path[call_idx_in_path[i] + 1:]:
                if st2 != st and st2 not in downstream:
                    downstream.append(st2)
            via = [s for s in downstream if s in major and haversine_km(pos[st], pos[s]) >= MAJOR_MIN_KM][:3]
            if not via:
                via = [downstream[-1]]
            m = dep % 1440
            night = 60 <= m < 300 or 60 <= origin_dep < 300
            dest = trip["headsign"] or station_name[calls[-1][0]]
            agg[st][(trip["num"], dep, dest)].append({
                "trip": trip, "dep": dep, "platform": stops[sid]["platform"], "night": night,
                "via": tuple(via), "next": calls[i + 1][0], "dest": dest,
                "path": tuple(downstream[:12]),
            })

    # keep, per weekday, only departures that run on the majority of that weekday's dates
    deps = defaultdict(list)
    for st, by_key in agg.items():
        for occurrences in by_key.values():
            n = Counter(d.weekday() for o in occurrences for d in o["trip"]["dates"])
            days = sorted(wd for wd, c in n.items() if c * 2 > per_weekday[wd])
            if not days:
                continue
            weight = lambda o: len(o["trip"]["dates"])
            rep_ = max(occurrences, key=weight)
            platforms = Counter()
            for o in occurrences:
                platforms[o["platform"]] += weight(o)
            deps[st].append(dict(rep_, trip=dict(rep_["trip"], days=days),
                                 platform=platforms.most_common(1)[0][0]))

    os.makedirs(os.path.join(out_dir, "stations"), exist_ok=True)
    index = []
    for st, ds in deps.items():
        rows = patterns_for_station(ds)
        names = {v for r in rows for v in r["via"] + r["path"]} | {r["next"] for r in rows}
        index.append({"code": st, "name": station_name[st], "rows": len(rows), "deps": len(ds)})
        with open(os.path.join(out_dir, "stations", f"{st}.json"), "w", encoding="utf-8") as f:
            json.dump({"code": st, "name": station_name[st], "names": {k: station_name[k] for k in names},
                       "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
    index.sort(key=lambda s: s["name"])
    dates = data["dates"]
    meta = {
        "feed_version": data["feed"]["feed_version"],
        "feed_start": data["feed"]["feed_start_date"],
        "feed_end": data["feed"]["feed_end_date"],
        "week_start": dates[0].isoformat(),
        "week_end": dates[-1].isoformat(),
        "weeks": len(dates) // 7,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "stations": index,
    }
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(index)} stations to {out_dir}", file=sys.stderr)


def combine_coupled(ds):
    """Coupled train parts (same time, same next call, same platform) become one departure 'A / B'."""
    def base(p):
        m = re.match(r"\d+", p or "")
        return m.group(0) if m else p

    buckets = defaultdict(list)
    for d in ds:
        buckets[(d["dep"], d["trip"]["type"], d["next"], base(d["platform"]))].append(d)
    out = []
    for group in buckets.values():
        if len(group) == 1 or len({d["dest"] for d in group}) == 1:
            out += group
            continue
        group.sort(key=lambda d: int(d["trip"]["num"]) if d["trip"]["num"].isdigit() else 0)
        days = [set(d["trip"]["days"]) for d in group]
        common = set.intersection(*days)
        if not common:
            out += group
            continue
        first = group[0]
        combined = dict(first, trip=dict(first["trip"], days=sorted(common)),
                        dest=" / ".join(sorted({d["dest"] for d in group})))
        out.append(combined)
        for d, dd in zip(group, days):
            rest = dd - common
            if rest:
                out.append(dict(d, trip=dict(d["trip"], days=sorted(rest))))
    return out


def night_day(d, wd):
    """Weekday of the evening a night departure belongs to."""
    return wd if d["dep"] >= 1440 or d["dep"] % 1440 >= 720 else (wd - 1) % 7


def night_sort(m):
    m %= 1440
    return m + 1440 if m < 360 else m


def patterns_for_station(ds):
    # reference span per (direction, type, weekday): when does this kind of service run at all
    ref = {}
    for d in ds:
        if d["night"]:
            continue
        for wd in d["trip"]["days"]:
            k = (d["via"][0], d["trip"]["type"], wd)
            lo, hi = ref.get(k, (10 ** 9, -1))
            ref[k] = (min(lo, d["dep"]), max(hi, d["dep"]))

    ds = combine_coupled(ds)

    # 1. raw rows keyed by service + minute past the hour
    raw = defaultdict(list)
    for d in ds:
        t = d["trip"]
        raw[(serie_of(t["num"], t["agency"]), d["dep"] % 60, t["type"], t["agency"], d["night"], d["via"][0])].append(d)
    rows = [{"series": {k[0]}, "minutes": {k[1]}, "type": k[2], "agency": k[3], "night": k[4], "dir": k[5],
             "items": v} for k, v in raw.items()]

    def main_dest(row):
        return Counter(it["dest"] for it in row["items"]).most_common(1)[0][0]

    def volume(row):
        return sum(len(it["trip"]["days"]) for it in row["items"])

    def slots(row):
        return {(wd, it["dep"] // 20) for it in row["items"] for wd in it["trip"]["days"]}

    def mindist(a, b):
        return min(min(abs(x - y), 60 - abs(x - y)) for x in a for y in b)

    # 2. merge rows of the same service whose minutes differ slightly and that never run at the same time
    merged = True
    while merged:
        merged = False
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if (a["type"], a["agency"], a["night"], a["dir"]) != (b["type"], b["agency"], b["night"], b["dir"]):
                    continue
                if mindist(a["minutes"], b["minutes"]) > 3:
                    continue
                if a["series"] != b["series"] and (main_dest(a) != main_dest(b) or min(volume(a), volume(b)) < 20):
                    continue
                if slots(a) & slots(b):
                    continue
                a["series"] |= b["series"]
                a["minutes"] |= b["minutes"]
                a["items"] += b["items"]
                del rows[j]
                merged = True
                break
            if merged:
                break

    # 3. render
    out = []
    for row in rows:
        items = row["items"]
        night = row["night"]

        def by_day(its):
            res = defaultdict(list)
            for it in its:
                for wd in it["trip"]["days"]:
                    res[night_day(it, wd) if night else wd].append(it["dep"])
            return res

        union = by_day(items)
        ref_by_day = {}
        for wd, ts in union.items():
            ref_by_day[wd] = ref.get((row["dir"], row["type"], wd), (min(ts), max(ts)))

        lines = []
        dest_counts = Counter(it["dest"] for it in items)
        for dest, _ in dest_counts.most_common():
            own_items = [it for it in items if it["dest"] == dest]
            own = by_day(own_items)
            n = sum(len(v) for v in own.values())
            edge = all(not (390 <= t % 1440 < 1380) for ts in own.values() for t in ts)
            lines.append({
                "dest": dest,
                "opm": describe(own, union, ref_by_day, night),
                "minor": n <= 7 and edge and not night,
                "times": {DAYS[wd]: [fmt_time(t) for t in sorted(set(ts), key=night_sort if night else None)]
                          for wd, ts in sorted(own.items())},
            })

        all_times = {t % 1440 for ts in union.values() for t in ts}
        per_day = max(len(set(v)) for v in union.values())
        if per_day == 1 and len(all_times) <= 2 or len(all_times) == 1:
            ts = sorted(all_times, key=night_sort)
            vertrek = "/".join(fmt_time(t) for t in ts)
            sort = [1 if night else 0, night_sort(ts[0]) if night else ts[0] + 60]
        else:
            ms = sorted(row["minutes"])
            if ms[-1] - ms[0] > 30:  # wrap-around such as .59/.00
                ms = [m for m in ms if m > 30] + [m for m in ms if m <= 30]
            vertrek = "/".join(f".{m:02d}" for m in ms)
            sort = [1 if night else 0, ms[0] - 60 if ms[0] > 30 and ms[-1] < 30 else ms[0]]
            if night:
                sort[1] -= 60 * 24  # hourly night services before one-offs

        via = Counter(it["via"] for it in items).most_common(1)[0][0]
        out.append({
            "vertrek": vertrek,
            "sort": sort,
            "serie": "/".join(str(s) for s in sorted(row["series"], key=str)),
            "lines": lines,
            "spoor": plat_join(it["platform"] for it in items),
            "soort": row["type"],
            "vervoerder": row["agency"],
            "night": night,
            "minor": all(l["minor"] for l in lines),
            "intl": any(it["trip"]["agency"] in ("NSI", "GoVolta", "European Sleeper", "DB", "NMBS") for it in items),
            "via": list(via),
            "next": Counter(it["next"] for it in items).most_common(1)[0][0],
            # stations called at or passed, in order (for naming the direction)
            "path": list(Counter(it["path"] for it in items).most_common(1)[0][0]),
            "count": sum(len(v) for v in union.values()),
        })
    out.sort(key=lambda r: r["sort"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", default="gtfs-nl.zip", help="path to GTFS zip (downloaded if missing)")
    ap.add_argument("--download", action="store_true", help="(re)download the feed from OVapi")
    ap.add_argument("--start", "--week-start", dest="start",
                    help="Sunday the reference period starts (default: 2nd Sunday of December)")
    ap.add_argument("--weeks", type=int, help="number of weeks (default: all whole weeks in the feed, max 8)")
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    if args.download or not os.path.exists(args.gtfs):
        print(f"Downloading {GTFS_URL} ...", file=sys.stderr)
        req = urllib.request.Request(GTFS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as r, open(args.gtfs, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)

    feed = feed_info(args.gtfs)
    feed_end = dt.datetime.strptime(feed["feed_end_date"], "%Y%m%d").date()
    feed_start = dt.datetime.strptime(feed["feed_start_date"], "%Y%m%d").date()

    def whole_weeks(from_date):
        return ((feed_end - from_date).days + 1) // 7

    feed = feed_info(args.gtfs)
    feed_end = dt.datetime.strptime(feed["feed_end_date"], "%Y%m%d").date()
    feed_start = dt.datetime.strptime(feed["feed_start_date"], "%Y%m%d").date()

    def whole_weeks(from_date):
        return ((feed_end - from_date).days + 1) // 7

    if args.start:
        start = dt.date.fromisoformat(args.start)
        if start.weekday() != 6:
            sys.exit(f"--start {start} is not a Sunday")
    else:
        # the new timetable if the feed already has it, otherwise the first whole weeks in the feed
        start = second_sunday_of_december(feed_start.year)
        if start < feed_start or whole_weeks(start) < 1:
            print(f"Feed ({feed_start}..{feed_end}) does not cover the timetable change of {start} yet; "
                  f"using the current timetable", file=sys.stderr)
            start = feed_start + dt.timedelta(days=(6 - feed_start.weekday()) % 7)

    available = whole_weeks(start)
    weeks = args.weeks or min(available, 8)
    if start < feed_start or weeks < 1 or weeks > available:
        sys.exit(f"Feed covers {feed_start}..{feed_end}; {max(weeks, 1)} week(s) from {start} do not fit. "
                 f"Pick another --start or wait for a newer feed.")
    dates = [start + dt.timedelta(days=i) for i in range(weeks * 7)]
    print(f"Reference period {dates[0]}..{dates[-1]} ({weeks} weeks)", file=sys.stderr)

    cache = f"{args.gtfs}.{start.isoformat()}.{weeks}w.pickle"
    data = extract(args.gtfs, dates, cache)
    build(data, args.out)


if __name__ == "__main__":
    main()

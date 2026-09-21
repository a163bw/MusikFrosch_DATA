#!/usr/bin/env python3
"""Import all FroschData*_10fin.json files into the Musikfrosch PostgreSQL schema."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PLATFORM = "spotify"
IMPORT_SOURCE = "froschdata"
MOVED_RE = re.compile(r"\s+\(verlegt aus .+\)\s*$", re.IGNORECASE)

REQUIRED_TABLES = (
    "locations", "location_external_refs", "rooms", "room_external_refs",
    "events", "artists", "event_artists", "artist_platforms", "tracks",
    "track_artists", "artist_top_tracks", "event_tracks",
)
REQUIRED_EVENT_KEYS = {"date", "event", "items", "location", "tracks", "type", "url"}
REQUIRED_ITEM_KEYS = {"id", "name", "pop", "tracksID", "tracksName", "tracksPOP"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import all FroschData*_10fin.json into PostgreSQL.")
    p.add_argument("files", nargs="*", help="Optional explicit JSON files")
    p.add_argument("--directory", default=".", help="Directory used for automatic discovery")
    p.add_argument("--pattern", default="FroschData*_10fin.json", help="Glob pattern")
    p.add_argument("--location-map", default=None, help="Location/room mapping JSON")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="PostgreSQL DSN")
    p.add_argument("--default-timezone", default="Europe/Berlin")
    p.add_argument("--check-only", action="store_true")
    return p.parse_args()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "unknown"


def discover_files(args: argparse.Namespace) -> list[Path]:
    if args.files:
        files = [Path(x).expanduser().resolve() for x in args.files]
    else:
        directory = Path(args.directory).expanduser().resolve()
        files = sorted(p.resolve() for p in directory.glob(args.pattern))
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError("Missing file(s): " + ", ".join(missing))
    if not files:
        raise FileNotFoundError(f"No files found for pattern {args.pattern!r}")
    return files


def load_location_map(path_value: str | None) -> dict[str, dict[str, Any]]:
    if path_value:
        path = Path(path_value).expanduser().resolve()
    else:
        candidate = Path(__file__).resolve().with_name("froschdata_location_map.json")
        path = candidate if candidate.is_file() else None
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Location mapping must be a JSON object")
    return data


def load_json(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: JSON root must be a list")
    return data


def is_unresolved_artist_id(value: Any) -> bool:
    return str(value).strip().casefold() in {"", "0", "none", "null"}


def clean_location(raw_location: Any) -> tuple[str, bool]:
    raw = str(raw_location).strip()
    moved = bool(MOVED_RE.search(raw))
    return MOVED_RE.sub("", raw).strip(), moved


def resolve_venue(raw_location: Any, location_map: dict[str, dict[str, Any]], default_timezone: str) -> dict[str, Any]:
    base, moved = clean_location(raw_location)
    entry = dict(location_map.get(base, {}))
    location_name = str(entry.get("location_name") or base).strip()
    room_name = str(entry.get("room_name") or location_name).strip()
    location_key = str(entry.get("location_key") or slugify(location_name)).strip()
    room_key = str(entry.get("room_key") or slugify(room_name)).strip()
    timezone_name = str(entry.get("timezone") or default_timezone).strip()
    ZoneInfo(timezone_name)
    return {
        "moved": moved,
        "location_key": location_key,
        "location_name": location_name,
        "room_key": room_key,
        "room_name": room_name,
        "is_default_room": bool(entry.get("is_default_room", room_name == location_name)),
        "city": entry.get("city"),
        "country": entry.get("country"),
        "timezone": timezone_name,
        "website": entry.get("website"),
    }


def parse_datetime(value: Any, timezone_name: str) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
    return dt


def validate_file(path: Path, location_map: dict[str, dict[str, Any]], default_timezone: str) -> dict[str, Any]:
    data = load_json(path)
    raw_locations: Counter[str] = Counter()
    canonical_rooms: Counter[tuple[str, str]] = Counter()
    artist_ids: set[str] = set()
    track_ids: set[str] = set()
    dates: list[datetime] = []
    unresolved = empty_items = blank_names = 0
    identities: Counter[tuple[str, str, str, str]] = Counter()

    for ei, event in enumerate(data):
        if not isinstance(event, dict):
            raise ValueError(f"{path.name}: event #{ei} is not an object")
        missing = REQUIRED_EVENT_KEYS - event.keys()
        if missing:
            raise ValueError(f"{path.name}: event #{ei} missing {sorted(missing)}")
        if not isinstance(event["items"], list) or not isinstance(event["tracks"], list):
            raise ValueError(f"{path.name}: event #{ei}: items/tracks must be lists")
        if not str(event["url"]).strip():
            raise ValueError(f"{path.name}: event #{ei}: empty URL")

        venue = resolve_venue(event["location"], location_map, default_timezone)
        raw_locations[str(event["location"]).strip()] += 1
        canonical_rooms[(venue["location_name"], venue["room_name"])] += 1
        dt = parse_datetime(event["date"], venue["timezone"])
        dates.append(dt)
        identities[(str(event["url"]).strip(), venue["location_key"], venue["room_key"], dt.isoformat())] += 1
        if not str(event["event"]).strip():
            blank_names += 1
        if not event["items"]:
            empty_items += 1

        for ii, item in enumerate(event["items"]):
            if not isinstance(item, dict):
                raise ValueError(f"{path.name}: event #{ei}, item #{ii} is not an object")
            missing_item = REQUIRED_ITEM_KEYS - item.keys()
            if missing_item:
                raise ValueError(f"{path.name}: event #{ei}, item #{ii} missing {sorted(missing_item)}")
            ids, names, pops = item["tracksID"], item["tracksName"], item["tracksPOP"]
            if not all(isinstance(x, list) for x in (ids, names, pops)):
                raise ValueError(f"{path.name}: event #{ei}, item #{ii}: track arrays must be lists")
            if len({len(ids), len(names), len(pops)}) != 1:
                raise ValueError(f"{path.name}: event #{ei}, item #{ii}: track arrays have different lengths")
            if is_unresolved_artist_id(item["id"]):
                unresolved += 1
            else:
                artist_ids.add(str(item["id"]).strip())
            for tid in ids:
                tid = str(tid).strip()
                if not tid:
                    raise ValueError(f"{path.name}: event #{ei}: empty track id")
                track_ids.add(tid)
        for tid in event["tracks"]:
            tid = str(tid).strip()
            if not tid:
                raise ValueError(f"{path.name}: event #{ei}: empty event track id")
            track_ids.add(tid)

    return {
        "file": path.name,
        "events": len(data),
        "raw_locations": len(raw_locations),
        "canonical_rooms": len(canonical_rooms),
        "artist_ids": artist_ids,
        "track_ids": track_ids,
        "unresolved": unresolved,
        "empty_items": empty_items,
        "blank_names": blank_names,
        "duplicate_identities": sum(n - 1 for n in identities.values() if n > 1),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def print_validation(s: dict[str, Any]) -> None:
    print(f"\n{s['file']}")
    print(f"  Events:                     {s['events']}")
    print(f"  Raw-Location-Werte:         {s['raw_locations']}")
    print(f"  kanonische Location/Rooms:  {s['canonical_rooms']}")
    print(f"  eindeutige Spotify-Artists: {len(s['artist_ids'])}")
    print(f"  unresolved Artist-Einträge: {s['unresolved']}")
    print(f"  eindeutige Spotify-Tracks:  {len(s['track_ids'])}")
    print(f"  Events ohne items:          {s['empty_items']}")
    print(f"  Events ohne Eventnamen:     {s['blank_names']}")
    print(f"  doppelte Event-Identitäten: {s['duplicate_identities']}")
    if s["date_min"]:
        print(f"  Zeitraum: {s['date_min'].isoformat()} bis {s['date_max'].isoformat()}")


def check_schema(cur: Any) -> None:
    missing = []
    for table in REQUIRED_TABLES:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            missing.append(table)
    if missing:
        raise RuntimeError("Missing schema tables: " + ", ".join(missing))


def get_or_create_location(cur: Any, v: dict[str, Any]) -> int:
    cur.execute(
        "SELECT location_id FROM location_external_refs WHERE source=%s AND external_id=%s",
        (IMPORT_SOURCE, v["location_key"]),
    )
    row = cur.fetchone()
    if row:
        location_id = int(row[0])
        cur.execute(
            """UPDATE locations SET city=COALESCE(city,%s), country=COALESCE(country,%s),
               timezone=COALESCE(timezone,%s), website=COALESCE(website,%s) WHERE id=%s""",
            (v["city"], v["country"], v["timezone"], v["website"], location_id),
        )
        return location_id

    if v["city"]:
        cur.execute(
            """SELECT id FROM locations WHERE lower(name)=lower(%s)
               AND (city IS NULL OR lower(city)=lower(%s)) ORDER BY id LIMIT 2""",
            (v["location_name"], v["city"]),
        )
    else:
        cur.execute("SELECT id FROM locations WHERE lower(name)=lower(%s) ORDER BY id LIMIT 2", (v["location_name"],))
    rows = cur.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"Ambiguous existing location: {v['location_name']}")
    if rows:
        location_id = int(rows[0][0])
        cur.execute(
            """UPDATE locations SET city=COALESCE(city,%s), country=COALESCE(country,%s),
               timezone=COALESCE(timezone,%s), website=COALESCE(website,%s) WHERE id=%s""",
            (v["city"], v["country"], v["timezone"], v["website"], location_id),
        )
    else:
        cur.execute(
            """INSERT INTO locations(name,city,country,timezone,website,active)
               VALUES(%s,%s,%s,%s,%s,TRUE) RETURNING id""",
            (v["location_name"], v["city"], v["country"], v["timezone"], v["website"]),
        )
        location_id = int(cur.fetchone()[0])

    cur.execute(
        """INSERT INTO location_external_refs(location_id,source,external_id,source_url)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(source,external_id) DO UPDATE SET location_id=EXCLUDED.location_id,
             source_url=COALESCE(EXCLUDED.source_url,location_external_refs.source_url)""",
        (location_id, IMPORT_SOURCE, v["location_key"], v["website"]),
    )
    return location_id


def get_or_create_room(cur: Any, v: dict[str, Any], location_id: int) -> int:
    external_id = f"{v['location_key']}:{v['room_key']}"
    cur.execute("SELECT room_id FROM room_external_refs WHERE source=%s AND external_id=%s", (IMPORT_SOURCE, external_id))
    row = cur.fetchone()
    if row:
        room_id = int(row[0])
        if v["is_default_room"]:
            cur.execute("UPDATE rooms SET is_default=TRUE WHERE id=%s", (room_id,))
        return room_id
    cur.execute(
        """INSERT INTO rooms(location_id,name,is_default,active) VALUES(%s,%s,%s,TRUE)
           ON CONFLICT(location_id,name) DO UPDATE SET
             is_default=rooms.is_default OR EXCLUDED.is_default, active=TRUE RETURNING id""",
        (location_id, v["room_name"], v["is_default_room"]),
    )
    room_id = int(cur.fetchone()[0])
    cur.execute(
        """INSERT INTO room_external_refs(room_id,source,external_id,source_url)
           VALUES(%s,%s,%s,NULL)
           ON CONFLICT(source,external_id) DO UPDATE SET room_id=EXCLUDED.room_id""",
        (room_id, IMPORT_SOURCE, external_id),
    )
    return room_id


def upsert_event(cur: Any, event: dict[str, Any], v: dict[str, Any], room_id: int) -> int:
    start = parse_datetime(event["date"], v["timezone"])
    status = "moved" if v["moved"] else "scheduled"
    cur.execute(
        """INSERT INTO events(room_id,name,start_datetime,status,event_type,source_url)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(source_url,room_id,start_datetime) DO UPDATE SET
             name=CASE WHEN btrim(EXCLUDED.name)<>'' THEN EXCLUDED.name ELSE events.name END,
             event_type=COALESCE(EXCLUDED.event_type,events.event_type),
             status=CASE WHEN EXCLUDED.status='moved' THEN 'moved' ELSE events.status END
           RETURNING id""",
        (room_id, str(event["event"]), start, status,
         str(event["type"]) if event["type"] is not None else None, str(event["url"]).strip()),
    )
    return int(cur.fetchone()[0])


def find_or_create_unresolved_artist_for_event(cur: Any, event_id: int, name: str) -> int:
    cur.execute(
        """SELECT a.id FROM artists a JOIN event_artists ea ON ea.artist_id=a.id
           WHERE ea.event_id=%s AND a.resolution_status='unresolved' AND a.name=%s
           ORDER BY a.id LIMIT 1""",
        (event_id, name),
    )
    row = cur.fetchone()
    if row:
        return int(row[0])
    cur.execute("INSERT INTO artists(name,resolution_status) VALUES(%s,'unresolved') RETURNING id", (name,))
    return int(cur.fetchone()[0])


def upsert_resolved_artist(cur: Any, item: dict[str, Any], cache: dict[str, tuple[int, int]]) -> tuple[int, int]:
    ext = str(item["id"]).strip()
    name = str(item["name"])
    pop = item["pop"]
    if ext in cache:
        artist_id, platform_id = cache[ext]
        cur.execute("UPDATE artists SET name=CASE WHEN btrim(%s)<>'' THEN %s ELSE name END, resolution_status='resolved' WHERE id=%s", (name, name, artist_id))
        cur.execute("UPDATE artist_platforms SET popularity=%s WHERE id=%s", (pop, platform_id))
        return artist_id, platform_id

    cur.execute("SELECT artist_id,id FROM artist_platforms WHERE platform=%s AND external_artist_id=%s", (PLATFORM, ext))
    row = cur.fetchone()
    if row:
        artist_id, platform_id = int(row[0]), int(row[1])
        cur.execute("UPDATE artists SET name=CASE WHEN btrim(%s)<>'' THEN %s ELSE name END, resolution_status='resolved' WHERE id=%s", (name, name, artist_id))
        cur.execute("UPDATE artist_platforms SET popularity=%s WHERE id=%s", (pop, platform_id))
    else:
        cur.execute("INSERT INTO artists(name,resolution_status) VALUES(%s,'resolved') RETURNING id", (name,))
        artist_id = int(cur.fetchone()[0])
        cur.execute(
            """INSERT INTO artist_platforms(artist_id,platform,external_artist_id,popularity)
               VALUES(%s,%s,%s,%s) RETURNING id""",
            (artist_id, PLATFORM, ext, pop),
        )
        platform_id = int(cur.fetchone()[0])
    cache[ext] = (artist_id, platform_id)
    return artist_id, platform_id


def upsert_track(cur: Any, ext: str, name: str | None, pop: int | None, cache: dict[str, int]) -> int:
    ext = str(ext).strip()
    if ext in cache:
        track_id = cache[ext]
        cur.execute("UPDATE tracks SET name=COALESCE(%s,name), popularity=COALESCE(%s,popularity) WHERE id=%s", (name, pop, track_id))
        return track_id
    cur.execute(
        """INSERT INTO tracks(platform,external_track_id,name,popularity) VALUES(%s,%s,%s,%s)
           ON CONFLICT(platform,external_track_id) DO UPDATE SET
             name=COALESCE(EXCLUDED.name,tracks.name), popularity=COALESCE(EXCLUDED.popularity,tracks.popularity)
           RETURNING id""",
        (PLATFORM, ext, name, pop),
    )
    track_id = int(cur.fetchone()[0])
    cache[ext] = track_id
    return track_id


def sync_artist_top_tracks(cur: Any, artist_id: int, platform_id: int, item: dict[str, Any], track_cache: dict[str, int]) -> None:
    internal: list[int] = []
    seen: set[int] = set()
    for ext, name, pop in zip(item["tracksID"], item["tracksName"], item["tracksPOP"]):
        tid = upsert_track(cur, str(ext), str(name) if name is not None else None, int(pop) if pop is not None else None, track_cache)
        cur.execute("INSERT INTO track_artists(track_id,artist_id,position) VALUES(%s,%s,NULL) ON CONFLICT(track_id,artist_id) DO NOTHING", (tid, artist_id))
        if tid not in seen:
            seen.add(tid)
            internal.append(tid)
    cur.execute("DELETE FROM artist_top_tracks WHERE artist_platform_id=%s", (platform_id,))
    for rank, tid in enumerate(internal, 1):
        cur.execute("INSERT INTO artist_top_tracks(artist_platform_id,track_id,rank) VALUES(%s,%s,%s)", (platform_id, tid, rank))


def sync_event_relations(cur: Any, event_id: int, artist_ids: list[int], track_ext_ids: list[Any], track_cache: dict[str, int]) -> None:
    cur.execute("DELETE FROM event_artists WHERE event_id=%s", (event_id,))
    cur.execute("DELETE FROM event_tracks WHERE event_id=%s", (event_id,))
    seen_a: set[int] = set()
    for aid in artist_ids:
        if aid in seen_a:
            continue
        seen_a.add(aid)
        cur.execute("INSERT INTO event_artists(event_id,artist_id) VALUES(%s,%s) ON CONFLICT(event_id,artist_id) DO NOTHING", (event_id, aid))
    seen_t: set[int] = set()
    rank = 0
    for ext in track_ext_ids:
        tid = upsert_track(cur, str(ext), None, None, track_cache)
        if tid in seen_t:
            continue
        seen_t.add(tid)
        rank += 1
        cur.execute("INSERT INTO event_tracks(event_id,track_id,rank) VALUES(%s,%s,%s)", (event_id, tid, rank))


def import_file(cur: Any, path: Path, location_map: dict[str, dict[str, Any]], default_timezone: str,
                artist_cache: dict[str, tuple[int, int]], track_cache: dict[str, int],
                location_cache: dict[str, int], room_cache: dict[str, int]) -> dict[str, int]:
    data = load_json(path)
    counters = {"events": 0, "resolved": 0, "unresolved": 0}
    for event in data:
        v = resolve_venue(event["location"], location_map, default_timezone)
        location_id = location_cache.get(v["location_key"])
        if location_id is None:
            location_id = get_or_create_location(cur, v)
            location_cache[v["location_key"]] = location_id
        room_ref = f"{v['location_key']}:{v['room_key']}"
        room_id = room_cache.get(room_ref)
        if room_id is None:
            room_id = get_or_create_room(cur, v, location_id)
            room_cache[room_ref] = room_id
        event_id = upsert_event(cur, event, v, room_id)

        event_artist_ids: list[int] = []
        for item in event["items"]:
            if is_unresolved_artist_id(item["id"]):
                aid = find_or_create_unresolved_artist_for_event(cur, event_id, str(item["name"]))
                event_artist_ids.append(aid)
                counters["unresolved"] += 1
            else:
                aid, pid = upsert_resolved_artist(cur, item, artist_cache)
                event_artist_ids.append(aid)
                sync_artist_top_tracks(cur, aid, pid, item, track_cache)
                counters["resolved"] += 1
        sync_event_relations(cur, event_id, event_artist_ids, event["tracks"], track_cache)
        counters["events"] += 1
    return counters


def cleanup_orphan_unresolved(cur: Any) -> int:
    cur.execute(
        """DELETE FROM artists a WHERE a.resolution_status='unresolved'
           AND NOT EXISTS(SELECT 1 FROM event_artists ea WHERE ea.artist_id=a.id)
           AND NOT EXISTS(SELECT 1 FROM track_artists ta WHERE ta.artist_id=a.id)
           AND NOT EXISTS(SELECT 1 FROM artist_platforms ap WHERE ap.artist_id=a.id)"""
    )
    return cur.rowcount


def main() -> int:
    args = parse_args()
    try:
        ZoneInfo(args.default_timezone)
        files = discover_files(args)
        location_map = load_location_map(args.location_map)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print("Gefundene Dateien:")
    for path in files:
        print(f"  - {path.name}")

    global_artists: set[str] = set()
    global_tracks: set[str] = set()
    total_events = total_unresolved = total_blank = 0
    try:
        for path in files:
            stats = validate_file(path, location_map, args.default_timezone)
            print_validation(stats)
            total_events += stats["events"]
            total_unresolved += stats["unresolved"]
            total_blank += stats["blank_names"]
            global_artists.update(stats["artist_ids"])
            global_tracks.update(stats["track_ids"])
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 3

    print("\nGesamt")
    print(f"  Dateien:                     {len(files)}")
    print(f"  Events:                      {total_events}")
    print(f"  eindeutige Spotify-Artists:  {len(global_artists)}")
    print(f"  unresolved Artist-Einträge:  {total_unresolved}")
    print(f"  eindeutige Spotify-Tracks:   {len(global_tracks)}")
    print(f"  Events ohne Eventnamen:      {total_blank}")

    if args.check_only:
        print("\nCHECK-ONLY: keine Datenbankänderung durchgeführt.")
        return 0

    try:
        import psycopg
    except ImportError:
        print('Install dependency with: python -m pip install "psycopg[binary]>=3.1"', file=sys.stderr)
        return 4

    artist_cache: dict[str, tuple[int, int]] = {}
    track_cache: dict[str, int] = {}
    location_cache: dict[str, int] = {}
    room_cache: dict[str, int] = {}

    try:
        with psycopg.connect(args.dsn or "") as conn:
            with conn.cursor() as cur:
                check_schema(cur)
                for path in files:
                    r = import_file(cur, path, location_map, args.default_timezone,
                                    artist_cache, track_cache, location_cache, room_cache)
                    print(f"\nImportiert {path.name}: {r['events']} Events, {r['resolved']} resolved, {r['unresolved']} unresolved Artist-Einträge")
                removed = cleanup_orphan_unresolved(cur)
        print("\nSQL-Import erfolgreich.")
        print(f"  Dateien:            {len(files)}")
        print(f"  Events aus Quellen: {total_events}")
        print(f"  Locations:          {len(location_cache)}")
        print(f"  Rooms:              {len(room_cache)}")
        print(f"  Spotify-Artists:    {len(artist_cache)}")
        print(f"  Spotify-Tracks:     {len(track_cache)}")
        print(f"  entfernte Orphans:  {removed}")
        return 0
    except Exception as exc:
        print(f"SQL import failed; transaction rolled back: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())

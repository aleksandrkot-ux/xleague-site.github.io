import os
import re
import difflib
import requests
import gspread
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from countries import COUNTRIES

VERSION = "xleague-matcher-2026-09-11-reconcile2"

FIXTURE_CACHE_FILE = Path("/root/xleague-api/matcher_fixture_cache.json")
TEAM_FIXTURE_CACHE_FILE = Path("/root/xleague-api/matcher_team_fixture_cache.json")
FIXTURE_CACHE_TTL = 6 * 60 * 60  # 6 hours

# TEST workbook only
SHEET_ID = "1aOaRL3yJMFQHJZLrMpUNI9BylVS3WeaLpVW8FDLLQnY"
SEASON = 2
TABLE_TZ = timezone(timedelta(hours=3))

MAX_TIME_DIFF = 60
READY_THRESHOLD = 80

BAD_TEAM_KEYS = {
    "загрузка",
    "загрузка...",
    "loading",
    "loading...",
    "#n/a",
    "#ref!",
    "#value!",
    "#error!",
    "#name?",
    "#div/0!",
}

def is_valid_team_key(value):
    key = str(value or "").strip()
    if not key:
        return False

    normalized = key.lower().replace("…", "...")

    if normalized in BAD_TEAM_KEYS:
        return False

    if normalized.startswith("#"):
        return False

    return True

# tech_fixtures exact layout:
# A match_uid | B tour | C date | D time | E match | F country
# G fixture_id | H home_team_id | I away_team_id | J bind_status
# K api_home | L api_away | M api_date | N time_diff_min
# O confidence | P review

def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower().strip())
    return re.sub(r"\s+", " ", s).strip()

def fuzzy(a, b):
    return int(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() * 100)

def api_fixtures(api_key, date):
    r = requests.get(
        "https://v3.football.api-sports.io/fixtures",
        headers={"x-apisports-key": api_key},
        params={"date": date},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("response", [])

def api_team_fixtures(api_key, team_id, season=2026):
    r = requests.get(
        "https://v3.football.api-sports.io/fixtures",
        headers={"x-apisports-key": api_key},
        params={"team": team_id, "season": season},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("response", [])

def add_team(ws, memory, key, team_id, api_name, country):
    key = str(key).strip()

    if not is_valid_team_key(key):
        print(f"LEARN SKIP: invalid team key: {key!r}")
        return False

    if not team_id:
        return False

    team_id = int(team_id)
    existing_row = team_row_by_key.get(key)
    status = "MANUAL" if key in manual_team_keys else "CONFIRMED"

    # All source-team keys are pre-created in tech_teams. If a key is somehow
    # missing, append it once and immediately remember its real physical row.
    if not isinstance(existing_row, int) or existing_row < 2:
        ws.append_row(
            [key, team_id, api_name, country, status, ""],
            value_input_option="USER_ENTERED",
        )
        new_row = len(ws.get_all_values())
        team_row_by_key[key] = new_row
        team_state_by_key[key] = [
            key, str(team_id), str(api_name), str(country), status, ""
        ]
        memory[key] = team_id
        print(f"LEARN NEW: {key} -> {api_name} ({team_id})")
        return True

    current = team_state_by_key.get(key, [""] * 6)
    current = current + [""] * (6 - len(current))
    desired_be = [str(team_id), str(api_name), str(country), status]
    current_be = [
        str(current[1]).strip(),
        str(current[2]).strip(),
        str(current[3]).strip(),
        str(current[4]).strip(),
    ]

    # Important for quota: never rewrite a team row that is already correct.
    if current_be == desired_be:
        memory[key] = team_id
        print(f"LEARN NO WRITE: {key} already {api_name} ({team_id})")
        return True

    try:
        # Update existing UNRESOLVED/old row in place.
        # Column F manual_team_id is preserved.
        ws.update(
            range_name=f"B{existing_row}:E{existing_row}",
            values=[[team_id, api_name, country, status]],
        )
        memory[key] = team_id
        current[1:5] = desired_be
        team_state_by_key[key] = current
        print(f"LEARN UPDATE: {key} -> {api_name} ({team_id})")
        return True

    except gspread.exceptions.APIError as e:
        response = getattr(e, "response", None)
        status_code = getattr(response, "status_code", None)

        if status_code == 429:
            print(
                f"LEARN SKIPPED: Google Sheets write quota | "
                f"{key} -> {api_name} ({team_id})"
            )
            return False

        raise

def reconcile_team_duplicates(ws, values):
    """
    Keep exactly one physical tech_teams row per our_key.

    Canonical row = first physical occurrence.
    Data = best duplicate:
      manual_team_id > confirmed API id > unresolved.
    Extra duplicate rows are cleared (not deleted), so worksheet row numbers
    do not shift during the matcher run.
    """
    groups = {}

    for sheet_row, raw in enumerate(values[1:], start=2):
        row = raw + [""] * (6 - len(raw))
        key = row[0].strip()
        if key:
            groups.setdefault(key, []).append((sheet_row, row[:6]))

    updates = []
    duplicate_count = 0

    def rank(item):
        _, row = item
        status = row[4].strip().upper()
        return (
            1 if row[5].strip() else 0,                       # manual_team_id
            1 if row[1].strip() else 0,                       # API id
            1 if status in {"MANUAL", "CONFIRMED"} else 0,
            1 if row[2].strip() else 0,                       # API name
            1 if row[3].strip() else 0,                       # country
        )

    for key, items in groups.items():
        if len(items) < 2:
            continue

        canonical_row, canonical = items[0]
        _, best = max(items, key=rank)

        manual_id = best[5].strip() or next(
            (r[5].strip() for _, r in items if r[5].strip()), ""
        )
        api_id = manual_id or best[1].strip() or next(
            (r[1].strip() for _, r in items if r[1].strip()), ""
        )
        api_name = best[2].strip() or next(
            (r[2].strip() for _, r in items if r[2].strip()), ""
        )
        country = best[3].strip() or next(
            (r[3].strip() for _, r in items if r[3].strip()), ""
        )

        if manual_id:
            status = "MANUAL"
        elif api_id:
            status = "CONFIRMED"
        else:
            status = "UNRESOLVED"

        merged = [key, api_id, api_name, country, status, manual_id]

        if [str(x).strip() for x in canonical[:6]] != [str(x).strip() for x in merged]:
            updates.append({
                "range": f"A{canonical_row}:F{canonical_row}",
                "values": [merged],
            })

        for duplicate_row, _ in items[1:]:
            updates.append({
                "range": f"A{duplicate_row}:F{duplicate_row}",
                "values": [["", "", "", "", "", ""]],
            })
            duplicate_count += 1

    if updates:
        ws.batch_update(updates)
        print(f"TEAM DEDUPE: cleared duplicates={duplicate_count}")
        return ws.get_all_values()

    return values


def load_fixture_cache():
    try:
        if FIXTURE_CACHE_FILE.exists():
            return json.loads(FIXTURE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print("FIXTURE CACHE LOAD ERROR:", e)
    return {}


def save_fixture_cache(cache):
    try:
        FIXTURE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("FIXTURE CACHE SAVE ERROR:", e)


def load_team_fixture_cache():
    try:
        if TEAM_FIXTURE_CACHE_FILE.exists():
            return json.loads(
                TEAM_FIXTURE_CACHE_FILE.read_text(encoding="utf-8")
            )
    except Exception as e:
        print("TEAM FIXTURE CACHE LOAD ERROR:", e)
    return {}


def save_team_fixture_cache(cache):
    try:
        TEAM_FIXTURE_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("TEAM FIXTURE CACHE SAVE ERROR:", e)


fixture_disk_cache = load_fixture_cache()
team_fixture_disk_cache = load_team_fixture_cache()

print("VERSION:", VERSION)
API_KEY = os.environ["API_FOOTBALL_KEY"]
gc = gspread.service_account(filename="/root/xleague-api/google-service-account.json")
sh = gc.open_by_key(SHEET_ID)

matches_ws = sh.worksheet("📅Матчи")
refs_ws = sh.worksheet("Справочники")
fixtures_ws = sh.worksheet("tech_fixtures")
teams_ws = sh.worksheet("tech_teams")

matches = matches_ws.get_all_values()
refs = refs_ws.get_all_values()
fixture_values = fixtures_ws.get_all_values()
team_values = teams_ws.get_all_values()

# Repair old duplicate tech_teams rows before building any row maps.
team_values = reconcile_team_duplicates(teams_ws, team_values)

source_by_uid = {}
for row in matches[3:]:
    if len(row) >= 15 and row[12].strip():
        source_by_uid[row[12].strip()] = row

team_memory = {}
team_row_by_key = {}
team_state_by_key = {}
manual_sync_updates = []

for sheet_row, row in enumerate(team_values[1:], start=2):
    row = row + [""] * (6 - len(row))

    our_key = row[0].strip()
    api_team_id = row[1].strip()
    manual_team_id = row[5].strip()

    if not our_key:
        continue

    # After reconciliation there must be one physical row per key.
    if our_key not in team_row_by_key:
        team_row_by_key[our_key] = sheet_row
        team_state_by_key[our_key] = row[:6]

    effective_id = manual_team_id or api_team_id

    if not effective_id:
        continue

    try:
        team_memory[our_key] = int(effective_id)

        if manual_team_id:
            print(f"MANUAL TEAM: {our_key} -> {manual_team_id}")

            # manual_team_id is the absolute source of truth.
            # Keep columns C/D as they are, but synchronize:
            # B = effective API team ID
            # E = MANUAL
            current_api_name = row[2].strip()
            current_country = row[3].strip()

            if api_team_id != manual_team_id or row[4].strip().upper() != "MANUAL":
                manual_sync_updates.append({
                    "range": f"B{sheet_row}:E{sheet_row}",
                    "values": [[
                        int(manual_team_id),
                        current_api_name,
                        current_country,
                        "MANUAL",
                    ]],
                })

    except ValueError:
        print(f"BAD TEAM ID: {our_key} -> {effective_id}")

if manual_sync_updates:
    teams_ws.batch_update(manual_sync_updates)
    print(f"MANUAL rows synchronized: {len(manual_sync_updates)}")

manual_team_keys = {
    row[0].strip()
    for row in team_values[1:]
    if len(row) >= 6 and row[0].strip() and row[5].strip()
}

print(f"Известных команд в tech_teams: {len(team_memory)}")

# First consume manual approvals.
for sheet_row, row in enumerate(fixture_values[1:], start=2):
    row = row + [""] * (16 - len(row))
    uid = row[0].strip()
    if row[15].strip().upper() != "OK":
        continue

    # This approval was already consumed earlier.
    # Do not rewrite teams and CONFIRMED every 3 minutes.
    if row[9].strip().upper() == "CONFIRMED":
        continue

    source = source_by_uid.get(uid)
    if source is None:
        print(uid, "OK SKIP: UID не найден в 📅Матчи")
        continue

    try:
        home_id, away_id = int(row[7]), int(row[8])
    except ValueError:
        print(uid, "OK SKIP: некорректные team_id")
        continue

    home_key, away_key = source[13].strip(), source[14].strip()

    if home_key in manual_team_keys or away_key in manual_team_keys:
        print(uid, "OK RESET: есть manual_team_id, требуется новый поиск")
        fixtures_ws.update(
            range_name=f"J{sheet_row}:P{sheet_row}",
            values=[["CHECK", row[10], row[11], row[12], row[13], row[14], ""]],
        )
        continue

    api_home, api_away, country = row[10].strip(), row[11].strip(), row[5].strip()

    if not api_home or not api_away:
        print(uid, "OK SKIP: api_home/api_away пустые")
        continue

    add_team(teams_ws, team_memory, home_key, home_id, api_home, country)
    add_team(teams_ws, team_memory, away_key, away_id, api_away, country)
    fixtures_ws.update(range_name=f"J{sheet_row}", values=[["CONFIRMED"]])
    print(uid, "| OK -> CONFIRMED")

print(f"Команд после обучения: {len(team_memory)}")

fixture_values = fixtures_ws.get_all_values()
fixture_row_by_uid = {}
fixture_old_by_uid = {}
for sheet_row, row in enumerate(fixture_values[1:], start=2):
    row = row + [""] * (16 - len(row))
    uid = row[0].strip()
    if uid:
        fixture_row_by_uid[uid] = sheet_row
        fixture_old_by_uid[uid] = row

# Ensure every team from source matches exists in tech_teams.
# Unknown teams are added with empty API IDs so they are visible
# and can receive manual_team_id if automatic matching fails.
missing_team_rows = []

for row in matches[3:]:
    row = row + [""] * (15 - len(row))

    match_name = row[5].strip()
    home_key = row[13].strip()
    away_key = row[14].strip()

    if not match_name:
        continue

    country = next(
        (ref[17].strip() for ref in refs if len(ref) >= 18 and ref[16].strip() == match_name),
        "",
    )

    for team_key in (home_key, away_key):
        if not is_valid_team_key(team_key):
            print(f"TEAM KEY SKIP: invalid temporary value: {team_key!r}")
            continue

        if team_key not in team_row_by_key:
            missing_team_rows.append(
                [team_key, "", "", country, "UNRESOLVED", ""]
            )
            # Reserve in memory so the same key is queued only once.
            # Real row numbers are assigned immediately after append_rows().
            team_row_by_key[team_key] = None

if missing_team_rows:
    first_new_row = len(team_values) + 1

    teams_ws.append_rows(
        missing_team_rows,
        value_input_option="USER_ENTERED",
    )

    for offset, new_team in enumerate(missing_team_rows):
        key = new_team[0]
        real_row = first_new_row + offset
        team_row_by_key[key] = real_row
        team_state_by_key[key] = new_team[:6]

    team_values.extend(missing_team_rows)
    print(f"Добавлено новых команд в tech_teams: {len(missing_team_rows)}")

date_cache = {}
uid_updates = []

for sheet_row, row in enumerate(matches[3:], start=4):
    row = row + [""] * (15 - len(row))
    tour, match_id = row[0].strip(), row[1].strip()
    date, match_time = row[2].strip(), row[3].strip()
    match_name = row[5].strip()
    uid, home_key, away_key = row[12].strip(), row[13].strip(), row[14].strip()

    if not all([tour, match_id, date, match_time, match_name]):
        continue

    try:
        int(tour)
    except ValueError:
        continue

    if not uid:
        try:
            uid = f"S{SEASON}-T{int(tour):02d}-{int(match_id):03d}"
        except ValueError:
            print("Некорректный tour/id:", match_name)
            continue

        uid_updates.append({
            "range": f"M{sheet_row}",
            "values": [[uid]],
        })
        print("UID:", match_name, "->", uid)

    country = next(
        (ref[17].strip() for ref in refs if len(ref) >= 18 and ref[16].strip() == match_name),
        None,
    )
    if not country:
        print(uid, "SKIP: страна не найдена")
        continue

    api_country = COUNTRIES.get(country, country)

    try:
        day, month = date.split(".")
        query_date = f"2026-{month}-{day}"
        local_dt = datetime.strptime(
            f"{query_date} {match_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TABLE_TZ)
        target_utc = local_dt.astimezone(timezone.utc)
    except Exception:
        print(uid, "SKIP: ошибка даты/времени")
        continue

    known_home = team_memory.get(home_key)
    known_away = team_memory.get(away_key)

    manual_home = home_key in manual_team_keys
    manual_away = away_key in manual_team_keys

    # Production shortcut:
    # skip an already bound fixture if its stored team IDs still agree
    # with the current team mapping, including manual_team_id.
    old_fixture = fixture_old_by_uid.get(uid)

    if old_fixture:
        old_fixture = old_fixture + [""] * (16 - len(old_fixture))
        old_fixture_id = old_fixture[6].strip()
        old_status = old_fixture[9].strip().upper()
        old_review = old_fixture[15].strip().upper()

        # Fully manual match: detach it completely from API data.
        # Keep P=MANUAL, clear all stored fixture/team/API fields,
        # and mark J visibly for control from Google Sheets.
        if old_review == "MANUAL":
            manual_row = fixture_row_by_uid.get(uid)
            desired_manual_binding = [
                "", "", "", "NO ID - MANUAL", "", "", "", "", ""
            ]
            current_manual_binding = [
                str(x).strip() for x in old_fixture[6:15]
            ]

            if manual_row and current_manual_binding != desired_manual_binding:
                fixtures_ws.update(
                    range_name=f"G{manual_row}:O{manual_row}",
                    values=[desired_manual_binding],
                )
                print(uid, "MANUAL DETACH: API binding cleared")
            else:
                print(uid, "MANUAL NO WRITE: already detached")

            continue

        try:
            old_home_id = int(old_fixture[7]) if old_fixture[7].strip() else None
            old_away_id = int(old_fixture[8]) if old_fixture[8].strip() else None
        except ValueError:
            old_home_id = None
            old_away_id = None

        mapping_matches = (
            old_home_id == known_home
            and old_away_id == known_away
        )

        if (
            old_fixture_id
            and old_status in {"READY", "CONFIRMED"}
            and mapping_matches
        ):
            print(uid, "SKIP: fixture already bound", old_fixture_id)
            continue

    best = None

    # Если команда подтверждена вручную через manual_team_id,
    # её ID является абсолютным якорем. Время здесь не ограничивает поиск.
    if manual_home or manual_away:
        anchor_id = known_home if manual_home else known_away
        anchor_side = "home" if manual_home else "away"

        print(
            uid,
            f"MANUAL SEARCH: {anchor_side} team_id={anchor_id}"
        )

        manual_cache_key = f"{anchor_id}:2026"
        cached = team_fixture_disk_cache.get(manual_cache_key)

        cache_valid = (
            cached
            and time.time() - cached.get("timestamp", 0) < FIXTURE_CACHE_TTL
        )

        if cache_valid:
            manual_games = cached.get("games", [])
            print(uid, f"MANUAL CACHE: team_id={anchor_id}")
        else:
            try:
                manual_games = api_team_fixtures(API_KEY, anchor_id, 2026)

                team_fixture_disk_cache[manual_cache_key] = {
                    "timestamp": time.time(),
                    "games": manual_games,
                }
                save_team_fixture_cache(team_fixture_disk_cache)

                print(uid, f"MANUAL API: team_id={anchor_id}")

            except Exception as e:
                print(uid, "MANUAL API ERROR:", e)

                # If API temporarily fails, stale cached team fixtures
                # are still better than losing previously downloaded data.
                if cached:
                    manual_games = cached.get("games", [])
                    print(uid, f"MANUAL STALE CACHE: team_id={anchor_id}")
                else:
                    manual_games = []

        candidates = []

        source_date = local_dt.date()

        for game in manual_games:
            fixture_dt = datetime.fromisoformat(game["fixture"]["date"])
            fixture_local = fixture_dt.astimezone(TABLE_TZ)

            # Широкое окно: два календарных дня в обе стороны.
            date_diff = abs((fixture_local.date() - source_date).days)
            if date_diff > 2:
                continue

            api_home_id = game["teams"]["home"]["id"]
            api_away_id = game["teams"]["away"]["id"]

            # Manual IDs are absolute anchors.
            # If both teams are manual, both exact IDs must match their sides.
            if manual_home and manual_away:
                if api_home_id != known_home or api_away_id != known_away:
                    continue
            elif manual_home:
                if api_home_id != known_home:
                    continue
            elif manual_away:
                if api_away_id != known_away:
                    continue

            api_home = game["teams"]["home"]["name"]
            api_away = game["teams"]["away"]["name"]

            time_diff = abs(
                (fixture_dt - target_utc).total_seconds()
            ) / 60

            # Если второй участник уже известен по ID — это самый сильный признак.
            if manual_home:
                opponent_known = known_away
                opponent_api_id = api_away_id
                opponent_score = (
                    100 if opponent_known == opponent_api_id
                    else fuzzy(away_key, api_away)
                )
            else:
                opponent_known = known_home
                opponent_api_id = api_home_id
                opponent_score = (
                    100 if opponent_known == opponent_api_id
                    else fuzzy(home_key, api_home)
                )

            candidate = {
                "fixture_id": game["fixture"]["id"],
                "home_team_id": api_home_id,
                "away_team_id": api_away_id,
                "api_home": api_home,
                "api_away": api_away,
                "api_date": game["fixture"]["date"],
                "time_diff": round(time_diff),
                "confidence": 100,
                "home_mode": "ID" if manual_home else "AUTO",
                "away_mode": "ID" if manual_away else "AUTO",
                "_date_diff": date_diff,
                "_opponent_score": opponent_score,
            }

            candidates.append(candidate)

        if candidates:
            # 1. совпадение соперника
            # 2. близость календарной даты
            # 3. время только как последний tie-breaker
            best = max(
                candidates,
                key=lambda x: (
                    x["_opponent_score"],
                    -x["_date_diff"],
                    -x["time_diff"],
                ),
            )

            best.pop("_date_diff", None)
            best.pop("_opponent_score", None)

            print(
                uid,
                "MANUAL FOUND:",
                best["api_home"],
                "-",
                best["api_away"],
                "| fixture",
                best["fixture_id"],
            )

            # После подтверждения fixture обучаем обе команды.
            # Для manual-команды add_team сохранит manual_team_id в колонке F.
            add_team(
                teams_ws,
                team_memory,
                home_key,
                best["home_team_id"],
                best["api_home"],
                country,
            )
            add_team(
                teams_ws,
                team_memory,
                away_key,
                best["away_team_id"],
                best["api_away"],
                country,
            )

    else:
        query_dt = datetime.strptime(query_date, "%Y-%m-%d")
        api_dates = [
            (query_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
            query_date,
            (query_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
        ]

        games = []

        for api_date in api_dates:
            if api_date not in date_cache:
                cached = fixture_disk_cache.get(api_date)
                cache_valid = (
                    cached
                    and time.time() - cached.get("timestamp", 0) < FIXTURE_CACHE_TTL
                )

                if cache_valid:
                    date_cache[api_date] = cached.get("games", [])
                    print("CACHE:", api_date)
                else:
                    print("API:", api_date)
                    try:
                        date_cache[api_date] = api_fixtures(API_KEY, api_date)

                        fixture_disk_cache[api_date] = {
                            "timestamp": time.time(),
                            "games": date_cache[api_date],
                        }
                        save_fixture_cache(fixture_disk_cache)

                    except Exception as e:
                        print("API ERROR:", api_date, e)

                        # If API temporarily fails, stale cache is still better
                        # than throwing away previously downloaded fixtures.
                        if cached:
                            date_cache[api_date] = cached.get("games", [])
                            print("STALE CACHE:", api_date)
                        else:
                            date_cache[api_date] = []

            games.extend(date_cache[api_date])

        for game in games:
            if game["league"]["country"] != api_country:
                continue

            fixture_dt = datetime.fromisoformat(game["fixture"]["date"])
            time_diff = abs(
                (fixture_dt - target_utc).total_seconds()
            ) / 60

            if time_diff > MAX_TIME_DIFF:
                continue

            api_home_id = game["teams"]["home"]["id"]
            api_away_id = game["teams"]["away"]["id"]
            api_home = game["teams"]["home"]["name"]
            api_away = game["teams"]["away"]["name"]

            if known_home is not None:
                home_score, home_mode = (
                    (100 if known_home == api_home_id else 0),
                    "ID",
                )
            else:
                home_score, home_mode = fuzzy(home_key, api_home), "FUZZY"

            if known_away is not None:
                away_score, away_mode = (
                    (100 if known_away == api_away_id else 0),
                    "ID",
                )
            else:
                away_score, away_mode = fuzzy(away_key, api_away), "FUZZY"

            confidence = round(
                ((home_score + away_score) / 2) * 0.8
                + max(0, 100 - time_diff * 2) * 0.2
            )

            candidate = {
                "fixture_id": game["fixture"]["id"],
                "home_team_id": api_home_id,
                "away_team_id": api_away_id,
                "api_home": api_home,
                "api_away": api_away,
                "api_date": game["fixture"]["date"],
                "time_diff": round(time_diff),
                "confidence": confidence,
                "home_mode": home_mode,
                "away_mode": away_mode,
            }

            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate

    if not best:
        print(uid, "NOT FOUND:", match_name)

        existing_row = fixture_row_by_uid.get(uid)

        output = [
            uid, tour, date, match_time, match_name, country,
            "", "", "",
            "CHECK", "", "", "",
            "", "", "MANUAL TEAM ID REQUIRED",
        ]

        if existing_row:
            old_output = fixture_old_by_uid.get(uid, [""] * 16)[:16]
            if [str(x) for x in old_output] != [str(x) for x in output]:
                fixtures_ws.update(
                    range_name=f"A{existing_row}:P{existing_row}",
                    values=[output],
                )
            else:
                print(uid, "NO WRITE: unchanged CHECK row")
        else:
            fixtures_ws.append_row(
                output,
                value_input_option="USER_ENTERED",
            )
            fixture_row_by_uid[uid] = len(fixtures_ws.get_all_values())

        continue

    bind_status = (
        "READY"
        if best["confidence"] >= READY_THRESHOLD and best["time_diff"] <= 30
        else "CHECK"
    )

    existing_row = fixture_row_by_uid.get(uid)
    old = fixture_old_by_uid.get(uid, [""] * 16)
    review = old[15].strip()

    # Clear temporary unresolved marker once the fixture is found.
    if review.upper() in {"MANUAL TEAM ID REQUIRED", "TEAM UNRESOLVED"}:
        review = ""

    if old[6].strip() and old[6].strip() != str(best["fixture_id"]):
        review = ""
        print(uid, "REBOUND: fixture изменён, старый review сброшен")

    if (
        review.upper() == "OK"
        and old[9].strip() == "CONFIRMED"
        and old[6].strip() == str(best["fixture_id"])
    ):
        bind_status = "CONFIRMED"

    # AUTO learning:
    # once an automatically found fixture is trusted, remember both team IDs.
    # CHECK rows are deliberately not learned until manually confirmed.
    # Manual-team searches already learn both teams in their own branch above.
    if (
        not (manual_home or manual_away)
        and bind_status in {"READY", "CONFIRMED"}
    ):
        add_team(
            teams_ws,
            team_memory,
            home_key,
            best["home_team_id"],
            best["api_home"],
            country,
        )
        add_team(
            teams_ws,
            team_memory,
            away_key,
            best["away_team_id"],
            best["api_away"],
            country,
        )

    output = [
        uid, tour, date, match_time, match_name, country,
        best["fixture_id"], best["home_team_id"], best["away_team_id"],
        bind_status, best["api_home"], best["api_away"], best["api_date"],
        best["time_diff"], best["confidence"], review,
    ]

    if existing_row:
        old_output = fixture_old_by_uid.get(uid, [""] * 16)[:16]
        if [str(x) for x in old_output] != [str(x) for x in output]:
            fixtures_ws.update(
                range_name=f"A{existing_row}:P{existing_row}",
                values=[output],
            )
        else:
            print(uid, "NO WRITE: fixture unchanged")
    else:
        fixtures_ws.append_row(output, value_input_option="USER_ENTERED")
        fixture_row_by_uid[uid] = len(fixtures_ws.get_all_values())

    print(
        uid, "|", match_name, "=>", best["api_home"], "-", best["api_away"],
        "|", best["confidence"], bind_status,
        "|", best["home_mode"], "/", best["away_mode"]
    )

if uid_updates:
    matches_ws.batch_update(uid_updates)
    print(f"UID batch written: {len(uid_updates)}")

with open("/tmp/xleague-matcher.heartbeat", "w") as f:
    f.write(datetime.now(timezone.utc).isoformat())

print("\nMatcher finished.")

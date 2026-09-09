#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PID transfer monitor
====================
Sleduje a uklada zpozdeni prestupu mezi linkami 336 a 384 na uzlu Zlicin
(kazdy vsedni den rano). Cilem je zjistit, jak casto se prestup opravdu stihne.

Data se berou z Golemio API (https://api.golemio.cz), endpoint
/v2/pid/departureboards (odjezdove/prijezdove tabule s realtime zpozdenim).

Pouziti:
    python monitor.py selftest        # jednorazovy test API klice + parovani spoju
    python monitor.py collect         # bezi cele rano, sbira data, na konci zapise vysledek
    python monitor.py collect --once  # jeden dotaz ted (pro ladeni)
    python monitor.py report          # textova statistika z data/results.csv
    python monitor.py report --html data/report.html
    python monitor.py verify-timetable "C:\\...\\Jizdni rady"   # kontrola casu proti GTFS

Zadne externi zavislosti - jen standardni knihovna Pythonu 3.9+.
"""

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CONN_PATH = os.path.join(HERE, "connections.json")
DATA_DIR = os.path.join(HERE, "data")
LOG_DIR = os.path.join(HERE, "logs")
SAMPLES_PATH = os.path.join(DATA_DIR, "samples.jsonl")
RESULTS_PATH = os.path.join(DATA_DIR, "results.csv")

RESULTS_FIELDS = [
    "date", "conn_id", "label", "weekday",
    "feeder_route", "feeder_sched_arr", "feeder_delay_s", "feeder_eff_arr",
    "feeder_canceled", "feeder_rt_available", "feeder_seen_count",
    "conn_route", "conn_sched_dep", "conn_delay_s", "conn_eff_dep",
    "conn_canceled", "conn_rt_available",
    "buffer_min", "margin_s", "transfer_ok", "verdict",
    "next_conn_sched_dep", "wait_if_missed_min",
    "polls_ok", "polls_fail", "notes",
]

WEEKDAYS_CS = ["Po", "Ut", "St", "Ct", "Pa", "So", "Ne"]


# --------------------------------------------------------------------------- #
# Cas / casova zona Europe/Prague (bez zavislosti na tzdata)
# --------------------------------------------------------------------------- #
def _last_sunday(year, month):
    d = dt.date(year, month, 31)
    while d.weekday() != 6:  # 6 = nedele
        d -= dt.timedelta(days=1)
    return d


def prague_offset_hours(aware_utc):
    """Posun Prahy vuci UTC v hodinach pro dany okamzik (EU pravidla DST)."""
    n = aware_utc.astimezone(dt.timezone.utc).replace(tzinfo=None)
    y = n.year
    start = dt.datetime(y, 3, _last_sunday(y, 3).day, 1, 0)   # 01:00 UTC
    end = dt.datetime(y, 10, _last_sunday(y, 10).day, 1, 0)   # 01:00 UTC
    return 2 if start <= n < end else 1


def to_prague(aware_utc):
    return aware_utc.astimezone(dt.timezone.utc) + dt.timedelta(hours=prague_offset_hours(aware_utc))


def prague_now():
    return to_prague(dt.datetime.now(dt.timezone.utc))


def parse_iso_utc(s):
    """'2019-05-18T07:38:20.000Z' -> aware datetime v UTC."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def prague_hhmm(aware_utc):
    return to_prague(aware_utc).strftime("%H:%M")


def hhmm_to_minutes(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# --------------------------------------------------------------------------- #
# Konfigurace
# --------------------------------------------------------------------------- #
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    # Env promenna ma prednost (pro cloud / CI, aby klic nemusel byt v repu).
    env_key = os.environ.get("GOLEMIO_API_KEY")
    if env_key:
        cfg["api_key"] = env_key.strip()
    key = cfg.get("api_key") or ""
    if not key or "PASTE" in key or "SEM" in key.upper():
        sys.exit("Chybi Golemio API klic: nastav GOLEMIO_API_KEY nebo 'api_key' v config.json.")
    cfg.setdefault("api_base", "https://api.golemio.cz")
    cfg.setdefault("node_asw_id", 1141)
    cfg.setdefault("node_name", "Zlicin")
    cfg.setdefault("poll_interval_s", 60)
    cfg.setdefault("transfer_buffer_min", 1)
    return cfg


def load_connections():
    with open(CONN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Golemio API
# --------------------------------------------------------------------------- #
def api_get(cfg, path, params):
    url = cfg["api_base"].rstrip("/") + path + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "X-Access-Token": cfg["api_key"],
        "accept": "application/json",
        "User-Agent": "pid-transfer-monitor/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_board(cfg):
    return api_get(cfg, "/v2/pid/departureboards", {
        "aswIds": cfg["node_asw_id"],
        "mode": "mixed",
        "minutesBefore": 20,
        "minutesAfter": 80,
        "limit": 400,
        "order": "timetable",
        "airCondition": "false",
    })


# --------------------------------------------------------------------------- #
# Vyhledani spoje v tabuli
# --------------------------------------------------------------------------- #
def _stoptime(dep, kind):
    return dep.get("arrival_timestamp") if kind == "arrival" else dep.get("departure_timestamp")


def find_departure(board, route, hhmm, kind, tol_min=2):
    """Najde v tabuli spoj dane linky, ktery ma planovany cas ~ hhmm."""
    target = hhmm_to_minutes(hhmm)
    best, best_diff = None, 10 ** 9
    for dep in board.get("departures", []):
        if str(dep.get("route", {}).get("short_name")) != str(route):
            continue
        st = _stoptime(dep, kind)
        sched = (st or {}).get("scheduled")
        if not sched:
            # koncova/pocatecni zastavka nekdy nema pozadovany smer casu - zkus druhy
            st = _stoptime(dep, "departure" if kind == "arrival" else "arrival")
            sched = (st or {}).get("scheduled")
        if not sched:
            continue
        got = hhmm_to_minutes(prague_hhmm(parse_iso_utc(sched)))
        diff = abs(got - target)
        if diff <= tol_min and diff < best_diff:
            best, best_diff = dep, diff
    return best


def sample_from_departure(dep, kind):
    """Vytahne relevantni realtime hodnoty z jednoho zaznamu tabule."""
    st = _stoptime(dep, kind) or {}
    delay = dep.get("delay", {}) or {}
    trip = dep.get("trip", {}) or {}
    sched = st.get("scheduled")
    predicted = st.get("predicted")
    delay_s = delay.get("seconds") if delay.get("is_available") else None
    if delay_s is None and predicted and sched:
        delay_s = int((parse_iso_utc(predicted) - parse_iso_utc(sched)).total_seconds())
    return {
        "sched": sched,
        "predicted": predicted,
        "delay_s": delay_s,
        "rt_available": bool(delay.get("is_available")),
        "canceled": bool(trip.get("is_canceled")),
        "is_at_stop": bool(trip.get("is_at_stop")),
        "trip_id": trip.get("id"),
        "last_stop": (dep.get("last_stop") or {}).get("name"),
    }


# --------------------------------------------------------------------------- #
# Sber dat
# --------------------------------------------------------------------------- #
def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _log(msg):
    ts = prague_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "collect.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _today_has_result(date_str, conn_id):
    if not os.path.exists(RESULTS_PATH):
        return False
    with open(RESULTS_PATH, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["date"] == date_str and row["conn_id"] == conn_id:
                return True
    return False


def _write_result(row):
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(RESULTS_PATH)
    with open(RESULTS_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULTS_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def finalize(cfg, conn, state, date_str, polls_ok, polls_fail):
    buf = int(cfg["transfer_buffer_min"])
    f_route = conn["feeder"]["route"]
    c_route = conn["connector"]["route"]
    f_arr = conn["feeder"]["sched_arrival"]
    c_dep = conn["connector"]["sched_departure"]
    nxt = conn["connector"].get("next_sched_departure")

    fs = state.get("feeder")
    cs = state.get("connector")
    notes = []

    def eff(hhmm, delay_s):
        base = dt.datetime.strptime(date_str + " " + hhmm, "%Y-%m-%d %H:%M")
        return base + dt.timedelta(seconds=delay_s or 0)

    # --- verdikt bez dat -------------------------------------------------- #
    if fs is None and cs is None:
        if polls_ok == 0:
            verdict = "NO_DATA"
            notes.append("zadny uspesny dotaz na API")
        else:
            verdict = "NO_SERVICE"
            notes.append("linky se v tabuli neobjevily (svatek / vyluka / nedojezd)")
        row = _blank_row(conn, date_str, buf, verdict, "; ".join(notes), polls_ok, polls_fail)
        row.update(feeder_route=f_route, feeder_sched_arr=f_arr, conn_route=c_route,
                   conn_sched_dep=c_dep, next_conn_sched_dep=nxt or "")
        _write_result(row)
        _log(f"{conn['id']}: {verdict} ({notes[0]})")
        return

    f_delay = fs["delay_s"] if fs else None
    c_delay = cs["delay_s"] if cs else None
    f_canceled = bool(fs and fs["canceled"])
    c_canceled = bool(cs and cs["canceled"])
    f_rt = bool(fs and fs["rt_available"])
    c_rt = bool(cs and cs["rt_available"])

    feeder_eff = eff(f_arr, f_delay)
    conn_eff = eff(c_dep, c_delay)
    margin_s = int((conn_eff - feeder_eff).total_seconds())

    if f_canceled:
        verdict, transfer_ok = "FEEDER_CANCELED", False
        notes.append(f"linka {f_route} zrusena")
    elif c_canceled:
        verdict, transfer_ok = "CONNECTOR_CANCELED", False
        notes.append(f"linka {c_route} zrusena")
    elif not f_rt and not c_rt:
        verdict, transfer_ok = "UNKNOWN_RT", None
        notes.append("realtime zpozdeni nebylo k dispozici - nelze rozhodnout")
    else:
        transfer_ok = margin_s >= buf * 60
        verdict = "OK" if transfer_ok else "MISSED"
        if not f_rt:
            notes.append(f"{f_route} bez realtime - bran jako vcas")
        if not c_rt:
            notes.append(f"{c_route} bez realtime - bran jako vcas")

    wait_if_missed = ""
    if nxt:
        wait_if_missed = hhmm_to_minutes(nxt) - hhmm_to_minutes(c_dep)

    row = {
        "date": date_str, "conn_id": conn["id"], "label": conn.get("label", ""),
        "weekday": WEEKDAYS_CS[dt.datetime.strptime(date_str, "%Y-%m-%d").weekday()],
        "feeder_route": f_route, "feeder_sched_arr": f_arr,
        "feeder_delay_s": "" if f_delay is None else f_delay,
        "feeder_eff_arr": feeder_eff.strftime("%H:%M:%S"),
        "feeder_canceled": int(f_canceled), "feeder_rt_available": int(f_rt),
        "feeder_seen_count": state.get("feeder_seen", 0),
        "conn_route": c_route, "conn_sched_dep": c_dep,
        "conn_delay_s": "" if c_delay is None else c_delay,
        "conn_eff_dep": conn_eff.strftime("%H:%M:%S"),
        "conn_canceled": int(c_canceled), "conn_rt_available": int(c_rt),
        "buffer_min": buf, "margin_s": margin_s,
        "transfer_ok": "" if transfer_ok is None else int(transfer_ok),
        "verdict": verdict,
        "next_conn_sched_dep": nxt or "",
        "wait_if_missed_min": wait_if_missed,
        "polls_ok": polls_ok, "polls_fail": polls_fail,
        "notes": "; ".join(notes),
    }
    _write_result(row)
    _log(f"{conn['id']}: {verdict}  margin={margin_s}s  "
         f"{f_route} +{(f_delay or 0)//60}m / {c_route} +{(c_delay or 0)//60}m")


def _blank_row(conn, date_str, buf, verdict, notes, polls_ok, polls_fail):
    row = {k: "" for k in RESULTS_FIELDS}
    row.update(
        date=date_str, conn_id=conn["id"], label=conn.get("label", ""),
        weekday=WEEKDAYS_CS[dt.datetime.strptime(date_str, "%Y-%m-%d").weekday()],
        buffer_min=buf, verdict=verdict, transfer_ok="", notes=notes,
        polls_ok=polls_ok, polls_fail=polls_fail,
    )
    return row


def cmd_collect(cfg, conns, once=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    now = prague_now()
    date_str = now.strftime("%Y-%m-%d")

    if not once and now.weekday() >= 5:
        _log(f"Dnes je {WEEKDAYS_CS[now.weekday()]} - vikend, nesbiram.")
        return

    cur_min_now = now.hour * 60 + now.minute
    active = []
    for c in conns:
        if _today_has_result(date_str, c["id"]) and not once:
            _log(f"{c['id']}: vysledek pro {date_str} uz existuje, preskakuji.")
            continue
        if not once and cur_min_now > hhmm_to_minutes(c["window"]["end"]):
            _log(f"{c['id']}: okno ({c['window']['start']}-{c['window']['end']}) uz probehlo, "
                 f"preskakuji (PC nejspis spalo).")
            continue
        active.append({
            "conn": c,
            "start": hhmm_to_minutes(c["window"]["start"]),
            "end": hhmm_to_minutes(c["window"]["end"]),
            "state": {"feeder": None, "connector": None, "feeder_seen": 0, "connector_seen": 0},
            "final": False,
        })
    if not active:
        _log("Neni co sbirat.")
        return

    last_end = max(a["end"] for a in active)
    first_start = min(a["start"] for a in active)
    polls_ok = polls_fail = 0

    if once:
        _poll_once(cfg, active, date_str, verbose=True)
        return

    # cekej na start prvniho okna
    cur_min = now.hour * 60 + now.minute
    if cur_min < first_start:
        wait_s = (first_start - cur_min) * 60 - now.second
        _log(f"Cekam {wait_s // 60} min do zacatku okna ({active[0]['conn']['window']['start']}).")
        time.sleep(max(0, wait_s))

    _log(f"Start sberu, {len(active)} spoju, do {last_end // 60:02d}:{last_end % 60:02d}.")
    while True:
        now = prague_now()
        cur_min = now.hour * 60 + now.minute
        try:
            board = fetch_board(cfg)
            polls_ok += 1
            _process_board(board, active, date_str, cur_min)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            polls_fail += 1
            _log(f"Dotaz na API selhal: {e!r}")

        for a in active:
            if not a["final"] and cur_min >= a["end"]:
                finalize(cfg, a["conn"], a["state"], date_str, polls_ok, polls_fail)
                a["final"] = True

        if all(a["final"] for a in active) or cur_min > last_end + 2:
            for a in active:
                if not a["final"]:
                    finalize(cfg, a["conn"], a["state"], date_str, polls_ok, polls_fail)
            _log("Hotovo.")
            return
        time.sleep(cfg["poll_interval_s"])


def cmd_snapshot(cfg, conns, duration_min=6, poll_s=45):
    """Jeden 'snimek' - kratke polovani kolem casu prestupu. Pro cloud/cron.

    Sam si najde spoj(e), jejichz cas prestupu spada do 'ted', poll-uje ~6 minut
    a zapise vysledek. Nic nedela, kdyz zadny spoj neni na rade nebo uz ma vysledek.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    now = prague_now()
    date_str = now.strftime("%Y-%m-%d")
    cur_min = now.hour * 60 + now.minute

    if now.weekday() >= 5:
        _log(f"snapshot: {WEEKDAYS_CS[now.weekday()]} - vikend, koncim.")
        return

    targets = []
    for c in conns:
        arr = hhmm_to_minutes(c["feeder"]["sched_arrival"])
        dep = hhmm_to_minutes(c["connector"]["sched_departure"])
        if not (arr - 10 <= cur_min <= dep + 15):
            continue
        if _today_has_result(date_str, c["id"]):
            _log(f"snapshot {c['id']}: vysledek pro {date_str} uz existuje, preskakuji.")
            continue
        targets.append({"conn": c, "final": False,
                        "state": {"feeder": None, "connector": None,
                                  "feeder_seen": 0, "connector_seen": 0,
                                  "_feeder_worst": None}})
    if not targets:
        _log(f"snapshot: v {now:%H:%M} neni zadny spoj na rade, koncim.")
        return

    _log(f"snapshot: {now:%H:%M} sleduji {[t['conn']['id'] for t in targets]} po {duration_min} min.")
    end_ts = time.time() + duration_min * 60
    polls_ok = polls_fail = 0
    while True:
        try:
            board = fetch_board(cfg)
            polls_ok += 1
            _process_snapshot(board, targets, date_str)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            polls_fail += 1
            _log(f"snapshot: dotaz na API selhal: {e!r}")
        if time.time() >= end_ts:
            break
        time.sleep(poll_s)

    for t in targets:
        finalize(cfg, t["conn"], t["state"], date_str, polls_ok, polls_fail)
    _log("snapshot: hotovo.")


def _process_snapshot(board, targets, date_str):
    for t in targets:
        c, st = t["conn"], t["state"]
        raw = {"ts": prague_now().isoformat(timespec="seconds"), "date": date_str,
               "conn_id": c["id"], "kind": "snapshot"}
        fd = find_departure(board, c["feeder"]["route"], c["feeder"]["sched_arrival"], "arrival")
        if fd:
            s = sample_from_departure(fd, "arrival")
            st["feeder_seen"] += 1
            raw["feeder"] = s
            if s["canceled"]:
                st["feeder"] = s
            elif s["rt_available"]:
                # u pridavatele (336) si drz nejhorsi (nejvetsi) zpozdeni - to je
                # nejbliz skutecnemu prijezdu, jak se blizi ke Zlicinu
                w = st.get("_feeder_worst")
                if w is None or (s["delay_s"] or 0) >= (w["delay_s"] or 0):
                    st["_feeder_worst"] = s
                    st["feeder"] = s
        cd = find_departure(board, c["connector"]["route"], c["connector"]["sched_departure"], "departure")
        if cd:
            s = sample_from_departure(cd, "departure")
            st["connector_seen"] += 1
            raw["connector"] = s
            if s["rt_available"] or s["canceled"]:
                st["connector"] = s   # u navazujiciho (384) ber posledni = nejbliz odjezdu
        if "feeder" in raw or "connector" in raw:
            _append_jsonl(SAMPLES_PATH, raw)


def _process_board(board, active, date_str, cur_min):
    for a in active:
        if a["final"] or not (a["start"] <= cur_min <= a["end"]):
            continue
        c = a["conn"]
        st = a["state"]
        raw = {"ts": prague_now().isoformat(timespec="seconds"), "date": date_str,
               "conn_id": c["id"]}
        fd = find_departure(board, c["feeder"]["route"], c["feeder"]["sched_arrival"], "arrival")
        if fd:
            s = sample_from_departure(fd, "arrival")
            st["feeder_seen"] += 1
            raw["feeder"] = s
            if s["rt_available"] or s["canceled"]:
                st["feeder"] = s
        cd = find_departure(board, c["connector"]["route"], c["connector"]["sched_departure"], "departure")
        if cd:
            s = sample_from_departure(cd, "departure")
            st["connector_seen"] += 1
            raw["connector"] = s
            if s["rt_available"] or s["canceled"]:
                st["connector"] = s
        if "feeder" in raw or "connector" in raw:
            _append_jsonl(SAMPLES_PATH, raw)


def _poll_once(cfg, active, date_str, verbose=False):
    board = fetch_board(cfg)
    print(f"Tabule uzlu {cfg['node_name']} ({cfg['node_asw_id']}) - {len(board.get('departures', []))} zaznamu")
    for a in active:
        c = a["conn"]
        print(f"\n=== {c['id']}  {c.get('label','')} ===")
        for role, route, hhmm, kind in (
            ("feeder", c["feeder"]["route"], c["feeder"]["sched_arrival"], "arrival"),
            ("connector", c["connector"]["route"], c["connector"]["sched_departure"], "departure"),
        ):
            dep = find_departure(board, route, hhmm, kind)
            if not dep:
                print(f"  {role:9s} linka {route} {kind} ~{hhmm}: NENALEZENO v tabuli")
                continue
            s = sample_from_departure(dep, kind)
            d = s["delay_s"]
            dtxt = "n/a" if d is None else f"{d:+d}s ({d//60:+d} min)"
            print(f"  {role:9s} linka {route} {kind} plan {hhmm}: zpozdeni {dtxt}"
                  f"  rt={s['rt_available']} canceled={s['canceled']} trip={s['trip_id']}")


# --------------------------------------------------------------------------- #
# Statistika
# --------------------------------------------------------------------------- #
def _read_results():
    if not os.path.exists(RESULTS_PATH):
        return []
    with open(RESULTS_PATH, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _stats_for(rows):
    decisive = [r for r in rows if r["verdict"] in ("OK", "MISSED", "FEEDER_CANCELED", "CONNECTOR_CANCELED")]
    ok = [r for r in decisive if r["verdict"] == "OK"]
    delays = [int(r["feeder_delay_s"]) for r in rows
              if r.get("feeder_rt_available") == "1" and r["feeder_delay_s"] not in ("", None)]
    out = {
        "days_total": len(rows),
        "decisive": len(decisive),
        "ok": len(ok),
        "missed": sum(1 for r in decisive if r["verdict"] == "MISSED"),
        "feeder_canceled": sum(1 for r in decisive if r["verdict"] == "FEEDER_CANCELED"),
        "connector_canceled": sum(1 for r in decisive if r["verdict"] == "CONNECTOR_CANCELED"),
        "no_data": sum(1 for r in rows if r["verdict"] in ("NO_DATA", "UNKNOWN_RT")),
        "no_service": sum(1 for r in rows if r["verdict"] == "NO_SERVICE"),
        "success_rate": (len(ok) / len(decisive)) if decisive else None,
    }
    if delays:
        out["delay_mean_min"] = round(statistics.mean(delays) / 60, 1)
        out["delay_median_min"] = round(statistics.median(delays) / 60, 1)
        out["delay_p90_min"] = round(sorted(delays)[max(0, int(len(delays) * 0.9) - 1)] / 60, 1)
        out["delay_max_min"] = round(max(delays) / 60, 1)
    return out


def cmd_report(html_path=None):
    rows = _read_results()
    if not rows:
        print("Zatim zadna data v data/results.csv.")
        return
    conns = load_connections()
    by_id = {}
    for r in rows:
        by_id.setdefault(r["conn_id"], []).append(r)

    def fmt(s):
        if s["success_rate"] is None:
            return "  uspesnost: zatim nelze spocitat (chybi rozhodnutelne dny)"
        return (f"  uspesnost prestupu: {s['success_rate']*100:.0f} %  "
                f"({s['ok']}/{s['decisive']} rozhodnutelnych dni)")

    print(f"PID transfer monitor - statistika ({rows[0]['date']} .. {rows[-1]['date']})\n")
    overall = _stats_for(rows)
    print("CELKEM")
    print(fmt(overall))
    if "delay_mean_min" in overall:
        print(f"  zpozdeni prijezdu 336: prumer {overall['delay_mean_min']} min, "
              f"median {overall['delay_median_min']} min, p90 {overall['delay_p90_min']} min, "
              f"max {overall['delay_max_min']} min")
    print(f"  dni bez dat/rozhodnuti: {overall['no_data']}, mimo provoz: {overall['no_service']}")

    for cid, crows in by_id.items():
        label = next((c.get("label", "") for c in conns if c["id"] == cid), "")
        s = _stats_for(crows)
        print(f"\n{cid}  {label}")
        print(fmt(s))
        print(f"  OK {s['ok']} | zmeskano {s['missed']} | 336 zrus. {s['feeder_canceled']} "
              f"| 384 zrus. {s['connector_canceled']} | bez dat {s['no_data']}")
        if "delay_mean_min" in s:
            print(f"  zpozdeni 336: prumer {s['delay_mean_min']} / median {s['delay_median_min']} "
                  f"/ p90 {s['delay_p90_min']} / max {s['delay_max_min']} min")

    print("\nPOSLEDNICH 14 ZAZNAMU")
    print(f"{'datum':11s} {'den':3s} {'spoj':5s} {'336 zpozd':>10s} {'384 zpozd':>10s} "
          f"{'rezerva':>8s}  verdikt")
    for r in rows[-14:]:
        fd = r["feeder_delay_s"]
        cd = r["conn_delay_s"]
        fd_t = "-" if fd == "" else f"{int(fd)//60:+d} min"
        cd_t = "-" if cd == "" else f"{int(cd)//60:+d} min"
        mg = r["margin_s"]
        mg_t = "-" if mg == "" else f"{int(mg)//60:+d} min"
        print(f"{r['date']:11s} {r['weekday']:3s} {r['conn_id']:5s} {fd_t:>10s} {cd_t:>10s} "
              f"{mg_t:>8s}  {r['verdict']}")

    if html_path:
        _write_html(html_path, rows, conns, overall, by_id)
        print(f"\nHTML report ulozen: {html_path}")


def _write_html(path, rows, conns, overall, by_id):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def card(title, s):
        rate = "-" if s["success_rate"] is None else f"{s['success_rate']*100:.0f}&nbsp;%"
        dl = ""
        if "delay_mean_min" in s:
            dl = (f"<div class='sub'>336 zpozdeni: prumer {s['delay_mean_min']} / "
                  f"median {s['delay_median_min']} / p90 {s['delay_p90_min']} / max {s['delay_max_min']} min</div>")
        return f"""<div class="card">
  <h2>{esc(title)}</h2>
  <div class="rate">{rate}</div>
  <div class="sub">{s['ok']} OK / {s['missed']} zmeskano / {s['decisive']} rozhodnutelnych dni</div>
  <div class="sub">336 zrus. {s['feeder_canceled']} &middot; 384 zrus. {s['connector_canceled']} &middot; bez dat {s['no_data']} &middot; mimo provoz {s['no_service']}</div>
  {dl}
</div>"""

    trs = []
    for r in reversed(rows):
        cls = {"OK": "ok", "MISSED": "bad", "FEEDER_CANCELED": "bad",
               "CONNECTOR_CANCELED": "bad"}.get(r["verdict"], "meh")
        fd = "-" if r["feeder_delay_s"] == "" else f"{int(r['feeder_delay_s'])//60:+d} min"
        cd = "-" if r["conn_delay_s"] == "" else f"{int(r['conn_delay_s'])//60:+d} min"
        mg = "-" if r["margin_s"] == "" else f"{int(r['margin_s'])//60:+d} min"
        trs.append(f"<tr class='{cls}'><td>{esc(r['date'])}</td><td>{esc(r['weekday'])}</td>"
                   f"<td>{esc(r['conn_id'])}</td><td>{esc(r['feeder_eff_arr'])}</td>"
                   f"<td>{fd}</td><td>{esc(r['conn_eff_dep'])}</td><td>{cd}</td>"
                   f"<td>{mg}</td><td>{esc(r['verdict'])}</td><td>{esc(r['notes'])}</td></tr>")

    cards = card("CELKEM", overall)
    for cid, crows in by_id.items():
        label = next((c.get("label", "") for c in conns if c["id"] == cid), cid)
        cards += card(label or cid, _stats_for(crows))

    html = f"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PID prestup 336 &rarr; 384 Zlicin</title>
<style>
 body{{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1a1a1a}}
 h1{{font-size:20px;margin:0 0 4px}}
 .meta{{color:#666;margin-bottom:20px}}
 .cards{{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:24px}}
 .card{{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:16px 20px;min-width:220px;flex:1}}
 .card h2{{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#555;margin:0 0 8px}}
 .rate{{font-size:34px;font-weight:700}}
 .sub{{color:#666;font-size:12px;margin-top:4px}}
 table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e2e5e9;border-radius:10px;overflow:hidden}}
 th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #eef0f2;font-size:13px}}
 th{{background:#f0f2f4;font-weight:600}}
 tr.ok td:nth-child(9){{color:#0a7d28;font-weight:600}}
 tr.bad td:nth-child(9){{color:#c02626;font-weight:600}}
 tr.meh td:nth-child(9){{color:#8a6d1a}}
</style></head><body>
<h1>PID prestup 336 &rarr; 384 na Zlicine</h1>
<div class="meta">Aktualizovano {esc(prague_now().strftime('%d.%m.%Y %H:%M'))} &middot;
 obdobi {esc(rows[0]['date'])} &ndash; {esc(rows[-1]['date'])} &middot;
 rezerva na prestup {esc(rows[-1]['buffer_min'])} min</div>
<div class="cards">{cards}</div>
<table><thead><tr><th>datum</th><th>den</th><th>spoj</th><th>336 prijezd</th><th>zpozd.</th>
 <th>384 odjezd</th><th>zpozd.</th><th>rezerva</th><th>verdikt</th><th>pozn.</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------- #
# Kontrola jizdniho radu proti GTFS
# --------------------------------------------------------------------------- #
def cmd_verify_timetable(gtfs_dir):
    import csv as _csv
    cfg = None
    try:
        cfg = load_config()
        node = str(cfg["node_asw_id"])
    except SystemExit:
        node = "1141"
    stops_path = os.path.join(gtfs_dir, "stops.txt")
    st_path = os.path.join(gtfs_dir, "stop_times.txt")
    trips_path = os.path.join(gtfs_dir, "trips.txt")
    if not all(os.path.exists(p) for p in (stops_path, st_path, trips_path)):
        sys.exit(f"V {gtfs_dir} nejsou soubory GTFS (stops.txt, stop_times.txt, trips.txt).")

    node_stops = set()
    with open(stops_path, "r", encoding="utf-8", newline="") as f:
        for row in _csv.DictReader(f):
            if row.get("asw_node_id") == node:
                node_stops.add(row["stop_id"])
    print(f"Uzel {node}: {len(node_stops)} zastavek")

    conns = load_connections()
    routes = {c["feeder"]["route"] for c in conns} | {c["connector"]["route"] for c in conns}
    want_trips = {}
    with open(trips_path, "r", encoding="utf-8", newline="") as f:
        for row in _csv.DictReader(f):
            rn = row["route_id"].lstrip("L")
            if rn in routes:
                want_trips[row["trip_id"]] = rn

    found = []
    with open(st_path, "r", encoding="utf-8", newline="") as f:
        for row in _csv.DictReader(f):
            if row["trip_id"] in want_trips and row["stop_id"] in node_stops:
                found.append((want_trips[row["trip_id"]], row["arrival_time"],
                              row["departure_time"], row["stop_id"]))
    found.sort(key=lambda x: x[1])
    print(f"\nVsechny zastaveni linek {sorted(routes)} v uzlu {node} (podle GTFS):")
    for rn, arr, dep, sid in found:
        print(f"  linka {rn:4s} prijezd {arr}  odjezd {dep}  ({sid})")
    print("\nZkontroluj, ze casy v connections.json (sched_arrival / sched_departure) "
          "odpovidaji. GTFS se meni tydne.")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="PID transfer monitor 336->384 Zlicin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    pc = sub.add_parser("collect")
    pc.add_argument("--once", action="store_true", help="jen jeden dotaz a vypis")
    ps = sub.add_parser("snapshot", help="kratke polovani kolem casu prestupu (cloud/cron)")
    ps.add_argument("--minutes", type=int, default=6, help="jak dlouho pollovat (vychozi 6)")
    pr = sub.add_parser("report")
    pr.add_argument("--html", metavar="PATH", help="ulozit HTML report")
    pv = sub.add_parser("verify-timetable")
    pv.add_argument("gtfs_dir", help="slozka s rozbalenym GTFS (Jizdni rady)")
    args = ap.parse_args()

    if args.cmd == "selftest":
        cfg = load_config()
        conns = load_connections()
        active = [{"conn": c} for c in conns]
        _poll_once(cfg, active, prague_now().strftime("%Y-%m-%d"), verbose=True)
    elif args.cmd == "collect":
        cfg = load_config()
        conns = load_connections()
        cmd_collect(cfg, conns, once=args.once)
    elif args.cmd == "snapshot":
        cfg = load_config()
        conns = load_connections()
        cmd_snapshot(cfg, conns, duration_min=args.minutes)
    elif args.cmd == "report":
        cmd_report(html_path=args.html)
    elif args.cmd == "verify-timetable":
        cmd_verify_timetable(args.gtfs_dir)


if __name__ == "__main__":
    main()

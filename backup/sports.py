# Kansas City Royals (MLB StatsAPI) and Chiefs (ESPN core API).
# Both are free, keyless and serve plain HTTP - mbedTLS will not fit in RAM.
import time, net

MLB_TEAM = 118          # Kansas City Royals
NFL_TEAM = 12           # Kansas City Chiefs
NFL = "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl"

def _dash(ep, days=0):
    t = time.localtime(ep + days * 86400)
    return "%04d-%02d-%02d" % (t[0], t[1], t[2])

def _ymd(ep, days=0):
    t = time.localtime(ep + days * 86400)
    return "%04d%02d%02d" % (t[0], t[1], t[2])

def _clock(iso, tz_off):
    """'2026-09-04T23:10:00Z' -> ('SEP 4', '6:10 PM') in local time."""
    try:
        y, mo, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
        h, mi = int(iso[11:13]), int(iso[14:16])
        t = time.localtime(time.mktime((y, mo, d, h, mi, 0, 0, 0)) + tz_off)
        mons = ("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC")
        h12 = t[3] % 12 or 12
        return "%s %d" % (mons[t[1]-1], t[2]), "%d:%02d %s" % (h12, t[4], "AM" if t[3] < 12 else "PM")
    except Exception:
        return "", ""

def _val(b, key):
    """Pull "key":"value" out of a raw json fragment without parsing it."""
    if b is None:
        return None
    if not isinstance(b, str):
        # MicroPython's socket read gives a bytearray, for which
        # isinstance(x, bytes) is False - convert unconditionally.
        try:
            b = bytes(b).decode("utf8", "replace")
        except Exception:
            return None
    m = '"%s":"' % key
    i = b.find(m)
    if i < 0:
        return None
    i += len(m)
    j = b.find('"', i)
    return b[i:j] if j > i else None

# ------------------------------------------------------------------ Royals
def royals(now_local, tz_off):
    url = ("http://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=%d"
           "&startDate=%s&endDate=%s&hydrate=linescore,team"
           "&fields=dates,date,games,gameDate,status,detailedState,abstractGameState,"
           "teams,away,home,team,abbreviation,score,linescore,currentInning,inningState"
           % (MLB_TEAM, _dash(now_local, -3), _dash(now_local, 7)))
    d = net.get_json(url)
    if not d:
        return None
    live = fin = nxt = None
    for day in d.get("dates", []):
        for g in day.get("games", []):
            t = g.get("teams", {})
            a, h = t.get("away", {}), t.get("home", {})
            ls = g.get("linescore") or {}
            st = g.get("status", {})
            state = st.get("abstractGameState")
            when, at = _clock(g.get("gameDate", ""), tz_off)
            rec = {"league": "ROYALS",
                   "away": a.get("team", {}).get("abbreviation", "?"),
                   "home": h.get("team", {}).get("abbreviation", "?"),
                   "as": a.get("score"), "hs": h.get("score"),
                   "when": when, "at": at}
            if state == "Live":
                inn = ls.get("currentInning")
                half = (ls.get("inningState") or "")[:3].upper()
                rec["status"] = ("%s %s" % (half, inn)) if inn else "LIVE"
                live = live or rec
            elif state == "Final":
                rec["status"] = "FINAL"
                fin = rec                       # dates ascend, so the last wins
            elif state == "Preview":
                rec["status"] = st.get("detailedState", "SCHEDULED").upper()
                nxt = nxt or rec
    return live or fin or nxt

# ------------------------------------------------------------------ Chiefs
def _season(ep):
    t = time.localtime(ep)
    return t[0] - 1 if t[1] <= 2 else t[0]      # Jan/Feb belong to last season

def _events(season, stype, lo, hi):
    u = "%s/seasons/%d/types/%d/teams/%d/events?limit=6&dates=%s-%s" % (
        NFL, season, stype, NFL_TEAM, lo, hi)
    d = net.get_json(u)
    return (d or {}).get("items") or []

def chiefs(now_local, tz_off):
    season = _season(now_local)
    items, recent = [], True
    for stype in (2, 1, 3):                     # regular, pre, post
        items = _events(season, stype, _ymd(now_local, -2), _ymd(now_local, 1))
        if items:
            break
    if not items:                               # nothing recent - look ahead
        recent = False
        for stype in (2, 3, 1):
            items = _events(season, stype, _ymd(now_local), _ymd(now_local, 45))
            if items:
                break
    if not items:
        return None

    # a "recent" window wants its LAST entry (most recently played); a
    # look-ahead window wants its FIRST (the soonest upcoming game).
    ref = items[-1]["$ref"] if recent else items[0]["$ref"]
    ev = ref.split("/events/")[1].split("?")[0]

    head = net.get_head("%s/events/%s" % (NFL, ev), 600)
    short = _val(head, "shortName") or _val(head, "name") or ""
    when, at = _clock(_val(head, "date") or "", tz_off)

    away = home = "?"
    if " @ " in short:
        away, home = short.split(" @ ", 1)
    elif " AT " in short.upper():
        parts = short.upper().split(" AT ", 1); away, home = parts[0], parts[1]

    st = net.get_json("%s/events/%s/competitions/%s/status" % (NFL, ev, ev)) or {}
    ty = st.get("type") or {}
    state = ty.get("state", "pre")
    label = (ty.get("shortDetail") or ty.get("description") or "").upper()

    rec = {"league": "CHIEFS", "away": away, "home": home,
           "as": None, "hs": None, "when": when, "at": at,
           "status": "FINAL" if state == "post" else ("LIVE" if state == "in" else "")}

    if state in ("in", "post"):
        comps = net.get_json("%s/events/%s/competitions/%s/competitors" % (NFL, ev, ev)) or {}
        for it in comps.get("items", []):
            tid = it["$ref"].split("/competitors/")[1].split("?")[0]
            sc = net.get_json("%s/events/%s/competitions/%s/competitors/%s/score"
                              % (NFL, ev, ev, tid)) or {}
            v = sc.get("displayValue")
            if it.get("homeAway") == "home":
                rec["hs"] = v
            else:
                rec["as"] = v
        if state == "in":
            rec["status"] = label[:12] or "LIVE"
    return rec

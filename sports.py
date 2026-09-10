# Scores for the info frame: Royals (MLB StatsAPI) plus Chiefs and both KU
# teams (ESPN core API). All are free, keyless and serve plain HTTP, which
# keeps a nine-request refresh off the TLS path entirely.
#
# Every feed returns {"last": rec, "next": rec} so one fetch drives both of a
# team's tiles. "last" is the most recent completed game, "next" is the game
# in progress if there is one, otherwise the next scheduled game. Either may
# be None (start of season, off-season). The whole feed is None only when the
# network failed, which is what tells the caller to keep showing stale data.
import time, net

MLB_TEAM = 118          # Kansas City Royals
LOOKBACK = 10           # days of history to search for the last game
LOOKAHEAD = 45          # days ahead to search for the next one

# The schedule is fetched as two windows, one back and one forward, which
# makes the last/next split explicit and keeps each response near a kilobyte.
# Bodies run about 270 bytes per day, and five days either way always spans a
# last game and a next one while the Royals are playing. Four days keeps the
# body near 1.6 KB, comfortably inside net.BODY_MAX even with a doubleheader.
MLB_BACK, MLB_AHEAD = 4, 4

ESPN = "http://sports.core.api.espn.com/v2/sports"
# (base url, espn team id, label for logs, season-year rule)
NFL = (ESPN + "/football/leagues/nfl", 12, "CHIEFS", "fall")
CFB = (ESPN + "/football/leagues/college-football", 2305, "KU FOOTBALL", "fall")
CBB = (ESPN + "/basketball/leagues/mens-college-basketball", 2305, "KU BASKETBALL", "winter")

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

def _val(buf, end, key, start=0):
    """String value of "key" between start and end in net.body(), or None.

    Scans the buffer in place. Turning the response into a string, or into
    dictionaries via json, allocates memory proportional to its size, and any
    document over about 1.5 KB pushes MicroPython past its initial heap. It
    grows the heap out of the ESP-IDF heap and never gives it back, which is
    where mbedTLS gets the memory for the markets handshake (see markets.py).
    Only the short values themselves become strings here.
    """
    if not end:
        return None
    m = ('"%s":"' % key).encode()
    i = buf.find(m, start, end)
    if i < 0:
        return None
    i += len(m)
    j = buf.find(b'"', i, end)
    if j <= i:
        return None
    try:
        return bytes(buf[i:j]).decode("utf8", "replace")
    except Exception:
        return None

def _int(buf, end, key, start=0):
    """Integer value of "key" (an unquoted number) in the same range."""
    if not end:
        return None
    m = ('"%s":' % key).encode()
    i = buf.find(m, start, end)
    if i < 0:
        return None
    i += len(m)
    j = i
    while j < end and 48 <= buf[j] <= 57:
        j += 1
    if j == i:
        return None
    try:
        return int(bytes(buf[i:j]))
    except Exception:
        return None

# ------------------------------------------------------------------ Royals
MLB_URL = ("http://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=%d"
           "&startDate=%s&endDate=%s&hydrate=linescore,team"
           "&fields=dates,date,games,gameDate,status,detailedState,abstractGameState,"
           "teams,away,home,team,abbreviation,score,linescore,currentInning,inningState")

def _mlb_game(buf, s, e, tz_off):
    """One game, read straight out of the buffer between offsets s and e."""
    state = _val(buf, e, "abstractGameState", s)
    when, at = _clock(_val(buf, e, "gameDate", s) or "", tz_off)
    # The away block precedes the home block, but locate both rather than
    # trusting the order, and read each side's fields only within its own span.
    a_pos = buf.find(b'"away"', s, e)
    h_pos = buf.find(b'"home"', s, e)
    if a_pos < 0 or h_pos < 0:
        return None
    if a_pos < h_pos:
        a_end, h_end = h_pos, e
    else:
        a_end, h_end = e, a_pos
    rec = {"league": "ROYALS",
           "away": _val(buf, a_end, "abbreviation", a_pos) or "?",
           "home": _val(buf, h_end, "abbreviation", h_pos) or "?",
           "as": _int(buf, a_end, "score", a_pos),
           "hs": _int(buf, h_end, "score", h_pos),
           "when": when, "at": at,
           "live": state == "Live", "final": state == "Final"}
    if state == "Live":
        inn = _int(buf, e, "currentInning", s)
        half = (_val(buf, e, "inningState", s) or "")[:3].upper()
        rec["status"] = ("%s %s" % (half, inn)) if inn else "LIVE"
    elif state == "Final":
        rec["status"] = "FINAL"
    else:
        rec["status"] = (_val(buf, e, "detailedState", s) or "SCHEDULED").upper()
    return rec

def _mlb(start, end, tz_off):
    """Games in a date range, oldest first. None if the request failed."""
    n = net.get_head(MLB_URL % (MLB_TEAM, start, end), net.BODY_MAX)
    if not n:
        return None
    buf = net.body()
    out = []
    pos = 0
    while True:
        i = buf.find(b'"gameDate":"', pos, n)
        if i < 0:
            break
        j = buf.find(b'"gameDate":"', i + 12, n)   # this game ends where the next starts
        stop = n if j < 0 else j
        rec = _mlb_game(buf, i, stop, tz_off)
        if rec:
            out.append(rec)
        if j < 0:
            break
        pos = j
    return out

def royals(now_local, tz_off):
    back = _mlb(_dash(now_local, -MLB_BACK), _dash(now_local), tz_off)
    fwd = _mlb(_dash(now_local), _dash(now_local, MLB_AHEAD), tz_off)
    if back is None and fwd is None:
        return None
    last = live = nxt = None
    for rec in (back or []):
        if rec["final"]:
            last = rec                          # dates ascend, so the last wins
    # Today appears in both windows; take the soonest unplayed game either way.
    for rec in (fwd or []) + (back or []):
        if rec["live"]:
            live = live or rec
        elif not rec["final"]:
            nxt = nxt or rec
    return {"last": last, "next": live or nxt}

# ------------------------------------------------------------------ ESPN teams
def _season(ep, kind):
    t = time.localtime(ep)
    if kind == "winter":
        # ESPN names a basketball season by the year it ends in, so November
        # 2025 games live under season 2026.
        return t[0] + 1 if t[1] >= 7 else t[0]
    return t[0] - 1 if t[1] <= 2 else t[0]      # Jan/Feb belong to last season

def _window(base, season, team, lo, hi):
    """Event ids in a date range, or None if every request failed.

    An empty list is a real answer (no games that week); None is not, and the
    caller must not mistake a dead network for an empty schedule.
    """
    failed = True
    for stype in (2, 3, 1):                     # regular, post, pre
        d = net.get_json("%s/seasons/%d/types/%d/teams/%d/events?limit=6&dates=%s-%s"
                         % (base, season, stype, team, lo, hi))
        if d is None:
            continue
        failed = False
        items = d.get("items") or []
        if items:
            return [it["$ref"].split("/events/")[1].split("?")[0] for it in items]
    return None if failed else []

def _teams(short):
    """'MIZ @ KU' -> ('MIZ', 'KU'). ESPN always lists the away side first, but
    the separator varies: '@' normally, 'VS' for neutral-site games (which the
    college schedules are full of), and 'at' in the long-form name we fall
    back to when shortName is missing."""
    up = short.upper()
    for sep in (" @ ", " VS ", " AT "):
        if sep in up:
            a, h = up.split(sep, 1)
            return a.strip(), h.strip()
    return "?", "?"

def _scores(base, ev, rec):
    """Fill in the two scores.

    The competitors list is fetched one competitor at a time, reading only the
    front of each page. The full document runs past 3 KB, and reading more
    than about 1.5 KB from a socket costs 59 KB of ESP-IDF heap permanently -
    the memory mbedTLS needs for the markets handshake (see markets.py). Both
    the team id and the side sit within the first 300 bytes of a page, so 400
    is plenty and nothing large is ever read.
    """
    for page in (1, 2):
        n = net.get_head("%s/events/%s/competitions/%s/competitors"
                         "?limit=1&page=%d" % (base, ev, ev, page), 400)
        if not n:
            return
        buf = net.body()
        tid = _val(buf, n, "id")
        side = _val(buf, n, "homeAway")
        if not tid or not side:
            return
        sc = net.get_json("%s/events/%s/competitions/%s/competitors/%s/score"
                          % (base, ev, ev, tid)) or {}
        v = sc.get("displayValue")
        if side == "home":
            rec["hs"] = v
        else:
            rec["as"] = v

def _record(base, ev, league, tz_off, seen):
    """One game. None if any part of it could not be fetched - a half-built
    record would show up on the frame as '? AT ?', which is worse than the
    previous tile left in place."""
    if ev in seen:
        return seen[ev]
    # Read everything wanted from the event document before the next request:
    # net.body() is one shared buffer and the status fetch below overwrites it.
    n = net.get_head("%s/events/%s" % (base, ev), 600)
    buf = net.body()
    short = _val(buf, n, "shortName") or _val(buf, n, "name")
    iso = _val(buf, n, "date")
    if not short:
        return None
    st = net.get_json("%s/events/%s/competitions/%s/status" % (base, ev, ev))
    if not st:
        return None
    when, at = _clock(iso or "", tz_off)
    away, home = _teams(short)
    ty = st.get("type") or {}
    state = ty.get("state", "pre")
    label = (ty.get("shortDetail") or ty.get("description") or "").upper()
    if state == "pre" and "TBD" in label:
        # ESPN parks an unannounced kickoff at midnight UTC, which would
        # otherwise render as a confident and wrong "12:00 AM".
        at = "TBD"
    rec = {"league": league, "away": away, "home": home,
           "as": None, "hs": None, "when": when, "at": at,
           "live": state == "in", "final": state == "post",
           "status": "FINAL" if state == "post" else ("LIVE" if state == "in" else "")}
    if state in ("in", "post"):
        _scores(base, ev, rec)
        if state == "in":
            rec["status"] = label[:12] or "LIVE"
    seen[ev] = rec
    return rec

def _espn(cfg, now_local, tz_off):
    base, team, league, kind = cfg
    season = _season(now_local, kind)
    back = _window(base, season, team, _ymd(now_local, -LOOKBACK), _ymd(now_local))
    fwd  = _window(base, season, team, _ymd(now_local), _ymd(now_local, LOOKAHEAD))
    if back is None and fwd is None:
        return None
    seen = {}
    last = nxt = None
    if fwd:
        nxt = _record(base, fwd[0], league, tz_off, seen)
    if back:
        rec = _record(base, back[-1], league, tz_off, seen)
        if rec and rec["final"]:
            last = rec
        else:
            # Today's game shows up in the back window too. If it has not
            # finished it belongs on the next-game tile, and the last game is
            # the one before it.
            if rec and not nxt:
                nxt = rec
            if len(back) > 1:
                rec = _record(base, back[-2], league, tz_off, seen)
                if rec and rec["final"]:
                    last = rec
    return {"last": last, "next": nxt}

def chiefs(now_local, tz_off):
    return _espn(NFL, now_local, tz_off)

def ku_football(now_local, tz_off):
    return _espn(CFB, now_local, tz_off)

def ku_basketball(now_local, tz_off):
    return _espn(CBB, now_local, tz_off)

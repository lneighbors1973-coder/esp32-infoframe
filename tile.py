# Tiles for the info frame, designed for low vision (macular degeneration).
#
# Design rules applied throughout:
#   - Maximum luminance contrast: white/yellow on pure black. No greys, no
#     gradients - AMD degrades contrast sensitivity as well as acuity.
#   - Very large glyphs, emboldened (strokes thickened ~1.3x).
#   - One idea per tile. Clutter competes with a central scotoma.
#   - Colour is never the only cue; size, position and rules carry meaning too.
#   - Every tile leaves the bottom 24 px clear for the OFFLINE badge, so each
#     painter needs only one layout instead of a stale-data variant.
#
# DARK_MODE below is only the default. Which polarity suits a given person
# depends on their glare sensitivity, so the frame offers the choice at every
# boot and remembers it - see set_mode() and the chooser in frame.py.
import framebuf, gc

# STRIP is 8 rows (5 KB), far less than free memory alone would allow.
# mbedTLS needs a large CONTIGUOUS block for a handshake, and a big strip
# buffer splits the heap: at 48 rows markets.py's HTTPS fetch hung for 150 s,
# at 24 it failed instantly with ENOMEM, both with 75-80 KB still free. The
# cost is thirty blits per tile instead of five, which the early-out in text()
# keeps cheap because most elements fall outside any given strip.
W, H, STRIP = 320, 240, 8
BADGE_Y = H - 24
DARK_MODE = True

def _sw(c):
    """framebuf.RGB565 is little-endian here; the ILI9341 wants hi byte first."""
    return ((c & 0xFF) << 8) | (c >> 8)

BG = FG = ACCENT = 0

def set_mode(dark):
    """Switch the palette. The painters read these names at draw time, so
    rebinding them here is enough - nothing has to be redrawn or reallocated.

    Light mode drops the yellow: on white it is far too low-contrast to read.
    Nothing is lost, because no tile uses colour as its only cue - size,
    position and the rules carry the same meaning in both palettes.
    """
    global BG, FG, ACCENT, DARK_MODE
    DARK_MODE = bool(dark)
    if DARK_MODE:
        BG, FG, ACCENT = _sw(0x0000), _sw(0xFFFF), _sw(0xFFE0)
    else:
        BG, FG, ACCENT = _sw(0xFFFF), _sw(0x0000), _sw(0x0000)

set_mode(DARK_MODE)

WMO = {0:"CLEAR", 1:"CLEAR", 2:"CLOUDY", 3:"CLOUDY", 45:"FOG", 48:"FOG",
       51:"DRIZZLE", 53:"DRIZZLE", 55:"DRIZZLE", 56:"ICE", 57:"ICE",
       61:"RAIN", 63:"RAIN", 65:"RAIN", 66:"ICE", 67:"ICE",
       71:"SNOW", 73:"SNOW", 75:"SNOW", 77:"SNOW",
       80:"SHOWERS", 81:"SHOWERS", 82:"SHOWERS", 85:"SNOW", 86:"SNOW",
       95:"STORM", 96:"STORM", 99:"STORM"}

# 8-point compass only. 16-point names (SSW, ENE) are three characters and
# would push the wind line past 320 px, shrinking it to an unreadable size.
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

def compass(deg):
    """Meteorological convention: the direction the wind blows FROM."""
    try:
        return COMPASS[int((float(deg) + 22.5) // 45) % 8]
    except Exception:
        return ""

DAYS  = ("MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY")
SHORT = ("MON","TUE","WED","THU","FRI","SAT","SUN")
MONS  = ("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC")

# ------------------------------------------------------------------ drawing
def tw(s, scale):
    return len(s) * 8 * scale

def fit(s, maxw, cap):
    return 1 if not s else max(1, min(cap, maxw // (len(s) * 8)))

# One reusable glyph strip instead of a fresh bytearray per call. text() runs
# once per element per strip pass - hundreds of times per tile - and those
# short-lived allocations shattered the heap into pieces too small for a 2.7 KB
# socket read. MicroPython answered that by growing its GC heap, which on ESP32
# means taking a ~54 KB segment from the ESP-IDF heap and never giving it back,
# leaving mbedTLS too little for the markets handshake. Reusing one buffer
# keeps rendering allocation-free, which is what the frame needs anyway.
GMAX = 40                       # longest string any tile draws, in characters
_gbuf = None
_gfb = None

def _glyphs(s):
    global _gbuf, _gfb
    if _gfb is None:
        _gbuf = bytearray(GMAX * 8)      # GMAX bytes per row, 8 rows
        _gfb = framebuf.FrameBuffer(_gbuf, 8 * GMAX, 8, framebuf.MONO_HLSB)
    if len(s) > GMAX:
        s = s[:GMAX]
    _gfb.fill(0)
    _gfb.text(s, 0, 0, 1)
    return _gfb, 8 * len(s)

def text(dst, s, x, y, scale, colour, bold=True):
    # Bail before _glyphs allocates: with a 12-row strip most elements miss
    # any given pass entirely, and rendering the font for them 30 times per
    # tile would be the bulk of the work.
    if not s or y > STRIP or y + 8 * scale < 0:
        return
    g, w = _glyphs(s)
    d = max(1, scale // 3) if bold else 0
    for gy in range(8):
        yy = y + gy * scale
        if yy + scale < 0 or yy > STRIP:
            continue
        for gx in range(w):
            if g.pixel(gx, gy):
                dst.fill_rect(x + gx * scale, yy, scale + d, scale, colour)

def ctext(dst, s, y, scale, colour):
    text(dst, s, (W - tw(s, scale)) // 2, y, scale, colour)

def rtext(dst, s, y, scale, colour, margin=14):
    text(dst, s, W - margin - tw(s, scale), y, scale, colour)

def _rule(dst, y, h=4):
    dst.fill_rect(24, y, W - 48, h, FG)

def _degree(dst, x, y, r, colour):
    try:
        dst.ellipse(x + r, y + r, r, r, colour, True)
        dst.ellipse(x + r, y + r, max(1, r // 2), max(1, r // 2), BG, True)
    except AttributeError:
        dst.fill_rect(x, y, r*2, r*2, colour)
        dst.fill_rect(x + r//2, y + r//2, r, r, BG)

def _tri(dst, x, y, h, up, colour):
    """A solid triangle 2h wide, so market direction is a shape and not only
    a sign. Row i counts down from the top in both cases."""
    for i in range(h):
        if up:
            dst.fill_rect(x + h - i - 1, y + i, 2 * (i + 1), 1, colour)
        else:
            dst.fill_rect(x + i, y + i, 2 * (h - i), 1, colour)

def _temp(fb, tstr, y, scale):
    """Big temperature with a degree ring and an F, centred as a group."""
    r = max(4, scale + 2)
    fs = max(2, scale // 2)
    grp = tw(tstr, scale) + 6 + r*2 + 6 + tw("F", fs)
    x0 = (W - grp) // 2
    text(fb, tstr, x0, y, scale, ACCENT)
    dx = x0 + tw(tstr, scale) + 6
    _degree(fb, dx, y, r, ACCENT)
    text(fb, "F", dx + r*2 + 6, y + scale*2, fs, ACCENT)

def _bigtime(fb, s, y, scale):
    """Draw H:MM with a hand-built colon.

    Bold thickening widens the font's colon dots into what reads as an equals
    sign at large sizes, so the dots are drawn square and spaced by hand.
    """
    a, _, b = s.partition(":")
    gap = scale * 5
    total = tw(a, scale) + gap + tw(b, scale)
    x = (W - total) // 2
    text(fb, a, x, y, scale, FG)
    text(fb, b, x + tw(a, scale) + gap, y, scale, FG)
    cx = x + tw(a, scale) + (gap - scale) // 2
    fb.fill_rect(cx, y + scale * 2, scale, scale, FG)
    fb.fill_rect(cx, y + scale * 5, scale, scale, FG)

# ------------------------------------------------------------------ offline badge
def _badge(fb, oy):
    """A full-width bordered band reading OFFLINE, along the bottom edge.

    Deliberately not colour-only: it is a distinct horizontal shape spanning
    the tile, so it registers in peripheral vision regardless of where a
    central scotoma falls, and it renders identically in both DARK_MODE
    settings since it only uses FG/BG.
    """
    y0 = oy + BADGE_Y
    fb.rect(14, y0, W - 28, 22, FG)
    fb.rect(15, y0 + 1, W - 30, 20, FG)          # thicken the border by hand
    s = "OFFLINE"
    text(fb, s, (W - tw(s, 2)) // 2, y0 + 6, 2, FG)

# ------------------------------------------------------------------ tiles
def _paint_time(fb, d, oy):
    fb.fill(BG)
    _bigtime(fb, d["time"], oy + 10, fit(d["time"], W - 60, 8))
    ctext(fb, d["ampm"], oy + 96,  5, FG)
    ctext(fb, d["day"],  oy + 146, fit(d["day"],  W - 16, 4), FG)
    ctext(fb, d["date"], oy + 184, fit(d["date"], W - 16, 3), ACCENT)

def _paint_weather(fb, d, oy):
    fb.fill(BG)
    _temp(fb, d["temp"], oy + 2, fit(d["temp"], 190, 7))
    ctext(fb, d["cond"], oy + 64, fit(d["cond"], W - 24, 5), FG)
    _rule(fb, oy + 110)
    ctext(fb, d["feels"], oy + 120, fit(d["feels"], W - 24, 4), FG)
    ctext(fb, d["hum"],   oy + 158, fit(d["hum"],   W - 24, 3), FG)
    ctext(fb, d["wind"],  oy + 186, fit(d["wind"],  W - 24, 3), FG)

def _paint_forecast(fb, d, oy):
    fb.fill(BG)
    y = oy + 4
    for i, (day, hilo, cond) in enumerate(d["days"]):
        text(fb, day, 14, y, 4, ACCENT)
        # 192 px is what is left to the right of a three-letter day at scale 4.
        # "102/67" fills it exactly; only a three-digit low drops a size.
        rtext(fb, hilo, y, fit(hilo, 192, 4), FG)
        ctext(fb, cond, y + 38, fit(cond, W - 24, 3), FG)
        if i < 2:
            _rule(fb, y + 66, 2)
        y += 70

def _row(fb, team, score, y, scale, hilite):
    col = ACCENT if hilite else FG
    if hilite:                      # shape cue, not just colour
        fb.fill_rect(4, y + scale * 2, scale, scale * 4, col)
    text(fb, team, 18, y, scale, col)
    if score is not None:
        s = str(score)
        text(fb, s, W - 18 - tw(s, scale), y, scale, col)

def _row_scale(d, cap=5):
    """One scale for both rows - college abbreviations run to five characters
    and a three-digit basketball score would otherwise run into them."""
    pairs = ((d["away"], d["as"]), (d["home"], d["hs"]))
    for sc in range(cap, 1, -1):
        widest = max(tw(t, sc) + tw("" if v is None else str(v), sc) for t, v in pairs)
        if 36 + 16 + widest <= W:
            return sc
    return 2

def _paint_score(fb, d, oy):
    fb.fill(BG)
    ctext(fb, d["name"], oy + 2,  fit(d["name"], W - 16, 5), FG)
    ctext(fb, d["sub"],  oy + 48, fit(d["sub"],  W - 16, 3), ACCENT)
    _rule(fb, oy + 80)
    if d.get("none"):                                # off-season, or no game yet
        ctext(fb, "NO GAME",   oy + 108, 5, FG)
        ctext(fb, "SCHEDULED", oy + 162, 3, FG)
    elif d["as"] is None and d["hs"] is None:        # not played yet
        ctext(fb, d["match"], oy + 92,  fit(d["match"], W - 24, 4), FG)
        ctext(fb, d["when"],  oy + 134, fit(d["when"],  W - 24, 5), ACCENT)
        ctext(fb, d["at"],    oy + 180, fit(d["at"],    W - 24, 4), FG)
    else:
        me, sc = d.get("me"), _row_scale(d)
        _row(fb, d["away"], d["as"], oy + 92,  sc, d["away"] == me)
        _row(fb, d["home"], d["hs"], oy + 142, sc, d["home"] == me)
        ctext(fb, d["status"], oy + 188, fit(d["status"], W - 24, 3), FG)

def _paint_market(fb, d, oy):
    fb.fill(BG)
    y = oy + 4
    for i, (name, value, chg, up) in enumerate(d["rows"]):
        text(fb, name, 14, y, 3, FG)
        ctext(fb, value, y + 28, fit(value, W - 24, 5), FG)
        # arrow and percentage centred as one group, so neither can collide
        # with the index name above them however long the numbers get
        grp = 20 + 10 + tw(chg, 3)
        x0 = (W - grp) // 2
        _tri(fb, x0, y + 76, 10, up, ACCENT)
        text(fb, chg, x0 + 30, y + 72, 3, ACCENT)
        if i == 0:
            _rule(fb, y + 100, 2)
        y += 108

def _paint_mode(fb, d, oy):
    """The boot chooser. Tap anywhere rather than aiming at a side - it is
    easier to hit, and it shows the palette it is offering by being drawn in
    it, so the choice is the thing you are looking at."""
    fb.fill(BG)
    ctext(fb, d["mode"],  oy + 16,  fit(d["mode"], W - 24, 6), FG)
    ctext(fb, "TAP SCREEN", oy + 84,  3, ACCENT)
    ctext(fb, "TO CHANGE",  oy + 116, 3, ACCENT)
    _rule(fb, oy + 158, 2)
    ctext(fb, d["count"], oy + 176, fit(d["count"], W - 24, 3), FG)

def _paint_info(fb, d, oy):
    fb.fill(BG)
    ctext(fb, d["l1"], oy + 66,  fit(d["l1"], W - 24, 5), FG)
    ctext(fb, d["l2"], oy + 130, fit(d["l2"], W - 24, 3), ACCENT)

_PAINT = {"time": _paint_time, "weather": _paint_weather,
          "forecast": _paint_forecast, "score": _paint_score,
          "market": _paint_market, "info": _paint_info, "mode": _paint_mode}

# ------------------------------------------------------------------ blitting
_buf = None
_fb = None

def init():
    """Allocate the strip buffer once, early, and keep it forever.

    Allocating and freeing the buffer per draw fragments the heap: after a few
    tiles there is plenty of free memory but no contiguous block that size, and
    the draw dies with MemoryError. One permanent buffer makes rendering
    allocation-free and therefore safe to run for months. Its size is capped by
    TLS rather than by rendering - see the note on STRIP.
    """
    global _buf, _fb
    if _buf is None:
        gc.collect()
        _buf = bytearray(W * STRIP * 2)
        _fb = framebuf.FrameBuffer(_buf, W, STRIP, framebuf.RGB565)
    return _fb

def draw(tft, kind, d):
    fb = _fb if _fb is not None else init()
    paint = _PAINT[kind]
    tft.window(0, 0, W - 1, H - 1)
    for sy in range(0, H, STRIP):
        paint(fb, d, -sy)
        if d.get("offline"):
            _badge(fb, -sy)
        tft.blit(_buf)
    tft.end()

# ------------------------------------------------------------------ builders
def time_data(epoch_local, offline=False):
    import time as _t
    tm = _t.localtime(epoch_local)
    h12 = tm[3] % 12 or 12
    return {"time": "%d:%02d" % (h12, tm[4]),
            "ampm": "AM" if tm[3] < 12 else "PM",
            "day":  DAYS[tm[6]],
            "date": "%s %d" % (MONS[tm[1]-1], tm[2]),
            "offline": offline}

def _wind(cur):
    mph = round(cur.get("wind_speed_10m", 0))
    d = compass(cur.get("wind_direction_10m"))
    # no space before MPH: "WIND NW 12 MPH" is 14 chars and would drop a size
    return ("WIND %s %dMPH" % (d, mph)) if d else ("WIND %d MPH" % mph)

def weather_data(cur, offline=False):
    return {"temp":  "%d" % round(cur["temperature_2m"]),
            "cond":  WMO.get(cur["weather_code"], "WX %d" % cur["weather_code"]),
            "feels": "FEELS %d" % round(cur["apparent_temperature"]),
            "hum":   "HUMIDITY %d%%" % round(cur["relative_humidity_2m"]),
            "wind":  _wind(cur),
            "offline": offline}

def forecast_data(daily, epoch_local, offline=False):
    """The next three days, starting with tomorrow.

    open-meteo returns day 0 as today, which the weather tile already covers,
    so this starts at day 1 and net.py asks for four days to get three. The
    weekday names are just today's plus the offset - no date parsing.
    """
    import time as _t
    wd = _t.localtime(epoch_local)[6]
    hi = daily["temperature_2m_max"]
    lo = daily["temperature_2m_min"]
    codes = daily.get("weather_code") or []
    rows = []
    for i in range(1, min(4, len(hi), len(lo))):
        rows.append((SHORT[(wd + i) % 7],
                     "%d/%d" % (round(hi[i]), round(lo[i])),
                     WMO.get(codes[i], "") if i < len(codes) else ""))
    return {"days": rows, "offline": offline}

def _commas(n):
    s = "%d" % abs(n)
    out = ""
    while len(s) > 3:
        out = "," + s[-3:] + out
        s = s[:-3]
    return ("-" if n < 0 else "") + s + out

def market_data(quotes, offline=False):
    """quotes: sequence of (name, price, change_percent)."""
    rows = []
    for name, price, chg in quotes:
        rows.append((name, _commas(price), "%+.2f%%" % chg, chg >= 0))
    return {"rows": rows, "offline": offline}

def score_data(rec, name, sub, me, offline=False):
    if not rec:
        return {"name": name, "sub": sub, "me": me, "none": True,
                "as": None, "hs": None, "offline": offline}
    d = dict(rec)
    d["name"], d["sub"], d["me"] = name, sub, me
    d["match"] = "%s AT %s" % (rec.get("away", "?"), rec.get("home", "?"))
    d.setdefault("status", "")
    d["offline"] = offline
    return d

def mode_data(dark, secs):
    return {"mode": "DARK" if dark else "LIGHT",
            "count": "STARTING %d" % secs, "offline": False}

def info_data(l1, l2="", offline=False):
    return {"l1": l1, "l2": l2, "offline": offline}

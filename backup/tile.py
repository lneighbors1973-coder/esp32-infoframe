# Tiles for the photo frame, designed for low vision (macular degeneration).
#
# Design rules applied throughout:
#   - Maximum luminance contrast: white/yellow on pure black. No greys, no
#     gradients - AMD degrades contrast sensitivity as well as acuity.
#   - Very large glyphs, emboldened (strokes thickened ~1.3x).
#   - One idea per tile. Clutter competes with a central scotoma.
#   - Colour is never the only cue; size, position and rules carry meaning too.
#
# Flip the flag below for black-on-white. Which polarity suits a given person
# depends on their glare sensitivity, so try both.
import framebuf, gc

W, H, STRIP = 320, 240, 48
DARK_MODE = True

def _sw(c):
    """framebuf.RGB565 is little-endian here; the ILI9341 wants hi byte first."""
    return ((c & 0xFF) << 8) | (c >> 8)

if DARK_MODE:
    BG, FG, ACCENT = _sw(0x0000), _sw(0xFFFF), _sw(0xFFE0)
else:
    BG, FG, ACCENT = _sw(0xFFFF), _sw(0x0000), _sw(0x0000)

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

def _glyphs(s):
    w = 8 * len(s)
    fb = framebuf.FrameBuffer(bytearray(((w + 7) // 8) * 8), w, 8, framebuf.MONO_HLSB)
    fb.text(s, 0, 0, 1)
    return fb, w

def text(dst, s, x, y, scale, colour, bold=True):
    if not s:
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

def _degree(dst, x, y, r, colour):
    try:
        dst.ellipse(x + r, y + r, r, r, colour, True)
        dst.ellipse(x + r, y + r, max(1, r // 2), max(1, r // 2), BG, True)
    except AttributeError:
        dst.fill_rect(x, y, r*2, r*2, colour)
        dst.fill_rect(x + r//2, y + r//2, r, r, BG)

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
BADGE_H = 26          # total band a tile reserves when it is showing stale data

def _badge(fb, oy):
    """A full-width bordered band reading OFFLINE.

    Deliberately not colour-only: it is a distinct horizontal shape spanning
    the tile, so it registers in peripheral vision regardless of where a
    central scotoma falls, and it renders identically in both DARK_MODE
    settings since it only uses FG/BG.
    """
    y0 = oy + 2
    fb.rect(14, y0, W - 28, 22, FG)
    fb.rect(15, y0 + 1, W - 30, 20, FG)          # thicken the border by hand
    s = "OFFLINE"
    text(fb, s, (W - tw(s, 2)) // 2, y0 + 6, 2, FG)

# ------------------------------------------------------------------ tiles
def _paint_time(fb, d, oy):
    fb.fill(BG)
    off = d.get("offline")
    top = oy + (BADGE_H + 8 if off else 14)
    if off:
        _badge(fb, oy)
        _bigtime(fb, d["time"], top, fit(d["time"], W - 60, 7))
        ctext(fb, d["ampm"], top + 60, 3, FG)
        ctext(fb, d["day"],  top + 96, fit(d["day"], W - 16, 3), FG)
        ctext(fb, d["date"], top + 128, fit(d["date"], W - 16, 3), ACCENT)
    else:
        _bigtime(fb, d["time"], top, fit(d["time"], W - 60, 9))
        ctext(fb, d["ampm"], oy + 100, 5, FG)
        ctext(fb, d["day"],  oy + 156, fit(d["day"], W - 16, 4), FG)
        ctext(fb, d["date"], oy + 198, fit(d["date"], W - 16, 4), ACCENT)

def _paint_weather(fb, d, oy):
    fb.fill(BG)
    off = d.get("offline")
    if off:
        _badge(fb, oy)
        top = oy + BADGE_H + 6
        _temp(fb, d["temp"], top, fit(d["temp"], 190, 6))
        ctext(fb, d["cond"], top + 58, fit(d["cond"], W - 24, 4), FG)
        fb.fill_rect(24, top + 96, W - 48, 3, FG)
        ctext(fb, d["feels"], top + 104, fit(d["feels"], W - 24, 3), FG)
        ctext(fb, d["wind"],  top + 132, fit(d["wind"],  W - 24, 3), FG)
    else:
        _temp(fb, d["temp"], oy + 2, fit(d["temp"], 190, 7))
        ctext(fb, d["cond"], oy + 64, fit(d["cond"], W - 24, 5), FG)
        fb.fill_rect(24, oy + 110, W - 48, 4, FG)
        ctext(fb, d["feels"], oy + 120, fit(d["feels"], W - 24, 4), FG)
        ctext(fb, d["hilo"],  oy + 158, fit(d["hilo"],  W - 24, 3), ACCENT)
        ctext(fb, d["hum"],   oy + 188, fit(d["hum"],   W - 24, 3), FG)
        ctext(fb, d["wind"],  oy + 214, fit(d["wind"],  W - 24, 3), FG)

def _row(fb, team, score, y, scale, hilite):
    col = ACCENT if hilite else FG
    if hilite:                      # shape cue, not just colour
        fb.fill_rect(4, y + scale * 2, scale, scale * 4, col)
    text(fb, team, 18, y, scale, col)
    if score is not None:
        s = str(score)
        text(fb, s, W - 18 - tw(s, scale), y, scale, col)

def _paint_score(fb, d, oy):
    fb.fill(BG)
    off = d.get("offline")
    top = oy + (BADGE_H + 6 if off else 0)
    if off:
        _badge(fb, oy)
    ctext(fb, d["league"], top + 6, fit(d["league"], W - 24, 4), ACCENT)
    fb.fill_rect(24, top + 46, W - 48, 3, FG)
    if d["as"] is None and d["hs"] is None:          # not played yet
        sc = 4 if off else 5
        ctext(fb, d["match"], top + 62,  fit(d["match"], W - 24, sc), FG)
        ctext(fb, d["when"],  top + (108 if off else 118), fit(d["when"], W - 24, sc), FG)
        ctext(fb, d["at"],    top + (154 if off else 174), fit(d["at"],   W - 24, sc), ACCENT)
    else:
        sc = 5 if off else 6
        gap = 50 if off else 60
        _row(fb, d["away"], d["as"], top + 62,          sc, d["away"] == "KC")
        _row(fb, d["home"], d["hs"], top + 62 + gap,     sc, d["home"] == "KC")
        ctext(fb, d["status"], top + 62 + gap*2 + 8, fit(d["status"], W - 24, 4), FG)

_PAINT = {"time": _paint_time, "weather": _paint_weather, "score": _paint_score}

# ------------------------------------------------------------------ blitting
_buf = None
_fb = None

def init():
    """Allocate the strip buffer once, early, and keep it forever.

    Allocating and freeing 30 KB per draw fragments the heap: after a few tiles
    there is plenty of free memory but no contiguous block that size, and the
    draw dies with MemoryError. One permanent buffer makes rendering
    allocation-free and therefore safe to run for months.
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

def weather_data(cur, daily, offline=False):
    hi = lo = None
    if daily:
        try:
            hi = round(daily["temperature_2m_max"][0])
            lo = round(daily["temperature_2m_min"][0])
        except Exception:
            pass
    return {"temp":  "%d" % round(cur["temperature_2m"]),
            "cond":  WMO.get(cur["weather_code"], "WX %d" % cur["weather_code"]),
            "feels": "FEELS %d" % round(cur["apparent_temperature"]),
            "hilo":  ("HI %d   LO %d" % (hi, lo)) if hi is not None else "",
            "hum":   "HUMIDITY %d%%" % round(cur["relative_humidity_2m"]),
            "wind":  _wind(cur),
            "offline": offline}

def score_data(rec, offline=False):
    d = dict(rec)
    d["match"] = "%s AT %s" % (rec.get("away", "?"), rec.get("home", "?"))
    d.setdefault("status", "")
    d["offline"] = offline
    return d

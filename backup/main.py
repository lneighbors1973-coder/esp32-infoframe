# ESP32 photo frame - RGB565 frames streamed from SD, with touch, fade, and a
# rotating set of large-type tiles (clock / weather / Royals / Chiefs).
#
# HARDWARE (all pins verified empirically on this board, 2026-09-03):
#   Display  ILI9341   SPI1  SCK=14 MOSI=13 MISO=12  CS=15 DC=2  BL=21  MADCTL=0x28
#   SD card            SPI2  SCK=18 MOSI=23 MISO=19  CS=5   @20 MHz
#   Touch    XPT2046   SoftSPI SCK=25 MOSI=32 MISO=39 CS=33 IRQ=36
#   MCU      ESP32-D0WD-V3 rev3.1, 4 MB flash, no PSRAM, run at 240 MHz
#
# Touch axes are TRANSPOSED vs the display (panel is natively portrait).
# Measured: SD 227 KB/s, full-screen paint 66 ms, ~690 ms per photo, tile ~75 ms.
import os, sys, time, gc
from machine import Pin, SPI, PWM, WDT
from ili9341 import ILI9341
from xpt2046 import XPT2046
import tile, net, sports

INTERVAL    = 15            # seconds per slide
TILE_EVERY  = 3             # photo, photo, tile - so a tile every 45 s
# Which tile appears each time one is due. The clock gets extra slots because a
# clock you only see every six minutes is not much use as a clock.
TILE_ORDER  = ("time", "weather", "time", "royals", "time", "chiefs")
WX_TTL      = 900           # re-poll weather after this many seconds
SPORT_TTL   = 300           # scores go stale faster than weather
RETRY       = 120           # wait this long before retrying a failed fetch
NTP_TTL     = 21600         # re-sync the clock every 6 h
WDT_MS      = 90000         # a hung fetch reboots the frame instead of freezing it
FADE_MS     = 260
FRAME_DIR   = "/sd/frames"
FRAME_BYTES = 320 * 240 * 2
BUFSIZE     = 16 * 1024
MAX_FRAMES  = 1200          # heap guard; ~50 bytes of RAM per frame

# from 5-point calibration; mean residual 2.4 px. Axes are transposed.
TX_A, TX_B = 0.090795, -19.066      # screen x = TX_A * raw_y + TX_B
TY_A, TY_B = 0.067273, -18.758      # screen y = TY_A * raw_x + TY_B

spi = SPI(1, baudrate=40000000, polarity=0, phase=0,
          sck=Pin(14), mosi=Pin(13), miso=Pin(12))
bl  = PWM(Pin(21), freq=1000); bl.duty_u16(65535)
tft = ILI9341(spi, cs=15, dc=2, madctl=0x28)
ts  = XPT2046()

buf = bytearray(BUFSIZE)
mv  = memoryview(buf)
tile.init()                 # claim the tile strip buffer while the heap is clean

# ---------------------------------------------------------------- backlight
def _level(x):
    """Perceptual ramp - LED brightness is nowhere near linear in duty cycle."""
    return int(65535 * (x ** 2.2))

def fade(a, b, ms=FADE_MS, steps=22):
    for i in range(steps + 1):
        bl.duty_u16(_level(a + (b - a) * i / steps))
        time.sleep_ms(ms // steps)

# ---------------------------------------------------------------- photos
def show(path):
    tft.window(0, 0, 319, 239)
    with open(path, "rb") as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            tft.blit(buf if n == BUFSIZE else mv[:n])
    tft.end()

def playlist():
    try:
        names = []
        for f in os.listdir(FRAME_DIR):
            if f.endswith(".565"):
                names.append(f)
                if len(names) >= MAX_FRAMES:
                    print("frame cap %d reached - extra photos ignored" % MAX_FRAMES)
                    break
        names.sort()
        return [FRAME_DIR + "/" + f for f in names
                if os.stat(FRAME_DIR + "/" + f)[6] == FRAME_BYTES]
    except MemoryError:
        print("out of memory building playlist - lower MAX_FRAMES")
        gc.collect()
        return []
    except OSError as e:
        print("cannot read", FRAME_DIR, e)
        return []

# ---------------------------------------------------------------- touch
def touch_xy():
    r = ts.raw()
    if not r:
        return None
    x = int(TX_A * r[1] + TX_B)
    y = int(TY_A * r[0] + TY_B)
    return (0 if x < 0 else 319 if x > 319 else x,
            0 if y < 0 else 239 if y > 239 else y)

def wait_release():
    t0 = time.ticks_ms()
    while ts.pressed() and time.ticks_diff(time.ticks_ms(), t0) < 3000:
        time.sleep_ms(20)
    time.sleep_ms(150)

def blink(n):
    for _ in range(n):
        bl.duty_u16(0)
        time.sleep_ms(90)
        bl.duty_u16(65535)
        time.sleep_ms(90)

# ---------------------------------------------------------------- data
_wx = {"cur": None, "daily": None, "off": 0, "ts": -99999, "ntp": -99999, "try": -99999}
_sp = {"royals": {"d": None, "ts": -99999, "try": -99999},
       "chiefs": {"d": None, "ts": -99999, "try": -99999}}

def time_valid():
    return time.localtime()[0] >= 2024        # RTC unset reports year 2000/2001

def ensure_net():
    if not net.online():
        net.connect()                    # net.py rate-limits this internally
    if net.online() and (not time_valid() or time.time() - _wx["ntp"] > NTP_TTL):
        if net.sync_time():
            # Capture "now" AFTER a successful sync, not before: the very
            # first NTP sync of a boot jumps the clock forward ~25 years from
            # its unset ~2000 epoch, so a pre-sync timestamp looks decades
            # old by comparison and falsely trips the offline check on the
            # first-ever clock tile.
            _wx["ntp"] = time.time()

def weather():
    ensure_net()
    now = time.time()
    stale = _wx["cur"] is None or now - _wx["ts"] > WX_TTL
    if stale and now - _wx["try"] > RETRY:
        _wx["try"] = now
        cur, daily, off = net.fetch()
        if cur:
            _wx["cur"], _wx["daily"], _wx["off"], _wx["ts"] = cur, daily, off, now
    return _wx

def sport(kind):
    ensure_net()
    c = _sp[kind]
    now = time.time()
    stale = c["d"] is None or now - c["ts"] > SPORT_TTL
    if stale and now - c["try"] > RETRY:
        c["try"] = now
        fn = sports.royals if kind == "royals" else sports.chiefs
        try:
            r = fn(now + _wx["off"], _wx["off"])
            if r:
                c["d"], c["ts"] = r, now
        except Exception as e:
            print("%s fetch failed:" % kind)
            sys.print_exception(e)
    return c["d"]

def draw_tile(kind):
    """True if a tile was drawn; False falls back to a photo.

    "offline" below means this tile's own data is older than its normal
    refresh window - not just "wifi is down". That is the more honest signal:
    it still flags stale weather if wifi is fine but the API fetch itself
    keeps failing, and it stays quiet the instant a real refresh succeeds.

    Never allowed to raise - a fault here must not stop the photographs.
    """
    try:
        wx = weather()                       # also keeps wifi and the clock fresh
        now = time.time()
        if kind == "time":
            if not time_valid():
                return False
            offline = now - _wx["ntp"] > NTP_TTL
            tile.draw(tft, "time", tile.time_data(now + _wx["off"], offline))
        elif kind == "weather":
            if not wx["cur"]:
                return False
            offline = now - _wx["ts"] > WX_TTL
            tile.draw(tft, "weather", tile.weather_data(wx["cur"], wx["daily"], offline))
        else:
            rec = sport(kind)
            if not rec:
                return False
            offline = now - _sp[kind]["ts"] > SPORT_TTL
            tile.draw(tft, "score", tile.score_data(rec, offline))
        print("  tile:", kind, "(offline, showing last known)" if offline else "")
        return True
    except Exception as e:
        print("tile %s failed, showing a photo instead:" % kind)
        sys.print_exception(e)
        return False

# ---------------------------------------------------------------- main loop
tft.fill(0x0000)
print("photo frame ready - tap left/right to change, middle to pause")
print("interval %ds, tile every %d slides" % (INTERVAL, TILE_EVERY))
print("tile order:", TILE_ORDER)
net.connect()
wdt = WDT(timeout=WDT_MS)
print("watchdog armed at %d ms" % WDT_MS)

idx = slide = tidx = 0
paused = False

while True:
    wdt.feed()
    shots = playlist()
    if not shots:
        fade(1, 0, 120)
        tft.fill(0x4000)
        fade(0, 1, 120)
        print("no valid frames in", FRAME_DIR)
        time.sleep(5)
        continue

    want_tile = (slide % TILE_EVERY) == (TILE_EVERY - 1)
    fade(1, 0)                                   # everything below happens dark
    drew_tile = False
    if want_tile:
        drew_tile = draw_tile(TILE_ORDER[tidx % len(TILE_ORDER)])
        tidx += 1                                # advance even on failure
    if not drew_tile:
        idx %= len(shots)
        t0 = time.ticks_ms()
        show(shots[idx])
        print("[%d/%d] %s  %d ms%s" % (idx + 1, len(shots), shots[idx],
              time.ticks_diff(time.ticks_ms(), t0), "  (paused)" if paused else ""))
    fade(0, 1)
    gc.collect()

    step = 1
    deadline = time.ticks_add(time.ticks_ms(), INTERVAL * 1000)
    while True:
        if not paused and time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            break
        p = touch_xy()
        if p:
            if p[0] < 107:
                step = -1
                print("  tap left -> previous")
                wait_release()
                break
            elif p[0] > 213:
                step = 1
                print("  tap right -> next")
                wait_release()
                break
            else:
                paused = not paused
                print("  tap middle ->", "PAUSED" if paused else "RESUMED")
                blink(1 if paused else 2)
                wait_release()
                deadline = time.ticks_add(time.ticks_ms(), INTERVAL * 1000)
        time.sleep_ms(30)
        wdt.feed()

    if not drew_tile:
        idx += step
    slide += 1

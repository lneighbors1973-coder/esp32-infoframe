# ESP32 info frame - a rotating set of large-type tiles for a viewer with
# macular degeneration. Every slide is a tile; there is no photo mode.
#
# HARDWARE (all pins verified empirically on this board, 2026-09-03):
#   Display  ILI9341   SPI1  SCK=14 MOSI=13 MISO=12  CS=15 DC=2  BL=21  MADCTL=0x28
#   Touch    XPT2046   SoftSPI SCK=25 MOSI=32 MISO=39 CS=33 IRQ=36
#   MCU      ESP32-D0WD-V3 rev3.1, 4 MB flash, no PSRAM, run at 240 MHz
#
# Touch axes are TRANSPOSED vs the display (panel is natively portrait).
# Measured: full-screen paint 66 ms, tile ~75 ms.
import sys, os, time, gc

from machine import Pin, SPI, PWM, WDT
from ili9341 import ILI9341
from xpt2046 import XPT2046
import tile, net, sports, markets

INTERVAL   = 15             # seconds per tile
TILE_ORDER = ("time", "royals_last", "royals_next",
              "chiefs_last", "chiefs_next",
              "time", "weather", "forecast",
              "ku_fb", "ku_bb", "markets")
WX_TTL     = 900            # re-poll weather after this many seconds
MKT_TTL    = 900            # index levels; refreshed live whenever the
                            # ESP-IDF heap has room for a handshake, and
                            # skipped instantly when it does not
SPORT_TTL  = 1800           # a schedule barely moves between games
LIVE_TTL   = 300            # ...but a game in progress does
RETRY      = 120            # wait this long before retrying a failed fetch
MKT_RETRY  = 300            # ...except the indices, which back off further.
                            # This was 900 with a 2 h ceiling, sized for when a
                            # failure meant a wedged TLS handshake: 150 s of
                            # frozen tile and an ESP-IDF heap too damaged to
                            # retry at all. On the current firmware that is
                            # gone. Measured over 16 h on 2026-09-10: 51 good
                            # fetches, 4 failures, every one of them at=read -
                            # the handshake succeeded and Yahoo was merely slow
                            # with the body - costing 16 s, the socket timeout,
                            # with the heap untouched at idf 34812. A 30 min
                            # backoff after a 16 s blip just left the tile
                            # stale, so retry in five minutes instead.
MKT_MAX_W  = 1800           # ...and never back off more than half an hour
NTP_TTL    = 21600          # re-sync the clock every 6 h
WDT_MS     = 240000         # a hung fetch reboots the frame instead of freezing it
                            # - longer than the ~150 s a wedged TLS socket
                            # takes to give up, so a bad endpoint degrades to
                            # an OFFLINE badge rather than a reboot loop
FADE_MS    = 260
MODE_FILE  = "mode.txt"     # remembers the light/dark choice across reboots
OFF_FILE   = "tzoff.txt"    # ...and the same for the UTC offset
MODE_MS    = 8000           # how long the chooser waits before starting
LOG_FILE   = "markets.log"  # outcome of every markets attempt, kept on the board
LOG_MAX    = 16000          # bytes; the log restarts rather than growing forever.
                            # Raised from 6000 once markets started succeeding: an
                            # ok line every 15 min is ~67 bytes, so 6000 covered
                            # barely a day and rotation DELETES the file rather
                            # than trimming it. 16000 holds about three days.

# from 5-point calibration; mean residual 2.4 px. Axes are transposed.
TX_A, TX_B = 0.090795, -19.066      # screen x = TX_A * raw_y + TX_B
TY_A, TY_B = 0.067273, -18.758      # screen y = TY_A * raw_x + TY_B

# tile name -> (feed, which game, headline, subhead, team to highlight)
SCORES = {"royals_last": ("royals", "last", "ROYALS",   "LAST GAME",  "KC"),
          "royals_next": ("royals", "next", "ROYALS",   "NEXT GAME",  "KC"),
          "chiefs_last": ("chiefs", "last", "CHIEFS",   "LAST GAME",  "KC"),
          "chiefs_next": ("chiefs", "next", "CHIEFS",   "NEXT GAME",  "KC"),
          "ku_fb":       ("ku_fb",  "auto", "JAYHAWKS", "FOOTBALL",   "KU"),
          "ku_bb":       ("ku_bb",  "auto", "JAYHAWKS", "BASKETBALL", "KU")}
FEEDS = {"royals": sports.royals, "chiefs": sports.chiefs,
         "ku_fb": sports.ku_football, "ku_bb": sports.ku_basketball}

spi = SPI(1, baudrate=40000000, polarity=0, phase=0,
          sck=Pin(14), mosi=Pin(13), miso=Pin(12))
bl  = PWM(Pin(21), freq=1000); bl.duty_u16(65535)
tft = ILI9341(spi, cs=15, dc=2, madctl=0x28)
ts  = XPT2046()

tile.init()                 # claim the tile strip buffer while the heap is clean

# Collect eagerly, so the allocator asks for more memory as rarely as possible.
#
# This used to carry a note saying MicroPython takes ESP-IDF heap for its GC
# heap and never returns it. Half true, and it misdirected the markets-tile
# hunt for two days: auto-grow does take from that pool, and it DOES hand an
# area back - but only once nothing in that area is live, so in practice it
# ratchets upward and never comes down. The board now runs firmware with a
# fixed 104 KiB heap and auto-grow off, so there is no ratchet left to avoid.
# Collecting early still costs nothing and keeps peak usage down, which on a
# heap that can no longer grow matters more than it did before.
gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())

# ---------------------------------------------------------------- backlight
def _level(x):
    """Perceptual ramp - LED brightness is nowhere near linear in duty cycle."""
    return int(65535 * (x ** 2.2))

def fade(a, b, ms=FADE_MS, steps=22):
    for i in range(steps + 1):
        bl.duty_u16(_level(a + (b - a) * i / steps))
        time.sleep_ms(ms // steps)

# ---------------------------------------------------------------- status LED
# The board's RGB LED is common-anode: driving a pin LOW lights that channel.
# The pins were found by photographing the board through a webcam while driving
# each candidate in turn, because red is NOT on GPIO 4 the way this board's
# usual documentation says - it is on GPIO 22, which the SPI expansion header
# also labels CS. Nothing in the frame uses that header, so there is no clash.
# Red runs on PWM so the steady offline glow can be turned down; at full
# power it is a beacon in a dim room. Green and blue stay plain outputs - the
# white flash wants all three at FULL brightness or it tints cyan, so only the
# steady state is dimmed, never the flash.
LED_R = PWM(Pin(22), freq=1000, duty_u16=65535)   # 65535 = off; do not
                                # omit it. A PWM built without a duty can
                                # come up at 0, which on a common-anode LED
                                # is FULL red - lit from boot until the
                                # first led() call some 20 s later.
LED_G = Pin(16, Pin.OUT, value=1)
LED_B = Pin(17, Pin.OUT, value=1)
RED_DIM = 0.5                   # perceived brightness of the offline red
_led = (0, 0, 0)                # what the steady state should be

def _red(level):
    """level is 0..1 and perceptual, using the same 2.2 gamma as the backlight:
    an LED at half duty looks much brighter than half. Common anode, so the
    duty is inverted - 65535 is fully off."""
    LED_R.duty_u16(65535 - int(65535 * (level ** 2.2)))

def led(r=0, g=0, b=0):
    global _led
    _led = (r, g, b)
    _red(r)                     # r is a level, not just a flag
    LED_G.value(0 if g else 1)
    LED_B.value(0 if b else 1)

def led_flash(times=2, ms=140):
    """Flash white, then restore whatever was showing before."""
    keep = _led
    for _ in range(times):
        led(1, 1, 1)
        time.sleep_ms(ms)
        led(0, 0, 0)
        time.sleep_ms(ms)
    led(*keep)

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

# ---------------------------------------------------------------- display mode
def load_mode():
    """True for dark. Defaults to dark when the file is missing or unreadable."""
    try:
        f = open(MODE_FILE)
        v = f.read().strip()
        f.close()
        return v != "light"
    except Exception:
        return True

def save_mode(dark):
    try:
        f = open(MODE_FILE, "w")
        f.write("dark" if dark else "light")
        f.close()
    except Exception as e:
        print("mode not saved:", e)

def choose_mode():
    """Offer light or dark for a few seconds at boot, then keep the answer.

    Tap anywhere rather than aiming at a side: it is far easier to hit, and
    the screen is already drawn in whichever palette is currently selected, so
    the choice is the thing being looked at. Each tap restarts the countdown,
    so there is no rush. Runs before the watchdog is armed - it is allowed to
    sit here for as long as somebody keeps tapping.
    """
    dark = load_mode()
    tile.set_mode(dark)
    end = time.ticks_add(time.ticks_ms(), MODE_MS)
    shown = None
    while True:
        left = time.ticks_diff(end, time.ticks_ms())
        if left <= 0:
            break
        secs = left // 1000 + 1
        if secs != shown:
            tile.draw(tft, "mode", tile.mode_data(dark, secs))
            shown = secs
        if touch_xy():
            dark = not dark
            tile.set_mode(dark)
            shown = None                    # redraw at once in the new palette
            wait_release()
            end = time.ticks_add(time.ticks_ms(), MODE_MS)
        time.sleep_ms(30)
    save_mode(dark)
    # Repaint at once. What follows - wifi, NTP, the first fetches - can take
    # a minute or more and polls nothing, so leaving the countdown frozen at
    # "STARTING 1" looks exactly like a crash.
    tile.draw(tft, "info", tile.info_data("STARTING", "PLEASE WAIT"))
    print("display mode:", "dark" if dark else "light")

# ---------------------------------------------------------------- markets log
def logline(s):
    """Append one dated line to a small on-board log.

    The markets fetch fails intermittently for reasons not yet explained, and
    a serial console cannot be left attached for days to catch it. Writing to
    flash instead means the record survives reboots and can be read back with
    one command. Capped so it can never fill the filesystem, and never allowed
    to raise - a logging fault must not cost a tile.
    """
    try:
        t = time.localtime(time.time() + _wx["off"])
        try:
            if os.stat(LOG_FILE)[6] > LOG_MAX:
                os.remove(LOG_FILE)
        except OSError:
            pass                        # no log yet, which is fine
        f = open(LOG_FILE, "a")
        f.write("%02d/%02d %02d:%02d %s\n" % (t[1], t[2], t[3], t[4], s))
        f.close()
    except Exception as e:
        print("log failed:", e)

# ---------------------------------------------------------------- data
def load_off():
    """The saved UTC offset in seconds, or 0 if there is none yet.

    NTP sets the clock to UTC, and the only thing that knows this frame is on
    Central time is the weather feed - which is the seventh tile in the
    rotation. Without a saved value the first clock tile of every boot is five
    hours fast, and stays that way until the weather lands. Keeping the offset
    on flash makes the very first tile right, and right even with no network.
    """
    try:
        f = open(OFF_FILE)
        v = int(f.read().strip())
        f.close()
        if -50400 <= v <= 50400:        # sanity: no real zone is past +/-14 h
            return v
    except Exception:
        pass
    return 0

def save_off(off):
    """Only ever called when the offset actually changes - twice a year, at
    the DST boundaries. Rewriting it on every weather poll would be a flash
    write every fifteen minutes for the life of the frame."""
    try:
        f = open(OFF_FILE, "w")
        f.write("%d" % off)
        f.close()
        print("tz offset saved:", off)
    except Exception as e:
        print("tz not saved:", e)

_wx = {"cur": None, "daily": None, "off": load_off(),
       "ts": -99999, "ntp": -99999, "try": -99999}
_mk = {"d": None, "ts": -99999, "try": -99999, "wait": MKT_RETRY}
_sp = {}
for _k in FEEDS:
    _sp[_k] = {"d": None, "ts": -99999, "try": -99999}

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
            if off != _wx["off"]:
                save_off(off)           # first run, or a DST changeover
            _wx["cur"], _wx["daily"], _wx["off"], _wx["ts"] = cur, daily, off, now
    return _wx

def market():
    ensure_net()
    now = time.time()
    stale = _mk["d"] is None or now - _mk["ts"] > MKT_TTL
    if stale and now - _mk["try"] > _mk["wait"]:
        _mk["try"] = now
        try:
            net.last_err = ""           # so a stale reason cannot be logged
            _reach = markets.reach()    # DNS and bare TCP, before any TLS
            _t0 = time.ticks_ms()
            q = markets.quotes()
            _ms = time.ticks_diff(time.ticks_ms(), _t0)
            if q:
                _mk["d"], _mk["ts"] = q, now
                _mk["wait"] = MKT_RETRY             # good again; back to normal
                print("   markets refreshed:", q)
                logline("ok   %.2f %.2f %dms [%s]" % (q[0][1], q[1][1], _ms, _reach))
            else:
                _mk["wait"] = min(_mk["wait"] * 2, MKT_MAX_W)
                # idf tells the two failure modes apart: a low number means the
                # heap guard skipped it, a high one means the network refused.
                # net.last_err then says which way the network refused, which
                # is the difference between a timeout, a reset and an ENOMEM.
                # big matters as much as the total: mbedTLS needs one
                # contiguous ~40 KB block, and a fragmented heap can show
                # plenty free while having nowhere to put it. The elapsed time
                # separates a hang (~150 s) from an instant refusal.
                # A failed handshake leaves ~25 KB of GC heap held. One
                # collect does not free it - on the bench the memory only
                # came back on the NEXT attempt's collect - and while it is
                # held the frame is close enough to the edge that an ordinary
                # weather parse forces MicroPython to take 59 KB of ESP-IDF
                # heap it never returns, which puts the tile below the
                # handshake threshold for good. Collect repeatedly here so a
                # timeout costs one stalled tile instead of the whole day.
                for _ in range(3):
                    gc.collect()
                _f, _b = markets.idf_free()
                logline("FAIL idf=%d %dms at=%s [%s] wait=%d %s"
                        % (_f, _ms, net.last_phase, _reach, _mk["wait"],
                           net.last_err or "guard or no quote"))
        except Exception as e:
            _mk["wait"] = min(_mk["wait"] * 2, MKT_MAX_W)
            print("markets fetch failed:")
            sys.print_exception(e)
            logline("RAISED %s: %s" % (type(e).__name__, e))
    return _mk["d"]

def _sig(d):
    """The scores in a feed, compressed for comparison.

    Only the teams and their scores - deliberately not the status, or a clock
    ticking from "TOP 5" to "BOT 5" would flash the LED without the score
    having moved.
    """
    if not d:
        return None
    out = []
    for r in (d.get("last"), d.get("next")):
        out.append((r.get("away"), r.get("as"), r.get("home"), r.get("hs"))
                   if r else None)
    return tuple(out)

def feed(name):
    """One team's {"last", "next"} pair. Both of a team's tiles share it, so
    the fetch happens once per refresh rather than once per tile."""
    ensure_net()
    c = _sp[name]
    now = time.time()
    nxt = c["d"].get("next") if c["d"] else None
    ttl = LIVE_TTL if (nxt and nxt.get("live")) else SPORT_TTL
    stale = c["d"] is None or now - c["ts"] > ttl
    if stale and now - c["try"] > RETRY:
        c["try"] = now
        try:
            r = FEEDS[name](now + _wx["off"], _wx["off"])
            if r:
                before = _sig(c["d"])
                c["d"], c["ts"] = r, now
                # Never on the first fetch of a boot: there is nothing to have
                # changed from, and every team would flash at once.
                if before is not None and _sig(r) != before:
                    print("   %s score changed" % name)
                    led_flash()
        except Exception as e:
            print("%s fetch failed:" % name)
            sys.print_exception(e)
    return c["d"]

def _pick(d, which):
    if not d:
        return None
    if which != "auto":
        return d.get(which)
    # One tile for a team that gets one: whatever is most interesting now.
    nxt = d.get("next")
    if nxt and nxt.get("live"):
        return nxt
    return d.get("last") or nxt

# ---------------------------------------------------------------- tiles
def tile_spec(kind):
    """(paint kind, data) for one tile, or None if it has nothing to show yet.

    This is where the network fetch happens, deliberately before the caller
    fades the screen down: a score refresh can take ten seconds, and the
    previous tile staying lit through it reads far better than a black panel.

    "offline" below means this tile's own data is older than its normal
    refresh window - not just "wifi is down". That is the more honest signal:
    it still flags stale weather if wifi is fine but the API fetch itself
    keeps failing, and it stays quiet the instant a real refresh succeeds.

    Never allowed to raise - a fault here must not stop the frame.
    """
    try:
        if kind == "time":
            ensure_net()                     # keeps wifi and the clock fresh
            if not time_valid():
                return None
            now = time.time()
            return "time", tile.time_data(now + _wx["off"],
                                          now - _wx["ntp"] > NTP_TTL)
        if kind in ("weather", "forecast"):
            wx = weather()
            now = time.time()
            off = now - _wx["ts"] > WX_TTL
            if kind == "weather":
                if not wx["cur"]:
                    return None
                return "weather", tile.weather_data(wx["cur"], off)
            if not wx["daily"]:
                return None
            return "forecast", tile.forecast_data(wx["daily"], now + _wx["off"], off)
        if kind == "markets":
            q = market()
            if not q:
                return None
            return "market", tile.market_data(q, time.time() - _mk["ts"] > MKT_TTL)
        name, which, label, sub, me = SCORES[kind]
        d = feed(name)
        if d is None:                        # never fetched - nothing honest to show
            return None
        rec = _pick(d, which)
        if rec and rec.get("live") and which == "next":
            # A game in progress arrives on the next-game tile. Saying "NEXT
            # GAME" above a live score reads as though it has not started.
            sub = "LIVE NOW"
        off = time.time() - _sp[name]["ts"] > SPORT_TTL
        return "score", tile.score_data(rec, label, sub, me, off)
    except Exception as e:
        print("tile %s failed:" % kind)
        sys.print_exception(e)
        return None

def show(idx, step):
    """Draw TILE_ORDER[idx], skipping over tiles with no data yet."""
    n = len(TILE_ORDER)
    for _ in range(n):
        kind = TILE_ORDER[idx % n]
        spec = tile_spec(kind)
        if spec:
            fade(1, 0)                       # only now does the screen go dark
            try:
                tile.draw(tft, spec[0], spec[1])
            except Exception as e:
                print("draw %s failed:" % kind)
                sys.print_exception(e)
            fade(0, 1)
            return idx, kind
        idx += step
    return idx, None

# ---------------------------------------------------------------- main loop
tft.fill(0x0000)
print("info frame ready - tap left/right to change tile, middle to pause")
print("%d tiles, %ds each" % (len(TILE_ORDER), INTERVAL))
print("tile order:", TILE_ORDER)
choose_mode()               # first thing on screen, before the slow network bit
net.connect()
wdt = WDT(timeout=WDT_MS)
net.tick = wdt.feed         # a slow multi-request fetch must not trip the dog
net.note = logline          # ...and record anything that threatens the heap
print("watchdog armed at %d ms" % WDT_MS)
# Region breakdown at boot. On 2026-09-09 the frame came up with ~62 KB of RAM
# simply absent after a SW_CPU_RESET, where a boot following a real power cycle
# had it all - so the shortfall survives machine.reset(), which no Python object
# or LWIP buffer would. Print the regions to see which one is short.
try:
    import esp32, machine
    print("reset cause:", machine.reset_cause())
    for _r in esp32.idf_heap_info(esp32.HEAP_DATA):
        print("  region total=%d free=%d largest=%d minfree=%d" % _r)
except Exception as _e:
    print("region dump failed:", _e)
# Fetch the indices HERE, before the rotation starts. This was deliberately
# left out for a while, because a Yahoo timeout holds the boot screen for
# ~150 s looking like a hang. The logs since have changed the calculation:
#
#   - The frame's markets fetch has failed on its FIRST attempt every single
#     boot (09/07 22:42, 09/08 23:40, 09/09 17:37), always ~6 minutes in, once
#     the rotation is already running.
#   - The identical call from a bench script on this board succeeded 10 times
#     out of 10 the same day, ~2 s each, including a soak that ran full tile
#     cycles and armed a watchdog between attempts.
#   - A failed handshake costs ~25 KB of GC heap, which shortly forces
#     MicroPython to take 59 KB of ESP-IDF heap it never returns. That drops
#     the heap below what mbedTLS needs, so the guard then skips every later
#     attempt: one failure kills the tile until the next reboot.
#
# So the first attempt is the only one that reliably matters, and boot is the
# moment the frame most resembles the bench that works. Doing it here also
# means a failure costs a slow boot rather than a poisoned heap for the day.
# ...but NOT here, tried 2026-09-09 and reverted the same evening. The fetch
# blocks for ~150 s on a timeout, and at boot it sits behind net.connect(),
# an NTP sync and the reachability probe with only a 240 s watchdog to spare.
# It overran, the watchdog reset the board, and the frame boot-looped on the
# MARKETS LOADING screen without ever reaching this loop - no log line was
# written at all, which is how it was spotted. It also did not help: the boot
# attempt timed out exactly like the rotation ones. The tile fills itself when
# it first comes round instead, where the watchdog has the whole cycle.

idx, step = 0, 1
paused = False
_booted = False             # log one line per boot, once the clock is real

# Highest ESP-IDF free seen this boot. MicroPython grows its GC heap out of
# this pool in one ~59 KB step and never gives it back, and that step has now
# happened twice with no idea which fetch triggered it - reasoning about it
# from the outside has been wrong twice. So watch the number directly and name
# the tile that was on screen when it moved. Only a fall of this size counts;
# the reading jitters by a few hundred bytes normally.
_idf_hwm = markets.idf_free()[0]
IDF_DROP = 20000

# The other half of the same question. When the heap grew on 09/09 the frame
# had roughly 22 KB of GC memory free, where a bench script running the same
# fetches sits at 101 KB - and nothing in the drivers or the tile buffer
# accounts for a 79 KB difference. Whatever holds that memory is what puts the
# frame one allocation away from taking 59 KB of ESP-IDF heap it never returns.
# So record the floor as it descends, and which tile pushed it there.
_gc_low = gc.mem_free()
GC_STEP = 10000

while True:
    wdt.feed()
    idx, kind = show(idx, step)
    if not _booted and time_valid():
        # A reboot would otherwise be invisible in the log, and a watchdog
        # reset is exactly the kind of thing worth spotting after the fact.
        # The offset comes off flash now, so this is local time from the first
        # pass and no longer has to wait for the weather fetch - which matters
        # precisely when it is most worth knowing, on a frame that cannot
        # reach the network.
        _booted = True
        logline("boot idf=%d gc=%d" % (markets.idf_free()[0], gc.mem_free()))
    led(RED_DIM, 0, 0) if not net.online() else led(0, 0, 0)
    gc.collect()
    _idf = markets.idf_free()[0]
    if _idf_hwm - _idf > IDF_DROP:
        # Record the step down once, then treat the new level as normal, so a
        # collapse costs one line rather than one per tile forever after.
        logline("DROP after %s idf %d->%d gcheap %d"
                % (kind or "none", _idf_hwm, _idf, gc.mem_free() + gc.mem_alloc()))
        # The GC heap does NOT grow across this drop - measured 2026-09-09,
        # gcheap 137920 -> 135552 while idf fell 59368 - so MicroPython is not
        # the one taking it, whatever every comment in this project says. One
        # ~58 KB block leaves the largest region in a single step, during the
        # tile that pulls the most data. That points at the wifi driver or
        # LWIP, so ask them: dump the regions, then cycle the interface. If
        # the memory comes back when wifi goes down, the owner is identified
        # and cycling is also the cure.
        try:
            import esp32
            rows = esp32.idf_heap_info(esp32.HEAP_DATA)
            logline("regions " + " ".join("%d/%d" % (r[1], r[2]) for r in rows))
        except Exception as e:
            logline("regions failed %s: %s" % (type(e).__name__, e))
        _idf_hwm = markets.idf_free()[0]
    elif _idf > _idf_hwm:
        _idf_hwm = _idf
    _gcf = gc.mem_free()
    if _gc_low - _gcf > GC_STEP:
        logline("gclow after %s gc %d->%d idf=%d" % (kind or "none", _gc_low, _gcf, _idf))
        _gc_low = _gcf
    elif _gcf > _gc_low + GC_STEP:
        _gc_low = _gcf          # heap grew or a cache was freed; track the new floor
    # gcheap is the one that settles it. If MicroPython grew its GC heap out
    # of the ESP-IDF pool, this total rises by the same ~59 KB the IDF heap
    # loses. If it stays flat while idf falls, the memory went somewhere else
    # entirely - LWIP socket buffers being the obvious suspect, since the
    # tile that triggers it reads 600 bytes out of 14 KB ESPN documents.
    print("[%s] free %d alloc %d gcheap %d, idf %d big %d%s"
          % (kind or "no data yet", gc.mem_free(), gc.mem_alloc(),
             gc.mem_free() + gc.mem_alloc(), _idf, markets.idf_free()[1],
             "  (paused)" if paused else ""))

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

    idx += step

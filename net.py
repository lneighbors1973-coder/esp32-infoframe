# Wi-Fi, NTP and weather for the info frame. All failures are non-fatal.
import network, time, gc

LAT, LON = 38.88, -94.82               # Olathe, KS (66062). Two decimals on
                                       # purpose: this repo is public, and ~1 km
                                       # is finer than the forecast grid anyway.
                                       # Do not restore the precise fix.
# Plain HTTP on purpose. HTTPS does fit on the custom firmware this board
# runs, but weather and scores are happy on port 80, and each avoided
# handshake saves a second of latency and a scarce contiguous block. No credentials are sent and the payload is
# public data, so the only exposure is LAN-level spoofing. markets.py uses
# HTTPS because its source offers nothing else.
URL = ("http://api.open-meteo.com/v1/forecast"
       "?latitude=38.88&longitude=-94.82"
       "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
       "weather_code,wind_speed_10m,wind_direction_10m"
       # four days, because the forecast tile shows the next three and skips
       # today - the weather tile already covers today
       "&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=4"
       "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=America%2FChicago")

_wlan = None
_last_attempt = -99999
RETRY_COOLDOWN = 60         # do not retry a dead network more than this often

# main.py points this at wdt.feed(). One score tile can make nine HTTP calls
# and the watchdog is shorter than their combined timeouts, so the board would
# reboot mid-refresh without a tick between requests. Each request keeps its
# own timeout, so a genuinely hung board is still caught.
tick = None

def _tick():
    if tick:
        tick()

# main.py points this at frame.logline. A response too big for the shared
# buffer is the one thing here that could still cost 59 KB of ESP-IDF heap if
# it were ever handled by allocating, so when one turns up it goes in the
# on-board log with its URL rather than only to a serial console nobody is
# watching.
note = None

# The reason the last request failed, kept for the caller to log. get_head and
# get_json swallow their exceptions on purpose - a dead feed must not stop the
# frame - but that also threw away the one detail worth having about the
# markets tile: whether Yahoo timed out, refused, or ran the board out of
# memory. Those want different fixes and the log could not tell them apart.
last_err = ""

# Which stage a request reached. The markets fetch hangs for the full 150 s
# socket timeout in the running frame while the identical call succeeds in
# 1.8 s from a pasted script - 15 times out of 15 - so the question is no
# longer whether it hangs but where. "req" covers DNS, the TCP connect and
# the TLS handshake; "read" is the body. Whichever one is still set when the
# exception lands is the stage that never finished.
last_phase = ""

def _phase(p):
    global last_phase
    last_phase = p

def _err(e):
    global last_err
    last_err = "%s: %s" % (type(e).__name__, e)
    return last_err

def _note(s):
    print("net:", s)
    if note:
        try:
            note(s)
        except Exception:
            pass

# MicroPython's requests sends no User-Agent at all, and Yahoo answers a
# header-less request with 429 forever. Any value works; this one is honest.
HEADERS = {"User-Agent": "esp32-infoframe/1.0"}

# get_head's headers, kept as one dict and mutated rather than rebuilt per
# call. Same User-Agent; the Range value is rewritten each time.
_RANGE_HDRS = {"User-Agent": "esp32-infoframe/1.0", "Range": "bytes=0-599"}

# One response buffer, claimed at import while the heap is still clean, and
# reused for every request afterwards. Building a body at run time - r.json(),
# or reading chunks and joining them - asks for a single contiguous block the
# size of the response, and a frame that has been drawing tiles for a while
# has no block that big left. MicroPython answers by enlarging its GC heap out
# of the ESP-IDF pool, and because auto-grow only ever ratchets upward (an
# area is released only when nothing in it is live), one transient spike
# permanently shrinks what is left for mbedTLS.
#
# The board now runs firmware with a fixed 104 KiB heap, so that ratchet is
# gone and this buffer is no longer load-bearing for the markets tile. Keep it
# anyway: it is still the difference between a bounded read and an unbounded
# one, and a fixed heap makes big transient allocations MORE likely to raise
# MemoryError, not less.
#
# The tail is kept full of spaces so the buffer always parses as one document
# followed by harmless trailing whitespace, however short this body is.
BODY_MAX = 2048
_body = bytearray(BODY_MAX)     # no "b' ' * n" temporary: that would be a
for _i in range(BODY_MAX):      # second block of the same size, and a single
    _body[_i] = 0x20            # large allocation here is enough to grow the
                                # GC heap out of the ESP-IDF heap for good
_bodymv = memoryview(_body)
_hwm = 0                        # bytes written by the largest body so far

def body():
    """The shared response buffer. Valid up to the length get_head returned;
    scan it with find(pattern, 0, n) rather than slicing the whole thing."""
    return _body

def _blank(got):
    """Restore the spaces after a body, so the next parse sees only it."""
    global _hwm
    for i in range(got, _hwm):
        _body[i] = 0x20
    if got > _hwm:
        _hwm = got

def connect(timeout=25):
    """Connect to Wi-Fi. Always leaves the WLAN driver in a clean state,
    even on failure - without this, a failed attempt (out of range, wrong
    password, router down) wedges the ESP32 wifi driver: it stops accepting
    new connect() calls and every future call raises
    OSError: Wifi Internal State Error until the board is rebooted.
    Cycling the interface off/on before each attempt avoids that.

    Rate-limited here, not by each caller: fetch(), get_json() and get_head()
    all fall back to connect() on their own when offline, so a single shared
    cooldown is the only way to stop a permanently-offline board from paying
    a fresh 25 s timeout on every one of the several HTTP calls one tile
    (e.g. the Chiefs tile, which makes up to five) can make in one draw.
    """
    global _wlan, _last_attempt
    try:
        import secrets
    except ImportError:
        print("net: no secrets.py - running offline")
        return False
    _wlan = network.WLAN(network.STA_IF)
    if _wlan.active() and _wlan.isconnected():
        return True
    now = time.time()
    if now - _last_attempt < RETRY_COOLDOWN:
        return False
    _last_attempt = now
    try:
        _wlan.active(False)
    except Exception:
        pass
    _wlan.active(True)
    # The ESP32 station defaults to pm=1, modem power save: it sleeps between
    # beacons and wakes on its own schedule. Every plain-HTTP fetch here is one
    # short round trip and shrugs that off, but a TLS handshake is half a dozen
    # round trips and a lost packet in one costs the whole exchange - and
    # MicroPython does not honour a socket timeout through a handshake, so the
    # cost is a 150 s stall, not a quick retry. The frame is mains powered, so
    # there is nothing to save. Guarded because the constant is not in every
    # port, and it is only ever an optimisation.
    try:
        _wlan.config(pm=network.WLAN.PM_NONE)
    except Exception as e:
        print("net: could not disable wifi power save:", e)
    print("net: connecting to", secrets.WIFI_SSID)
    try:
        _wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASS)
    except OSError as e:
        print("net: connect() raised:", e)
        return False
    t0 = time.ticks_ms()
    while not _wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout * 1000:
            print("net: wifi timeout (status %s)" % _wlan.status())
            return False
        time.sleep_ms(250)
        _tick()
    print("net: wifi ok", _wlan.ifconfig()[0])
    return True

def online():
    return _wlan is not None and _wlan.isconnected()

def sync_time():
    if not online():
        return False
    import ntptime
    for _ in range(3):
        try:
            ntptime.settime()
            print("net: ntp ok")
            return True
        except Exception as e:
            print("net: ntp retry:", e)
            time.sleep(2)
    return False

def fetch():
    """Returns (current, daily, utc_offset_seconds) or (None, None, None)."""
    d = get_json(URL, timeout=20)       # shares get_json's Content-Length read
    if not d:
        return None, None, None
    try:
        return d["current"], d.get("daily"), d["utc_offset_seconds"]
    except Exception as e:
        print("net: weather failed:", e)
        return None, None, None


def get_json(url, timeout=15):
    """Fetch and parse a SMALL json document. Returns None on any failure.

    Reads into the shared buffer and NEVER calls r.json(). That helper ends up
    in raw.read() with no size, which builds one contiguous object the size of
    the response; against statsapi.mlb.com that cost 59 KB of ESP-IDF heap PER
    CALL and never gave it back, which is the heap mbedTLS needs for the
    markets handshake. Measured: 84 KB free before and after, versus 25 KB
    with r.json().

    This used to keep r.json() as a fallback for responses with no
    Content-Length, on the assumption those were rare. They are not - see the
    note below - and on 2026-09-07 that fallback took the markets tile out for
    a day. A body that will not fit is now reported and refused instead: a
    tile that keeps its last value is a far smaller loss than a heap that
    never recovers until the frame is rebooted.
    """
    _tick()
    if not online() and not connect():
        return None
    gc.collect()
    r = None
    try:
        import requests, json
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            print("net: http", r.status_code, url[:60])
            return None
        n = _len(r)
        # No Content-Length means a chunked response, and that is not rare:
        # open-meteo chunks every reply and ESPN's cache chunks some of them.
        # requests de-chunks as it reads, so readinto reports a genuine EOF
        # and the read stops on its own. (The over-read hang get_head warns
        # about needs a keep-alive socket with a known-short body; a response
        # with neither a length nor chunking is not legal HTTP.)
        capped = n is None
        if capped:
            n = BODY_MAX
        elif n > BODY_MAX:
            _note("oversize %d %s" % (n, url[-44:]))
            return None
        got = 0
        while got < n:
            k = r.raw.readinto(_bodymv[got:n])
            if not k:
                break
            got += k
        _blank(got)
        if capped and got >= BODY_MAX:
            # The buffer filled and the body may well continue, so what is in
            # it is a truncated document. Say so rather than parse garbage.
            _note("truncated %s" % url[-44:])
            return None
        return json.loads(_body)
    except Exception as e:
        print("net: get_json failed:", _err(e))
        return None
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass
        gc.collect()


def _len(r):
    """Content-Length, or None if the server did not send one. Header names
    keep the server's own casing, so match them case-insensitively."""
    try:
        for k, v in r.headers.items():
            if k.lower() == "content-length":
                return int(v)
    except Exception:
        pass
    return None

def _cap(r, nbytes):
    """nbytes, limited to what the body actually contains."""
    n = _len(r)
    return nbytes if n is None else min(nbytes, n)

def get_head(url, nbytes=600, timeout=15):
    """Read the first nbytes of a response into the shared buffer, then hang
    up. Returns how many bytes landed there, or None on failure; read them
    with net.body(). Nothing is allocated per call, which is the point - see
    the note on the buffer above.

    ESPN's event document is 8.5 KB but date/name/shortName all live in the
    first ~350 bytes, so there is no reason to pull - or parse - the rest.

    The read is clamped to Content-Length. A keep-alive server sends no EOF,
    so asking for more bytes than the body holds blocks until the socket
    gives up - about 150 s on a TLS connection, whose timeout MicroPython
    does not honour - and used to reboot the frame via the watchdog. Callers
    therefore cannot break this by passing an nbytes that is too big.

    A Range header asks the server to send only what we are going to read.
    That matters more than it looks: hanging up early does NOT stop the board
    RECEIVING the rest, and the ESP32 wifi driver grows its buffer pool to
    absorb a burst and does not shrink back. Those buffers come from the same
    ESP-IDF heap mbedTLS needs a 40 KB contiguous block in, which is why the
    heap collapsed on the KU football tile - three event documents at 9-14 KB
    each, of which this function was reading 600 bytes and binning the rest.
    Measured 2026-09-09: ESPN answers 206 with 600 bytes instead of 14159.
    Servers that ignore Range answer 200 with the whole body and the
    Content-Length clamp below handles them exactly as before.
    """
    _tick()
    if not online() and not connect():
        return None
    gc.collect()
    r = None
    try:
        import requests
        _phase("req")
        _RANGE_HDRS["Range"] = "bytes=0-%d" % (min(nbytes, BODY_MAX) - 1)
        r = requests.get(url, headers=_RANGE_HDRS, timeout=timeout)
        if r.status_code not in (200, 206):     # 206 is the range honoured
            print("net: http", r.status_code, url[:60])
            return None
        _phase("read")
        n = min(_cap(r, nbytes), BODY_MAX)
        got = 0
        while got < n:
            k = r.raw.readinto(_bodymv[got:n])
            if not k:
                break
            got += k
        _blank(got)
        _phase("done")
        return got
    except Exception as e:
        print("net: get_head failed:", _err(e))
        return None
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass
        gc.collect()

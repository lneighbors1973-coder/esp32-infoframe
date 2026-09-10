# Dow and Nasdaq levels for the info frame.
#
# Yahoo's chart endpoint is the only index quote source left that is keyless
# and answers a board like this one. Two things it needs, both learned the
# hard way on the hardware:
#
#   - A User-Agent. MicroPython's requests sends none and Yahoo replies 429
#     to every header-less request forever. net.HEADERS supplies one.
#   - HTTPS. Plain HTTP 301s to it. TLS is affordable on this board ONLY on
#     the custom firmware it now runs: stock MicroPython leaves too little
#     contiguous ESP-IDF heap and every handshake hangs 150 s. A handshake
#     costs ~2 s here. See photoframe-firmware in memory before reflashing.
#
# Alternatives were surveyed properly on 2026-09-09 and there are none. US
# index levels are licensed data: Twelve Data serves plain HTTP and lists 1292
# indices on its free plan, of which zero are American (^NDX answers "available
# starting with the Grow plan"). Everything else - Finnhub, FMP, Polygon, FRED,
# Stooq - redirects port 80 to HTTPS, so no source removes the TLS dependency
# anyway. ETF proxies were rejected on purpose: DIA near $450 is not the Dow at
# 45,000, and this frame is read by someone who cannot easily cross-check it.
#
# The numbers live in the "meta" object at the very front of a document that
# runs to several KB, so a 1.6 KB head read gets them, returns immediately,
# and never risks the over-read hang described in net.get_head.
import gc, time, net

URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=1d"
HEAD = 1600
SYMBOLS = (("DOW", "%5EDJI"), ("NASDAQ", "%5EIXIC"))

# ---------------------------------------------------------------- the heap trap
# mbedTLS does not allocate from MicroPython's heap. It mallocs from the
# ESP-IDF heap, which gc.mem_free() does not measure. So the frame can sit on
# 80 KB of free Python memory while the IDF heap has no block big enough for a
# handshake - reported as ENOMEM, or as a 150 s timeout with no error at all.
# Measure with esp32.idf_heap_info(esp32.HEAP_DATA), never with gc.mem_free().
#
# What takes that memory was misdiagnosed here for two days. The story used to
# be "MicroPython grows its GC heap out of the IDF pool and never gives it
# back", and the code below and in net.py and sports.py was shaped to avoid
# large allocations on that basis. Half right. MicroPython's split-heap
# auto-grow does take from the IDF pool, but it also releases an area once
# that area is entirely empty - measured across one collapse, gcheap went
# 137920 -> 135552 while idf fell 59368, so it was releasing, not growing.
#
# The real problem was that auto-grow ratchets: any transient spike enlarges
# the heap, and an area with one live object in it can never be handed back.
# The frame settled at a 135 KB heap holding a 53 KB working set, leaving
# mbedTLS 23552 contiguous bytes against a 40 KB need. Fixed by rebuilding the
# firmware - see the note on NEED below, and photoframe-firmware in memory.
#
# Avoiding large allocations is still worth doing, and the code here still
# does it. It just was not the cure.

# A handshake needs roughly this much contiguous ESP-IDF heap.
#
# It was 40000, measured on stock firmware: 40017 bytes in the largest block
# succeeded, 39904 failed. That figure was really ESP-IDF's default 16 kiB
# mbedTLS incoming record buffer plus overhead, and it could never be met -
# once MicroPython's heap has grown and WiFi has taken its share this board
# has 23552 contiguous bytes and no more.
#
# The firmware was rebuilt on 2026-09-09 with CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN
# at 8192 (from 16384), OUT at 4096, and CONFIG_MBEDTLS_DYNAMIC_BUFFER on, so
# the record buffers are allocated only while in use. The largest single
# allocation is now the 8 kiB incoming buffer; 12000 leaves room for the
# context and stack around it while still refusing an attempt that would
# stall for 150 s. Raise it if handshakes start hanging again.
NEED = 12000

def idf_free():
    """Free bytes and largest block in the heap mbedTLS actually uses."""
    try:
        import esp32
        rows = esp32.idf_heap_info(esp32.HEAP_DATA)
        return sum(r[1] for r in rows), max(r[2] for r in rows)
    except Exception:
        return -1, -1

HOST = "query1.finance.yahoo.com"

def reach():
    """Time a DNS lookup and a bare TCP connect to the quote host.

    The frame's fetch hangs for the full 150 s socket timeout inside
    requests.get(), which covers DNS, the TCP connect and the TLS handshake
    all at once - and the same call from a pasted script succeeds in under
    two seconds, 15 times out of 15. This splits the three apart. If DNS and
    the connect both come back in milliseconds, the hang is the handshake
    itself and nothing below it; if the connect stalls, it never gets that
    far. Cheap enough to run before every attempt, and it opens no TLS.
    """
    import socket
    t0 = time.ticks_ms()
    try:
        ai = socket.getaddrinfo(HOST, 443)[0][-1]
    except Exception as e:
        return "dns FAIL %s" % e
    t1 = time.ticks_ms()
    s = socket.socket()
    try:
        s.settimeout(10)                # honoured: no TLS on this socket
        s.connect(ai)
        return "dns=%d tcp=%d" % (time.ticks_diff(t1, t0),
                                  time.ticks_diff(time.ticks_ms(), t1))
    except Exception as e:
        return "dns=%d tcpFAIL %s" % (time.ticks_diff(t1, t0), e)
    finally:
        try:
            s.close()
        except Exception:
            pass

_NUMERIC = b"0123456789+-.eE"

def _num(buf, n, key):
    """Pull "key":<number> out of the first n bytes of net.body().

    Scans in place; decoding the buffer to a string first would allocate a
    copy the size of the response, which is what starves the handshake in the
    first place. See the heap note above.
    """
    if not n:
        return None
    m = ('"%s":' % key).encode()
    i = buf.find(m, 0, n)
    if i < 0:
        return None
    i += len(m)
    j = i
    while j < n and buf[j] in _NUMERIC:
        j += 1
    try:
        return float(bytes(buf[i:j]))
    except Exception:
        return None

def quotes():
    """[(name, level, change_percent), ...] or None if any symbol failed.

    All or nothing on purpose: one stale row beside one fresh row on the same
    tile would be read as a single consistent snapshot.
    """
    gc.collect()
    # Only attempt a handshake when there is room for one. Below NEED mbedTLS
    # either fails immediately or, worse, leaves the socket to time out for
    # 150 s with the frame stuck on one tile. Checking first turns that into
    # an instant no-op, so the tile keeps its last values and ages into the
    # OFFLINE badge.
    free, big = idf_free()
    if 0 <= big < NEED:             # a negative reading means "cannot tell"
        print("markets: skipped, idf heap largest block %d < %d" % (big, NEED))
        return None
    try:
        out = []
        for name, sym in SYMBOLS:
            # Collect before each handshake: a finished TLS session holds
            # ESP-IDF heap until its object is actually reclaimed, which is
            # not soon enough for the second symbol on its own.
            gc.collect()
            n = net.get_head(URL % sym, HEAD)
            buf = net.body()            # valid only until the next request
            price = _num(buf, n, "regularMarketPrice")
            if price is None:
                print("markets: no quote for %s (idf heap free %d largest %d)"
                      % ((name,) + idf_free()))
                return None
            chg = _num(buf, n, "regularMarketChangePercent")
            if chg is None:
                prev = _num(buf, n, "chartPreviousClose")
                if not prev:
                    return None
                chg = (price / prev - 1.0) * 100.0
            out.append((name, price, chg))
        return out
    finally:
        gc.collect()

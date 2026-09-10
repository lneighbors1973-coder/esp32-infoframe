# ESP32 Info Frame

A desk frame that cycles through large-type tiles — clock, weather, three-day
forecast, Royals / Chiefs / KU schedules and scores, and Dow / Nasdaq levels.

It is built for a viewer with macular degeneration. That single constraint
drives most of the design decisions in this repo, and it is worth stating
plainly before anything else: **this is not a photo frame**. Someone with a
central scotoma cannot resolve the thing they are looking directly at, so
photographs are close to useless to them while a two-inch-tall number in white
on black is not. Every design rule below follows from that.

---

## ⚠️ It needs custom firmware

**Stock MicroPython cannot run the markets tile.** On stock firmware the tile
fails *silently* — no exception, no error, just a 150-second hang and stale
numbers — so this is the single most important thing to know before flashing.

The board runs a custom **MicroPython v1.29.0** build with two changes:

**`ports/esp32/boards/sdkconfig.base`**
```
CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=8192      # from 16384
CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=4096
CONFIG_MBEDTLS_DYNAMIC_BUFFER=y             # plus the two DYNAMIC_FREE_* options
```

**`ports/esp32/mpconfigport.h`**
```c
#define MICROPY_GC_INITIAL_HEAP_SIZE  (104 * 1024)
#define MICROPY_GC_SPLIT_HEAP_AUTO    (0)
/* py/mpconfig.h only supplies these when SPLIT_HEAP_AUTO is on, so without
   defining them as malloc/free here, main.c will not compile. */
#define MP_PLAT_ALLOC_HEAP  ...
#define MP_PLAT_FREE_HEAP   ...
```

### Why

Stock MicroPython's split-heap auto-grow *ratchets*. Any transient spike
enlarges the GC heap, and an area holding even one live object can never be
handed back. The frame settled at a 135 KB heap serving a 53 KB working set,
which left mbedTLS **23,552 contiguous bytes against a ~40 KB need**. Every
TLS handshake then hung for the full socket timeout.

After the change: `gcheap 104000`, ESP-IDF free ~35,300, handshake ~2 s.

### 104 KiB is not a round number

This chip's usable DRAM is two regions — **84,592** and **113,840** bytes — and
the heap is a single `malloc`. 112 KiB (114,688) exceeds the larger region and
the board boot-loops on `mp_task_heap allocation failed`. 104 KiB fits, and
pins the heap inside that region so the whole 84,592-byte one stays free for
WiFi and TLS. **Do not raise it past ~110 KiB.**

### Building it

Windows cannot do this build. It fails twice over: MicroPython's qstr rule
generates a 76 KB command line against cmd.exe's 8,191-character limit, and
another rule is a `cat | sed | gcc | sed` pipeline. Use WSL.

```bash
. /opt/esp/esp-idf/export.sh          # ESP-IDF v5.5.2
cd /opt/esp/micropython/ports/esp32
idf.py -D MICROPY_BOARD=ESP32_GENERIC build
```

From Git Bash, `export MSYS_NO_PATHCONV=1` first or it rewrites `/opt/...`
into `C:/Program Files/Git/opt/...`.

Flashing **only `0x10000`** (the app) leaves the filesystem intact — the
`.mpy` files, `secrets.py`, `mode.txt` and `tzoff.txt` all survive.

> Firmware images and the pre-change flash dump are excluded from git (see
> `.gitignore`) and are not in this repo.

---

## Hardware

ESP32-D0WD-V3 rev 3.1 · 4 MB flash · **no PSRAM** · run at 240 MHz.

| Peripheral | Bus | Pins |
|---|---|---|
| ILI9341 display | SPI1 | `SCK 14` `MOSI 13` `MISO 12` `CS 15` `DC 2` `BL 21` |
| XPT2046 touch | SoftSPI | `SCK 25` `MOSI 32` `MISO 39` `CS 33` `IRQ 36` |
| Status LED (common anode) | — | `R 22` (PWM) `G 16` `B 17` |

Display `MADCTL = 0x28`. All pins were verified empirically on this board, not
taken from documentation — the red LED channel in particular is on **GPIO 22**,
not GPIO 4 as this board is usually documented. GPIO 22 is also labelled `CS`
on the SPI expansion header, which nothing here uses.

**Touch axes are transposed** relative to the display; the panel is natively
portrait. Calibration constants live at the top of `frame.py` (5-point fit,
mean residual 2.4 px). To re-derive them after a panel swap, run `calib.py` on
the board and `fit.py` on the host.

Measured: full-screen paint 66 ms, tile ~75 ms.

---

## Using it

**At boot**, the frame offers light or dark for 8 seconds. Tap *anywhere* to
toggle — the screen is already drawn in the palette being offered, so the
choice is the thing you are looking at. Each tap restarts the countdown, so
there is no rush. The answer persists in `mode.txt`.

Which polarity suits a given person depends on their glare sensitivity. It is
a real choice, not a preference.

**While running**, the screen is three touch zones:

| Zone | Action |
|---|---|
| Left third | previous tile |
| Right third | next tile |
| Middle third | pause / resume (LED blinks 1× paused, 2× resumed) |

Tiles advance every 15 s in this order:

```
time · royals_last · royals_next · chiefs_last · chiefs_next
time · weather · forecast · ku_fb · ku_bb · markets
```

`time` appears twice on purpose — it is the tile most likely to be wanted at a
glance, so the rotation never leaves it more than five slides away.

### Display rules

These are applied throughout `tile.py` and are not stylistic:

- Maximum luminance contrast — white or yellow on pure black. No greys, no
  gradients. AMD degrades contrast sensitivity as well as acuity.
- Very large glyphs, strokes thickened ~1.3×.
- One idea per tile. Clutter competes with a central scotoma.
- Colour is never the only cue; size, position and rules carry meaning too.
- The bottom 24 px is always clear for the `OFFLINE` badge, so each painter
  needs one layout instead of a stale-data variant.

---

## Data sources

| Tile | Source | Transport |
|---|---|---|
| Weather, forecast | Open-Meteo | HTTP |
| Royals | MLB StatsAPI (team 118) | HTTP |
| Chiefs, KU football, KU basketball | ESPN core API | HTTP |
| Dow, Nasdaq | Yahoo chart (`^DJI`, `^IXIC`) | **HTTPS** |

Plain HTTP is deliberate where it is used. Each avoided handshake saves a
second of latency and a scarce contiguous block; no credentials are sent and
every payload is public, so the only exposure is LAN-level spoofing.

Markets is HTTPS because its source offers nothing else. Yahoo's chart endpoint
is the only keyless index-quote source that answers a board like this — it
needs a `User-Agent` (header-less requests get 429 forever) and it 301s port 80
to HTTPS. US index levels are licensed data; alternatives were surveyed
properly and there are none. **ETF proxies were rejected on purpose**: DIA near
$450 is not the Dow at 45,000, and a plausible wrong number is worse than a
stale one for a viewer who cannot easily cross-check it.

Refresh intervals: weather and markets 15 min, schedules 30 min, a game in
progress 5 min, NTP 6 h. Failures back off (markets to a 30 min ceiling) and
the frame keeps showing stale data behind an `OFFLINE` badge rather than
blanking.

---

## The memory rule

Everything unusual about this codebase traces to one fact:

> **mbedTLS does not allocate from MicroPython's heap.** It `malloc`s from the
> ESP-IDF heap, which `gc.mem_free()` cannot see.

The frame can sit on 80 KB of free Python memory while the IDF heap has no
block large enough for a handshake. Measure with
`esp32.idf_heap_info(esp32.HEAP_DATA)` — **never** with `gc.mem_free()`.

Consequences, all of them load-bearing:

- `tile.STRIP = 8` rows (5 KB). At 48 rows the markets fetch hung 150 s; at 24
  it failed instantly with ENOMEM — both with 75–80 KB still "free".
- `net.get_head()` sends `Range: bytes=0-N`. ESPN was sending 14,159 bytes to
  have 600 read.
- `markets.HEAD = 1600` — the numbers live in a `meta` object at the very front
  of a multi-KB document.
- `main.py` is one `import`. MicroPython compiles any `.py` it imports at run
  time, and the heap that compilation leaves behind is fragmented enough to
  deny mbedTLS its block. Everything else ships as `.mpy`.
- `markets.NEED = 12000`, lowered from 40000 to match the smaller TLS buffers.
  Leaving it at 40000 makes the guard skip every attempt *even on good
  firmware*.

---

## Deploying

Every module ships as precompiled bytecode; only `main.py` stays as source so
MicroPython auto-runs it.

```bash
cd C:/Users/lneig/Documents/photoframe && for f in tile sports net markets frame ili9341 xpt2046; do uvx mpy-cross -o "$f.mpy" "$f.py" && uvx mpremote connect COM3 fs cp "$f.mpy" ":$f.mpy"; done && uvx mpremote connect COM3 fs cp main.py : && rm -f *.mpy
```

Wi-Fi credentials are not in this repo:

```bash
cp secrets.example.py secrets.py       # then fill in SSID and password
uvx mpremote connect COM3 fs cp secrets.py :secrets.py
```

### Two gotchas that each cost hours

- **`mpremote run` and `fs ls` interrupt the running `main.py`** and leave the
  board at a bare REPL with nothing driving the display. A blank screen after
  debugging is usually just that — `mpremote connect COM3 reset repl` restarts
  the frame.
- **Never suppress mpremote's output when copying.** A silently failed `fs cp`
  looks exactly like a code bug in the next test run.

---

## Debugging

`mpremote run` **soft-reboots MicroPython before executing**. Every bench test
therefore runs against a freshly initialised heap with the big region free —
which is why a call can succeed 15/15 on the bench and never once in the
running frame. This cost two days.

Passive `pyserial` reading of the board's own console is the only
non-perturbing view. Use it as the source of truth.

The frame prints a memory line on every tile change:

```
[markets] free N alloc N gcheap N, idf N big N
```

`gcheap` is the one that settles arguments — if MicroPython grew its GC heap
out of the ESP-IDF pool, that total rises by the same amount the IDF heap
loses. If it stays flat while `idf` falls, the memory went somewhere else.

Markets outcomes are also appended to `markets.log` **on the board** (capped at
16 KB, rotated by deletion), so intermittent failures can be caught without
leaving a console attached for days. `net.last_phase` distinguishes `req` (DNS,
TCP connect, TLS handshake) from `read` (the body) — they want different fixes.

A hung fetch reboots the frame via a 240 s watchdog rather than freezing it.

---

## Files

| | |
|---|---|
| `main.py` | entry point, one import — see the memory rule |
| `frame.py` | tile rotation, touch, boot choosers, watchdog, logging |
| `tile.py` | all painters and the low-vision display rules |
| `net.py` | WiFi, NTP, weather, and the shared HTTP helpers |
| `sports.py` | MLB StatsAPI and ESPN feeds |
| `markets.py` | Yahoo index quotes, and the heap guard around them |
| `ili9341.py` `xpt2046.py` | display and touch drivers |
| `boot.py` | sets 240 MHz |
| `calib.py` `fit.py` | touch calibration (on board / on host) |
| `secrets.example.py` | template — copy to `secrets.py`, which is gitignored |
| `backup/` | superseded sources and captured device state |

On-board files not in this repo: `secrets.py`, `mode.txt`, `tzoff.txt`,
`markets.log`.

---

## Notes for the next person

Most of the comments in this codebase record *why*, including negative
results — why ETF proxies were rejected, why 104 KiB and not 112, why
`MKT_RETRY` moved back from 900 to 300 after 16 hours of measurement. That was
expensive to write and it is the most valuable thing here. Please keep the
habit; a tuning constant with no rationale gets re-derived or broken.

# Touch calibration: tap each white square as it appears.
from machine import Pin, SPI
import time, json
from ili9341 import ILI9341
from xpt2046 import XPT2046

spi = SPI(1, baudrate=40000000, polarity=0, phase=0,
          sck=Pin(14), mosi=Pin(13), miso=Pin(12))
bl  = Pin(21, Pin.OUT, value=1)
tft = ILI9341(spi, cs=15, dc=2, madctl=0x28)
ts  = XPT2046()

WHITE, GREEN, BLACK = 0xFFFF, 0x07E0, 0x0000

def rect(x, y, w, h, colour):
    row = bytearray(w * 2)
    hi, lo = colour >> 8, colour & 0xFF
    for i in range(0, len(row), 2):
        row[i], row[i+1] = hi, lo
    tft.window(x, y, x + w - 1, y + h - 1)
    for _ in range(h):
        tft.blit(row)
    tft.end()

def wait_release():
    t0 = time.ticks_ms()
    while ts.pressed() and time.ticks_diff(time.ticks_ms(), t0) < 5000:
        time.sleep_ms(20)
    time.sleep_ms(250)

TARGETS = [(30, 30), (290, 30), (290, 210), (30, 210), (160, 120)]
S = 9                       # half-size of the target square

tft.fill(BLACK)
print("=== TOUCH CALIBRATION ===")
print("tap each white square as it appears")
results = []

for i, (tx, ty) in enumerate(TARGETS):
    wait_release()
    rect(tx - S, ty - S, S*2, S*2, WHITE)
    print("target %d/%d at screen (%d,%d) - tap it" % (i+1, len(TARGETS), tx, ty))
    raw, t0 = None, time.ticks_ms()
    while raw is None:
        if time.ticks_diff(time.ticks_ms(), t0) > 90000:
            print("  TIMEOUT - skipping")
            break
        raw = ts.raw()
        time.sleep_ms(20)
    if raw:
        results.append({"sx": tx, "sy": ty, "rx": raw[0], "ry": raw[1]})
        print("  got raw x=%d y=%d" % raw)
        rect(tx - S, ty - S, S*2, S*2, GREEN)
        time.sleep_ms(350)
    rect(tx - S, ty - S, S*2, S*2, BLACK)

try:
    with open("/sd/touchcal.json", "w") as f:
        json.dump(results, f)
    print("saved /sd/touchcal.json with %d points" % len(results))
except Exception as e:
    print("could not save:", e)

print("=== RESULTS ===")
for r in results:
    print("  screen(%3d,%3d)  raw(%4d,%4d)" % (r["sx"], r["sy"], r["rx"], r["ry"]))
print("=== CALIBRATION DONE ===")
tft.fill(0x001F)            # blue = finished

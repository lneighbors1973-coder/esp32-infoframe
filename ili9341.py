from machine import Pin, SPI
import time

class ILI9341:
    def __init__(self, spi, cs, dc, rst=None, bl=None,
                 width=320, height=240, madctl=0x28, invert=False):
        self.spi = spi
        self.cs  = Pin(cs, Pin.OUT, value=1)
        self.dc  = Pin(dc, Pin.OUT, value=0)
        self.rst = Pin(rst, Pin.OUT, value=1) if rst is not None else None
        self.bl  = Pin(bl, Pin.OUT, value=1) if bl is not None else None
        self.width, self.height = width, height
        self.reset()
        self.init(madctl, invert)

    def reset(self):
        if self.rst:
            self.rst(0); time.sleep_ms(20); self.rst(1); time.sleep_ms(150)

    def _cmd(self, c, data=None):
        self.cs(0); self.dc(0)
        self.spi.write(bytes([c]))
        if data:
            self.dc(1); self.spi.write(bytes(data))
        self.cs(1)

    def init(self, madctl, invert):
        self._cmd(0x01); time.sleep_ms(150)      # soft reset
        self._cmd(0x11); time.sleep_ms(120)      # sleep out
        self._cmd(0x3A, b'\x55')                 # 16-bit colour
        self._cmd(0x36, bytes([madctl]))         # orientation / RGB order
        self._cmd(0x21 if invert else 0x20)      # inversion
        self._cmd(0x13)                          # normal display mode
        self._cmd(0x29); time.sleep_ms(50)       # display on

    def window(self, x0, y0, x1, y1):
        self._cmd(0x2A, bytes([x0>>8, x0&0xFF, x1>>8, x1&0xFF]))
        self._cmd(0x2B, bytes([y0>>8, y0&0xFF, y1>>8, y1&0xFF]))
        self.cs(0); self.dc(0); self.spi.write(b'\x2C'); self.dc(1)
        # caller streams pixels, then calls end()

    def end(self):
        self.cs(1)

    def blit(self, buf):
        self.spi.write(buf)

    def fill(self, colour):
        row = bytearray(self.width * 2)
        hi, lo = colour >> 8, colour & 0xFF
        for i in range(0, len(row), 2):
            row[i], row[i+1] = hi, lo
        self.window(0, 0, self.width-1, self.height-1)
        for _ in range(self.height):
            self.spi.write(row)
        self.end()

from machine import Pin, SoftSPI
import time

class XPT2046:
    """Resistive touch controller. Separate soft-SPI bus - both hardware
    SPI peripherals are already taken by the display and the SD card."""
    CMD_X = 0xD0          # A2..A0 = 101  -> X position, 12-bit, DFR
    CMD_Y = 0x90          # A2..A0 = 001  -> Y position
    CMD_Z1 = 0xB0
    CMD_Z2 = 0xC0

    SAMPLES = 5

    def __init__(self, sck=25, mosi=32, miso=39, cs=33, irq=36, baudrate=1000000):
        self.spi = SoftSPI(baudrate=baudrate, polarity=0, phase=0,
                           sck=Pin(sck), mosi=Pin(mosi), miso=Pin(miso))
        self.cs  = Pin(cs, Pin.OUT, value=1)
        self.irq = Pin(irq, Pin.IN) if irq is not None else None
        self._rx = bytearray(2)
        # Everything this class needs per reading is allocated once, here.
        # raw() runs ~33 times a second forever; when it built its command
        # bytes and sample lists on the fly it made about 6000 short-lived
        # objects per tile, and the holes they left fragmented the heap until
        # MicroPython could not find 2.7 KB for a socket read. It answered by
        # growing the GC heap out of the ESP-IDF heap, which is where mbedTLS
        # gets the memory for the markets handshake. See markets.py.
        self._tx_x = bytes([self.CMD_X])
        self._tx_y = bytes([self.CMD_Y])
        self._xs = [0] * self.SAMPLES
        self._ys = [0] * self.SAMPLES

    def _read(self, tx):
        self.cs(0)
        self.spi.write(tx)
        self.spi.readinto(self._rx)
        self.cs(1)
        return ((self._rx[0] << 8) | self._rx[1]) >> 3      # 12-bit result

    def pressed(self):
        return self.irq.value() == 0 if self.irq else True

    def raw(self):
        """Median-filtered raw reading. Returns (x, y) or None if not touched."""
        if not self.pressed():
            return None
        xs, ys = self._xs, self._ys
        for i in range(self.SAMPLES):
            ys[i] = self._read(self._tx_y)
            xs[i] = self._read(self._tx_x)
        xs.sort(); ys.sort()                    # in place, no new list
        x, y = xs[self.SAMPLES // 2], ys[self.SAMPLES // 2]
        if x < 100 or y < 100 or x > 4000 or y > 4000:
            return None
        return x, y

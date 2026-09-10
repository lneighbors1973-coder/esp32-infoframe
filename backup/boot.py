import machine
machine.freq(240000000)
from machine import Pin, SPI
import sdcard, os
try:
    import vfs as _v
except ImportError:
    import os as _v
try:
    _spi = SPI(2, baudrate=20000000, polarity=0, phase=0,
               sck=Pin(18), mosi=Pin(23), miso=Pin(19))
    _sd = sdcard.SDCard(_spi, Pin(5, Pin.OUT), baudrate=20000000)
    _v.mount(_sd, "/sd")
    print("SD mounted at /sd")
except Exception as e:
    print("SD mount failed:", e)

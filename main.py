# Entry point only. The frame itself lives in frame.py, which is shipped to
# the board precompiled as frame.mpy: MicroPython compiles any .py it imports
# at run time, and the heap that compilation leaves behind is fragmented
# enough to deny mbedTLS the contiguous block a TLS handshake needs. Keeping
# main.py to one line keeps that heap as clean as it can be.
import frame

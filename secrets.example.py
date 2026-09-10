# Wi-Fi credentials for the info frame. TEMPLATE - copy, do not edit in place:
#
#   cp secrets.example.py secrets.py
#
# then put your own network details in secrets.py and push it to the board:
#
#   uvx mpremote connect COM3 fs cp secrets.py :secrets.py
#
# secrets.py is gitignored and never leaves your machine. This file is the
# only record in the repo of what net.py expects, so keep the two names.
#
# Keep the quotes. The board only needs 2.4 GHz - it has no 5 GHz radio.

WIFI_SSID = "your-network-name"
WIFI_PASS = "your-network-password"

# Wi-Fi, NTP and weather for the photo frame. All failures are non-fatal.
import network, time, gc

LAT, LON = 38.88, -94.82               # Olathe, KS (66062)
# Plain HTTP on purpose: mbedTLS needs ~34 KB of heap that main.py does not
# have spare (MBEDTLS_ERR_MD_ALLOC_FAILED). No credentials are sent and the
# payload is public weather data, so the only exposure is LAN-level spoofing.
URL = ("http://api.open-meteo.com/v1/forecast"
       "?latitude=38.88&longitude=-94.82"
       "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
       "weather_code,wind_speed_10m,wind_direction_10m"
       "&daily=temperature_2m_max,temperature_2m_min&forecast_days=1"
       "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=America%2FChicago")

_wlan = None
_last_attempt = -99999
RETRY_COOLDOWN = 60         # do not retry a dead network more than this often

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
    if not online() and not connect():
        return None, None, None
    gc.collect()
    r = None
    try:
        import requests
        r = requests.get(URL, timeout=20)
        if r.status_code != 200:
            print("net: http", r.status_code)
            return None, None, None
        d = r.json()
        return d["current"], d.get("daily"), d["utc_offset_seconds"]
    except Exception as e:
        print("net: weather failed:", e)
        return None, None, None
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass
        gc.collect()


def get_json(url, timeout=20):
    """Fetch and parse a SMALL json document. Returns None on any failure."""
    if not online() and not connect():
        return None
    gc.collect()
    r = None
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            print("net: http", r.status_code, url[:60])
            return None
        return r.json()
    except Exception as e:
        print("net: get_json failed:", e)
        return None
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass
        gc.collect()


def get_head(url, nbytes=600, timeout=20):
    """Read only the first nbytes of a response, then hang up.

    ESPN's event document is 8.5 KB but date/name/shortName all live in the
    first ~350 bytes, so there is no reason to pull - or parse - the rest.
    """
    if not online() and not connect():
        return None
    gc.collect()
    r = None
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.raw.read(nbytes)
    except Exception as e:
        print("net: get_head failed:", e)
        return None
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass
        gc.collect()

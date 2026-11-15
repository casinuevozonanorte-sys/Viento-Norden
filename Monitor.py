"""Avisa por ntfy cuando el viento en Norden (CARP) o en San Isidro (club) llega a 15 nudos.

Norden: abre la pagina de CARP con un navegador sin pantalla y lee la rosa de
vientos (color = rango de nudos, orientacion = direccion).
San Isidro: lee la ultima medicion del canal publico de ThingSpeak que usa la
pagina del Club Nautico San Isidro (estacion del SMN).
Cada aviso muestra las dos estaciones y el pronostico de las proximas 12 horas
(Open-Meteo, modelo abierto).
"""
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

# --- Configuracion general ---
TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_NOTIFY = os.environ.get("TEST_NOTIFY") == "1"
STATE_FILE = "state.txt"
FAIL_LIMIT = 3      # lecturas fallidas seguidas antes de avisar
LOWS_TO_CLEAR = 2   # lecturas bajas seguidas para avisar que bajo el viento
ALERT_KNOTS = 15.0
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
KEYS = ["norden", "si"]
NAMES = {"norden": "Norden", "si": "San Isidro"}
LOCAL_TZ = timezone(timedelta(hours=-3))  # Argentina, sin horario de verano

# --- Norden (CARP) ---
URL = "https://www.comisionriodelaplata.org/servicios_main.php?sid=VM"
MIN_PIXELS = int(os.environ.get("MIN_PIXELS", "300"))
TOL = 24
BINS = {
    "<5": (65, 125, 205),
    "5-10": (16, 26, 62),
    "10-15": (140, 185, 45),
    "15-20": (139, 17, 13),
    "20-25": (70, 165, 210),
    "25-30": (70, 40, 110),
    ">30": (232, 152, 78),
}
ALERT_BINS = ["15-20", "20-25", "25-30", ">30"]

# --- San Isidro (ThingSpeak) ---
FEED_URL = "https://api.thingspeak.com/channels/1506798/feeds/last.json"
SPEED_FIELD = "field3"   # Velocidad viento (nudos)
DIR_FIELD = "field4"     # Direccion viento (grados)
MAX_AGE_MIN = 60         # minutos maximos de antiguedad del dato

# --- Pronostico (Open-Meteo): latitud, longitud (aproximadas) ---
FORECAST_POINTS = {
    "norden": (-34.64, -57.93),
    "si": (-34.46, -58.51),
}
FORECAST_HOURS = 12


def notify(msg):
    if not TOPIC:
        print("Sin NTFY_TOPIC, no se envia:", msg)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=msg.encode("utf-8"),
        headers={"Title": "Viento Norden y San Isidro", "Priority": "high",
                 "Tags": "wind_face"},
    )
    urllib.request.urlopen(req, timeout=20)


def compass(deg):
    return COMPASS[int(round((deg % 360) / 22.5)) % 16]


# ---------------- Norden ----------------
def open_norden(page):
    for frame in page.frames:
        try:
            label = frame.get_by_text("Norden", exact=True).first
            label.wait_for(state="attached", timeout=3000)
            box = label.bounding_box()
            if not box:
                continue
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] - 12)
            page.wait_for_timeout(3000)
            return True
        except Exception:
            pass
    return False


def click_wind(page):
    for frame in page.frames:
        try:
            frame.get_by_text("Wind", exact=True).first.click(timeout=2000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            pass
    return False


def locate_region(page):
    for frame in page.frames:
        try:
            ref = frame.get_by_text("5-10 knots", exact=True).first
            ref.wait_for(state="attached", timeout=3000)
            box = ref.bounding_box()
            if not box:
                continue
            sx = page.evaluate("window.scrollX")
            sy = page.evaluate("window.scrollY")
            bx, by = box["x"] + sx, box["y"] + box["height"] / 2 + sy
            return (max(0, int(bx - 340)), max(0, int(by - 100)),
                    int(bx - 30), int(by + 130))
        except Exception:
            pass
    return None


def capture():
    os.makedirs("out", exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)
        print("Marcador Norden tocado:", open_norden(page))
        print("Pestana Wind tocada:", click_wind(page))
        region = locate_region(page)
        print("Rosa localizada:", region)
        page.screenshot(path="out/chart.png", full_page=True)
        browser.close()
    return region


def wedge_direction(mask):
    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        return None
    pts = np.stack([xs, ys], axis=1).astype(float)
    q = pts - pts.mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov(q.T))
    u = vecs[:, 1]
    t = q @ u
    p = q @ np.array([-u[1], u[0]])
    lo = t <= np.percentile(t, 15)
    hi = t >= np.percentile(t, 85)
    if p[lo].std() > p[hi].std():
        u = -u
    return math.degrees(math.atan2(u[0], -u[1])) % 360


def read_rose(region):
    img = Image.open("out/chart.png").convert("RGB").crop(region)
    img.save("out/rose.png")
    arr = np.array(img).astype(int)
    result = {}
    for name, rgb in BINS.items():
        close = np.abs(arr - np.array(rgb)).max(axis=2) <= TOL
        eroded = Image.fromarray((close * 255).astype("uint8")).filter(ImageFilter.MinFilter(3))
        mask = np.array(eroded) > 0
        result[name] = (int(mask.sum()), mask)
    return result


def get_norden():
    """Devuelve {'high': bool, 'desc': str} o None si no se pudo leer."""
    try:
        region = capture()
        if region is None:
            return None
        result = read_rose(region)
    except Exception as exc:
        print("Error leyendo Norden:", exc)
        return None
    print("Pixeles por rango:", {k: v[0] for k, v in result.items()})
    active = {b: result[b][0] for b in BINS if result[b][0] >= MIN_PIXELS}
    if not active:
        return None
    hits = {b: n for b, n in active.items() if b in ALERT_BINS}
    pick = max(hits, key=hits.get) if hits else max(active, key=active.get)
    ang = wedge_direction(result[pick][1])
    desc = f"{pick} nudos"
    if ang is not None:
        desc += f", del {compass(ang)}"
    return {"high": bool(hits), "desc": desc}


# ---------------- San Isidro ----------------
def get_san_isidro():
    """Devuelve {'high': bool, 'desc': str} o None si no se pudo leer."""
    try:
        with urllib.request.urlopen(FEED_URL, timeout=20) as resp:
            feed = json.load(resp)
        speed = float(feed[SPEED_FIELD])
        raw_dir = feed.get(DIR_FIELD)
        deg = float(raw_dir) if raw_dir not in (None, "") else None
        when = datetime.fromisoformat(feed["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - when).total_seconds() / 60
    except Exception as exc:
        print("Error leyendo San Isidro:", exc)
        return None
    print("San Isidro:", speed, deg, "| edad (min):", round(age))
    if age > MAX_AGE_MIN:
        print("Dato demasiado viejo, se ignora")
        return None
    desc = f"{speed:g} nudos"
    if deg is not None:
        desc += f", del {compass(deg)}"
    return {"high": speed >= ALERT_KNOTS, "desc": desc}


# ---------------- Pronostico ----------------
def get_forecast(lat, lon):
    """Filas (hora local, nudos, rafagas, direccion) de las proximas horas."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&hourly=wind_speed_10m,wind_gusts_10m,wind_direction_10m"
           "&wind_speed_unit=kn&timezone=America%2FArgentina%2FBuenos_Aires"
           "&forecast_days=2")
    with urllib.request.urlopen(url, timeout=20) as resp:
        hourly = json.load(resp)["hourly"]
    now = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    end = now + timedelta(hours=FORECAST_HOURS)
    rows = []
    for t, s, g, d in zip(hourly["time"], hourly["wind_speed_10m"],
                          hourly["wind_gusts_10m"], hourly["wind_direction_10m"]):
        when = datetime.fromisoformat(t)
        if now <= when < end and s is not None:
            rows.append((when, s, g, d))
    return rows


def forecast_text(name, rows):
    if not rows:
        return f"{name}: sin pronostico"
    speeds = [r[1] for r in rows]
    gusts = [r[2] for r in rows if r[2] is not None]
    peak = max(rows, key=lambda r: r[1])
    text = f"{name}: {min(speeds):.0f}-{max(speeds):.0f} nudos"
    if gusts:
        text += f", rafagas hasta {max(gusts):.0f}"
    text += f", pico {peak[1]:.0f} a las {peak[0].hour}h"
    if peak[3] is not None:
        text += f" del {compass(peak[3])}"
    over = [r for r in rows if r[1] >= ALERT_KNOTS]
    if over:
        text += f"; 15 o mas desde las {over[0][0].hour}h"
    else:
        text += "; sin 15 nudos"
    return text


def forecast_block():
    lines = []
    for key in KEYS:
        try:
            rows = get_forecast(*FORECAST_POINTS[key])
            lines.append(forecast_text(NAMES[key], rows))
        except Exception as exc:
            print("Error de pronostico", key, exc)
            lines.append(f"{NAMES[key]}: sin pronostico")
    return f"Pronostico proximas {FORECAST_HOURS} h (modelo):\n" + "\n".join(lines)


# ---------------- Estado y avisos ----------------
def load_state():
    state = {k: {"state": "low", "fails": 0, "lows": 0} for k in KEYS}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k in KEYS:
            state[k].update(data.get(k, {}))
    except Exception:
        pass  # archivo ausente o con el formato anterior
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def status_lines(readings):
    return "\n".join(
        f"{NAMES[k]}: {readings[k]['desc'] if readings[k] else 'sin lectura'}" for k in KEYS
    )


def main():
    if TEST_NOTIFY:
        notify("Prueba: el aviso de viento funciona.")

    state = load_state()
    readings = {"norden": get_norden(), "si": get_san_isidro()}

    if TEST_NOTIFY:
        notify("Prueba - lectura actual:\n" + status_lines(readings)
               + "\n\n" + forecast_block())

    events = []
    for key in KEYS:
        st, r = state[key], readings[key]
        if r is None:
            st["fails"] += 1
            print(NAMES[key], "- lectura fallida seguida numero", st["fails"])
            if st["fails"] == FAIL_LIMIT:
                events.append(f"No puedo leer {NAMES[key]} desde hace unos 45 minutos.")
            continue
        st["fails"] = 0
        if r["high"]:
            st["lows"] = 0
            if st["state"] != "high":
                st["state"] = "high"
                events.append(f"{NAMES[key]} subio a 15 nudos o mas.")
        elif st["state"] == "high":
            st["lows"] += 1
            if st["lows"] >= LOWS_TO_CLEAR:
                st["state"], st["lows"] = "low", 0
                events.append(f"{NAMES[key]} bajo de 15 nudos.")

    if events:
        notify("\n".join(events) + "\n" + status_lines(readings)
               + "\n\n" + forecast_block())

    print("Estado:", state)
    save_state(state)


if __name__ == "__main__":
    main()

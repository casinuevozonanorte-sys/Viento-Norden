"""Avisa por ntfy cuando el viento en Pilote Norden (CARP) llega a 15 nudos o mas.

Abre la pagina de CARP con un navegador sin pantalla, toca el marcador Norden y
la pestana Wind, saca una captura y lee la rosa de vientos: el color de la cuña
indica el rango de nudos y su orientacion indica la direccion.
Tambien avisa cuando el viento baja de 15 nudos y si el script no logra leer.
"""
import math
import os
import urllib.request

import numpy as np
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

URL = "https://www.comisionriodelaplata.org/servicios_main.php?sid=VM"
TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_NOTIFY = os.environ.get("TEST_NOTIFY") == "1"
MIN_PIXELS = int(os.environ.get("MIN_PIXELS", "300"))
TOL = 24
FAIL_LIMIT = 3   # lecturas fallidas seguidas antes de avisar
LOWS_TO_CLEAR = 2  # lecturas bajas seguidas para avisar que bajo el viento

# Colores de la leyenda (aproximados, tomados de las capturas)
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
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def notify(msg):
    if not TOPIC:
        print("Sin NTFY_TOPIC, no se envia:", msg)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=msg.encode("utf-8"),
        headers={"Title": "Viento Norden", "Priority": "high", "Tags": "wind_face"},
    )
    urllib.request.urlopen(req, timeout=20)


def open_norden(page):
    # Toca el marcador "Norden": el pin queda justo arriba de su etiqueta
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
    """Zona de la pagina donde esta la rosa, tomando como guia la leyenda."""
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


def direction(mask):
    """Angulo (grados desde el norte, horario) hacia donde apunta la cuña."""
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
        u = -u  # el extremo angosto es el centro de la rosa
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


def describe(name, mask):
    ang = direction(mask)
    text = f"{name} nudos"
    if ang is not None:
        text += f", del {COMPASS[int(round(ang / 22.5)) % 16]}"
    return text


def load_state():
    """Devuelve (estado, fallos seguidos, lecturas bajas seguidas)."""
    try:
        parts = open("state.txt").read().split()
    except FileNotFoundError:
        parts = []
    state = parts[0] if parts and parts[0] in ("low", "high") else "low"
    nums = [int(x) for x in parts[1:3] if x.isdigit()]
    nums += [0] * (2 - len(nums))
    return state, nums[0], nums[1]


def save_state(state, fails, lows):
    with open("state.txt", "w") as f:
        f.write(f"{state} {fails} {lows}")


def main():
    if TEST_NOTIFY:
        notify("Prueba: el aviso de viento funciona.")

    state, fails, lows = load_state()

    try:
        region = capture()
    except Exception as exc:
        print("Error al capturar:", exc)
        region = None

    result, active = {}, {}
    if region is not None:
        result = read_rose(region)
        print("Pixeles por rango:", {k: v[0] for k, v in result.items()})
        active = {b: result[b][0] for b in BINS if result[b][0] >= MIN_PIXELS}

    dominant = max(active, key=active.get) if active else None

    if TEST_NOTIFY:
        if dominant:
            notify("Prueba - lectura actual: " + describe(dominant, result[dominant][1]))
        else:
            notify("Prueba: no pude leer la rosa de vientos.")

    # Lectura fallida: se cuenta y se avisa una sola vez por racha
    if dominant is None:
        fails += 1
        print("Lectura fallida seguida numero", fails)
        if fails == FAIL_LIMIT:
            notify("No puedo leer el viento del Norden desde hace unos 45 minutos. "
                   "Revisa la pagina de CARP o el script.")
        save_state(state, fails, lows)
        return

    fails = 0
    alert_hits = {b: n for b, n in active.items() if b in ALERT_BINS}

    if alert_hits:
        lows = 0
        if state != "high":
            best = max(alert_hits, key=alert_hits.get)
            notify("Viento en Pilote Norden: " + describe(best, result[best][1]))
            state = "high"
    elif state == "high":
        lows += 1
        if lows >= LOWS_TO_CLEAR:
            notify("Viento en Pilote Norden bajo de 15 nudos: ahora "
                   + describe(dominant, result[dominant][1]))
            state = "low"
            lows = 0

    print("Estado:", state, "| fallos:", fails, "| lecturas bajas:", lows)
    save_state(state, fails, lows)


if __name__ == "__main__":
    main()

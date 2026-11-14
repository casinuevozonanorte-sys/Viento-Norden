"""Avisa por ntfy cuando el viento en Pilote Norden (CARP) llega a 15 nudos o mas.

Abre la pagina de CARP con un navegador sin pantalla, saca una captura y
detecta el color de la rosa de vientos (los colores de la leyenda corresponden
a rangos de nudos). La primera corrida sirve para calibrar los colores.
"""
import os
import urllib.request

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

URL = "https://www.comisionriodelaplata.org/servicios_main.php?sid=VM"
TOPIC = os.environ.get("NTFY_TOPIC", "")
TEST_NOTIFY = os.environ.get("TEST_NOTIFY") == "1"
MIN_PIXELS = int(os.environ.get("MIN_PIXELS", "600"))
TOL = 28

# Colores de la leyenda (aproximados: a calibrar con la primera corrida)
BINS = {
    "<5": (65, 125, 205),
    "5-10": (25, 30, 70),
    "10-15": (140, 185, 45),
    "15-20": (139, 17, 13),
    "20-25": (70, 165, 210),
    "25-30": (70, 40, 110),
    ">30": (232, 152, 78),
}
ALERT_BINS = ["15-20", "20-25", "25-30", ">30"]


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


def click_wind(page):
    # El widget esta dentro de un iframe: se prueba en cada marco de la pagina
    for frame in page.frames:
        try:
            frame.get_by_text("Wind", exact=True).first.click(timeout=2000)
            page.wait_for_timeout(1500)
            return
        except Exception:
            pass


def capture():
    os.makedirs("out", exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)
        click_wind(page)
        page.screenshot(path="out/chart.png", full_page=True)
        texts = ["FRAMES: " + ", ".join(f.url for f in page.frames)]
        for frame in page.frames:
            try:
                texts.append(frame.inner_text("body"))
            except Exception:
                pass
        with open("out/page.txt", "w", encoding="utf-8") as f:
            f.write("\n\n".join(texts))
        browser.close()


def count_pixels():
    arr = np.array(Image.open("out/chart.png").convert("RGB")).astype(int)
    counts = {}
    for name, rgb in BINS.items():
        close = np.abs(arr - np.array(rgb)).max(axis=2) <= TOL
        counts[name] = int(close.sum())
    return counts


def main():
    if TEST_NOTIFY:
        notify("Prueba: el aviso de viento funciona.")

    capture()
    counts = count_pixels()
    print("Pixeles por rango:", counts)

    high = any(counts[b] >= MIN_PIXELS for b in ALERT_BINS)
    previous = open("state.txt").read().strip() if os.path.exists("state.txt") else "low"
    state = "high" if high else "low"

    if state == "high" and previous != "high":
        hits = [b for b in ALERT_BINS if counts[b] >= MIN_PIXELS]
        notify(f"Viento en Pilote Norden: rango {', '.join(hits)} nudos.")
    print("Estado:", previous, "->", state)

    with open("state.txt", "w") as f:
        f.write(state)


if __name__ == "__main__":
    main()

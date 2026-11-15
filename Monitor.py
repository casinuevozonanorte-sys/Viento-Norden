"""Avisa por ntfy sobre viento (Norden y San Isidro), nivel del rio y previsión de creciente.

Norden: abre la pagina de CARP con un navegador sin pantalla y lee la rosa de
vientos (color = rango de nudos, orientacion = direccion).
San Isidro: ultima medicion del canal publico de ThingSpeak que usa la pagina
del Club Nautico San Isidro (estacion del SMN).
Rio: mareografo del club (ThingSpeak, alturas en cm): nivel, tendencia y picos.
Pronostico de 12 h: Open-Meteo (modelo abierto). La previsión de creciente es un
indicador basado en el viento previsto, no un pronostico oficial.

Avisos: los eventos van al tema compartido (con el boton "Alertas SHN") y al tema
privado (ademas con el boton "Actualizar ahora"). Las pruebas y las
actualizaciones a pedido van solo al tema privado.
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
TOPIC = os.environ.get("NTFY_TOPIC", "")                   # tema compartido
PRIVATE_TOPIC = os.environ.get("NTFY_TOPIC_PRIVATE", "")   # tema solo tuyo (con boton)
DISPATCH_TOKEN = os.environ.get("DISPATCH_TOKEN", "")
REPO = os.environ.get("REPO", "")
TEST_NOTIFY = os.environ.get("TEST_NOTIFY") == "1"
REPORT_NOW = os.environ.get("REPORT_NOW") == "1"
STATE_FILE = "state.txt"
SHN_URL = "https://x.com/SHN_ALERTAS"  # alertas oficiales del SHN
FAIL_LIMIT = 3      # lecturas fallidas seguidas antes de avisar
LOWS_TO_CLEAR = 2   # lecturas "bajas" seguidas para avisar que se normalizo
ALERT_KNOTS = 15.0
LEVEL_ALERT_CM = float(os.environ.get("LEVEL_ALERT_CM", "300"))  # 3 m
MAX_MESSAGE_BYTES = 3800  # ntfy admite 4096 bytes por mensaje
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
STATIONS = ["norden", "si"]
KEYS = ["norden", "si", "rio", "creciente"]
NAMES = {"norden": "Norden", "si": "San Isidro",
         "rio": "el nivel del río", "creciente": "el pronóstico de creciente"}
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

# --- Rio / marea (ThingSpeak, mareografo del club) ---
RIVER_URL = ("https://api.thingspeak.com/channels/2075368/fields/1.json"
             "?days=1&average=10")
TREND_CM_H = 3.0         # cm/h minimos para decir que sube o baja

# --- Pronostico (Open-Meteo): latitud, longitud (aproximadas) ---
FORECAST_POINTS = {
    "norden": (-34.64, -57.93),
    "si": (-34.46, -58.51),
}
FORECAST_HOURS = 12

# --- Indicador de creciente (sudestada) ---
SURGE_DIR = (90, 170)    # viento del E al SSE (grados)
SURGE_WIND_KN = 20.0     # nudos minimos
SURGE_HOURS = 4          # horas seguidas

UP = {
    "norden": "Norden subió a 15 nudos o más.",
    "si": "San Isidro subió a 15 nudos o más.",
    "rio": f"El río superó los {LEVEL_ALERT_CM / 100:g} m en San Isidro.",
    "creciente": f"Hay previsión de creciente (sudestada) en las próximas {FORECAST_HOURS} h.",
}
DOWN = {
    "norden": "Norden bajó de 15 nudos.",
    "si": "San Isidro bajó de 15 nudos.",
    "rio": f"El río bajó de {LEVEL_ALERT_CM / 100:g} m en San Isidro.",
    "creciente": "Ya no se prevé creciente en las próximas horas.",
}


# ---------------- Avisos por ntfy ----------------
def post(topic, msg, button=False):
    """Publica en ntfy (JSON). Siempre agrega 'Alertas SHN'; con button=True
    agrega tambien 'Actualizar ahora' (lleva la llave de GitHub: solo tema privado)."""
    if not topic:
        print("Sin tema de ntfy, no se envia:", msg)
        return
    payload = {
        "topic": topic,
        "title": "Viento y río - Norden y San Isidro",
        "message": msg,
        "priority": 4,
        "tags": ["wind_face"],
    }
    actions = []
    if button and DISPATCH_TOKEN and REPO:
        actions.append({
            "action": "http",
            "label": "Actualizar ahora",
            "url": f"https://api.github.com/repos/{REPO}/actions/workflows/viento.yml/dispatches",
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {DISPATCH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            "body": json.dumps({"ref": "main", "inputs": {"modo": "reporte"}}),
        })
    actions.append({"action": "view", "label": "Alertas SHN", "url": SHN_URL})
    payload["actions"] = actions
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as exc:
        print("Error al enviar a ntfy:", exc)


def notify_alert(msg):
    """Eventos: tema compartido y tema privado (este con 'Actualizar ahora')."""
    post(TOPIC, msg)
    if PRIVATE_TOPIC:
        post(PRIVATE_TOPIC, msg, button=True)


def notify_private(msg):
    """Pruebas y actualizaciones a pedido: solo el tema privado."""
    if PRIVATE_TOPIC:
        post(PRIVATE_TOPIC, msg, button=True)
    else:
        post(TOPIC, msg)


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


# ---------------- Rio / marea ----------------
def to_local(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def find_peaks(pts, w=6, min_move=8.0):
    """Maximos y minimos locales (ventana de +-w puntos, desnivel minimo en cm)."""
    vals = [p[1] for p in pts]
    maxima, minima = [], []
    for i in range(w, len(pts) - w):
        win = vals[i - w:i + w + 1]
        if vals[i] == max(win) and vals[i] - min(win) >= min_move:
            maxima.append(pts[i])
        if vals[i] == min(win) and max(win) - vals[i] >= min_move:
            minima.append(pts[i])
    return maxima, minima


def get_river():
    """Devuelve {'high': bool, 'desc': str} o None si no se pudo leer."""
    try:
        with urllib.request.urlopen(RIVER_URL, timeout=20) as resp:
            feeds = json.load(resp)["feeds"]
        pts = []
        for f in feeds:
            v = f.get("field1")
            if v not in (None, ""):
                pts.append((to_local(f["created_at"]), float(v)))
    except Exception as exc:
        print("Error leyendo el rio:", exc)
        return None
    if len(pts) < 4:
        print("Rio: muy pocos datos:", len(pts))
        return None
    # Si las alturas vinieran en metros (ninguna mayor a 20), se pasan a cm
    if max(abs(p[1]) for p in pts) < 20:
        pts = [(t, v * 100) for t, v in pts]

    when, level = pts[-1]
    age = (datetime.now(LOCAL_TZ) - when).total_seconds() / 60
    print("Rio:", level, "cm | edad (min):", round(age))
    if age > MAX_AGE_MIN:
        print("Dato del rio demasiado viejo, se ignora")
        return None

    # Tendencia: pendiente (cm/h) de los ultimos ~60 min
    recent = pts[-7:]
    xs = [(p[0] - recent[0][0]).total_seconds() / 3600 for p in recent]
    ys = [p[1] for p in recent]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    if slope >= TREND_CM_H:
        trend = f"subiendo {slope:.0f} cm/h"
    elif slope <= -TREND_CM_H:
        trend = f"bajando {abs(slope):.0f} cm/h"
    else:
        trend = "estable"

    maxima, minima = find_peaks(pts)
    lines = [f"Nivel actual: {level / 100:.2f} m ({level:.0f} cm), {trend}."]
    if maxima:
        t, v = maxima[-1]
        lines.append(f"Última crecida (pico): {v / 100:.2f} m a las {t:%H:%M}.")
    if minima:
        t, v = minima[-1]
        lines.append(f"Última bajante (mínimo): {v / 100:.2f} m a las {t:%H:%M}.")
    lines.append(f"Alerta del río: {LEVEL_ALERT_CM / 100:g} m.")
    return {"high": level >= LEVEL_ALERT_CM, "desc": "\n".join(lines)}


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


def forecast_data():
    data = {}
    for key in STATIONS:
        try:
            data[key] = get_forecast(*FORECAST_POINTS[key])
        except Exception as exc:
            print("Error de pronostico", key, exc)
            data[key] = None
    return data


def hour_windows(rows, test):
    """Rangos (inicio, fin) de horas consecutivas donde test(fila) es verdadero."""
    windows, start, prev = [], None, None
    for r in rows:
        if test(r):
            if start is None:
                start = r[0]
            prev = r[0]
        elif start is not None:
            windows.append((start, prev))
            start = None
    if start is not None:
        windows.append((start, prev))
    return windows


def fmt_windows(windows):
    return ", ".join(
        f"{a.hour}h" if a == b else f"{a.hour}h a {b.hour}h" for a, b in windows
    )


def angle_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def forecast_text(name, rows, detailed=True):
    if not rows:
        return f"{name.upper()}\nSin pronóstico disponible."
    speeds = [r[1] for r in rows]
    peak = max(rows, key=lambda r: r[1])
    gust_rows = [r for r in rows if r[2] is not None]
    gpeak = max(gust_rows, key=lambda r: r[2]) if gust_rows else None

    n = max(1, len(rows) // 4)
    first = sum(speeds[:n]) / n
    last = sum(speeds[-n:]) / n
    if last - first >= 3:
        trend = "va en aumento"
    elif first - last >= 3:
        trend = "va en descenso"
    else:
        trend = "se mantiene parejo"

    peak_dir = f" del {compass(peak[3])}" if peak[3] is not None else ""
    lines = [
        name.upper(),
        f"Promedio entre {min(speeds):.0f} y {max(speeds):.0f} nudos, {trend}. "
        f"Pico de {peak[1]:.0f} nudos a las {peak[0].hour}h{peak_dir}.",
    ]
    if gpeak:
        lines.append(f"Ráfagas hasta {gpeak[2]:.0f} nudos (a las {gpeak[0].hour}h).")

    over = hour_windows(rows, lambda r: r[1] >= ALERT_KNOTS)
    if over:
        lines.append(f"Promedio de 15 nudos o más: {fmt_windows(over)}.")
    else:
        lines.append("No se espera promedio de 15 nudos o más.")
    gusty = hour_windows(rows, lambda r: r[2] is not None and r[2] >= ALERT_KNOTS)
    if gusty and gusty != over:
        lines.append(f"Ráfagas de 15 nudos o más: {fmt_windows(gusty)}.")

    d0, d1 = rows[0][3], rows[-1][3]
    if d0 is not None and d1 is not None and angle_diff(d0, d1) >= 45:
        lines.append(f"El viento rota del {compass(d0)} al {compass(d1)}.")

    if detailed:
        lines.append("Hora a hora (promedio / ráfaga / dirección, ⚠ = 15 o más):")
        for when, s, g, d in rows:
            gust = f"{g:.0f}" if g is not None else "-"
            direction = compass(d) if d is not None else "-"
            mark = " ⚠" if s >= ALERT_KNOTS else ""
            lines.append(f"{when.hour:02d}h  {s:.0f} / {gust} / {direction}{mark}")
    return "\n".join(lines)


def forecast_block(data, detailed=True):
    header = (f"PRONÓSTICO PRÓXIMAS {FORECAST_HOURS} HORAS (hora de Argentina)\n"
              "Modelo abierto, no es la medición de la estación.")
    parts = [forecast_text(NAMES[k], data[k], detailed) for k in STATIONS]
    return header + "\n\n" + "\n\n".join(parts)


def get_surge(data):
    """Indicador de creciente: viento del E al SSE, fuerte y sostenido."""
    if all(data[k] is None for k in STATIONS):
        return None
    found = []
    for k in STATIONS:
        rows = data[k]
        if not rows:
            continue

        def hit(r):
            return (r[3] is not None and SURGE_DIR[0] <= r[3] <= SURGE_DIR[1]
                    and r[1] >= SURGE_WIND_KN)

        for a, b in hour_windows(rows, hit):
            hours = (b - a).total_seconds() / 3600 + 1
            if hours >= SURGE_HOURS:
                found.append(f"{NAMES[k]}: viento del E al SSE de {SURGE_WIND_KN:.0f} "
                             f"nudos o más de {a.hour}h a {b.hour}h.")
    desc = "\n".join(found) if found else "Sin señales de sudestada en las próximas 12 horas."
    return {"high": bool(found), "desc": desc}


# ---------------- Estado y mensajes ----------------
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
    lines = [
        f"{NAMES[k]}: {readings[k]['desc'] if readings[k] else 'sin lectura'}"
        for k in STATIONS
    ]
    return "LECTURA ACTUAL\n" + "\n".join(lines)


def river_block(readings):
    r = readings["rio"]
    head = "RÍO / MAREA (mareógrafo del Club Náutico San Isidro)"
    return head + "\n" + (r["desc"] if r else "Sin datos del mareógrafo.")


def surge_block(readings):
    r = readings["creciente"]
    head = "RIESGO DE CRECIENTE (sudestada)"
    body = r["desc"] if r else "No pude evaluarlo (sin pronóstico)."
    note = (f"Es un indicador basado en el viento previsto (del E al SSE, "
            f"{SURGE_WIND_KN:.0f} nudos o más, {SURGE_HOURS} h seguidas), no un "
            "pronóstico oficial. Las alertas oficiales del SHN están en @SHN_ALERTAS "
            "(botón «Alertas SHN»).")
    return f"{head}\n{body}\n{note}"


def send_full(prefix, readings, fdata, sender):
    """Arma el mensaje completo; si es muy largo, quita la tabla hora a hora."""
    for detailed in (True, False):
        parts = [prefix, status_lines(readings), river_block(readings),
                 surge_block(readings), forecast_block(fdata, detailed)]
        msg = "\n\n".join(p for p in parts if p)
        if len(msg.encode("utf-8")) <= MAX_MESSAGE_BYTES:
            break
    raw = msg.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        msg = raw[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
    sender(msg)


def main():
    if TEST_NOTIFY:
        notify_private("Prueba: el aviso de viento funciona.")

    state = load_state()
    fdata = forecast_data()
    readings = {
        "norden": get_norden(),
        "si": get_san_isidro(),
        "rio": get_river(),
        "creciente": get_surge(fdata),
    }

    if TEST_NOTIFY:
        send_full("Prueba - mensaje completo.", readings, fdata, notify_private)
    if REPORT_NOW:
        send_full("Actualización a pedido.", readings, fdata, notify_private)

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
                events.append(UP[key])
        elif st["state"] == "high":
            st["lows"] += 1
            if st["lows"] >= LOWS_TO_CLEAR:
                st["state"], st["lows"] = "low", 0
                events.append(DOWN[key])

    if events:
        send_full("\n".join(events), readings, fdata, notify_alert)

    print("Estado:", state)
    save_state(state)


if __name__ == "__main__":
    main()

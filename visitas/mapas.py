"""Georeferencia (Geocoding API) y tiempos de viaje (Routes API) de Google; o línea recta offline para pruebas."""
import json
import math
import urllib.error
import urllib.parse
import urllib.request

URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
BLOQUE = 25  # 25x25 = 625 elementos (máximo por petición) y 50 direcciones (máximo con direcciones de texto)
CAMPOS = "originIndex,destinationIndex,duration,condition,status"
URL_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"


def _post(url, cuerpo, api_key):
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": CAMPOS})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e)["error"]["message"]
        except Exception:
            msg = e.reason
        raise ValueError(f"Google respondió {e.code}: {msg}") from None


def matriz_google(direcciones, api_key, post=_post):
    """Minutos en coche entre todas las direcciones (m[i][j] = de i a j). `post` se inyecta en las pruebas."""
    n = len(direcciones)
    m = [[0.0] * n for _ in range(n)]
    for o in range(0, n, BLOQUE):
        for d in range(0, n, BLOQUE):
            orig, dest = direcciones[o:o + BLOQUE], direcciones[d:d + BLOQUE]
            r = post(URL, {
                "origins": [{"waypoint": {"address": a}} for a in orig],
                "destinations": [{"waypoint": {"address": a}} for a in dest],
                "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE",
                "regionCode": "CO", "languageCode": "es-CO",
            }, api_key)
            if isinstance(r, dict):  # un error de toda la petición llega como objeto, no como lista
                raise ValueError(f"Google respondió: {r.get('error', r)}")
            for e in r:
                i, j = e["originIndex"], e["destinationIndex"]
                if o + i == d + j:
                    continue
                if e.get("condition") != "ROUTE_EXISTS":
                    raise ValueError(f"sin ruta {orig[i]!r} -> {dest[j]!r}: {e.get('condition')} {e.get('status', '')}")
                m[o + i][d + j] = float(e.get("duration", "0s").rstrip("s")) / 60  # "160s" -> minutos
    return m


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _sector(componentes):
    """Localidad según Google: sublocalidad (localidad de Bogotá) si existe; si no, el municipio. '' si no hay ninguno."""
    for tipos in (("sublocality_level_1", "sublocality"), ("locality",), ("administrative_area_level_2",)):
        for c in componentes:
            if any(t in c.get("types", []) for t in tipos):
                return c["long_name"]
    return ""


def geocodificar(direcciones, api_key, get=_get):
    """Georeferencia: (lat, lon, dirección que entendió Google, sector) por cada dirección. `get` se inyecta en las pruebas."""
    salida = []
    for a in direcciones:
        q = urllib.parse.urlencode({"address": a, "region": "co", "language": "es", "key": api_key})
        r = get(f"{URL_GEOCODE}?{q}")
        if r.get("status") != "OK":
            raise ValueError(f"no pude ubicar {a!r}: {r.get('status')} {r.get('error_message', '')}".strip())
        g = r["results"][0]
        salida.append((g["geometry"]["location"]["lat"], g["geometry"]["location"]["lng"], g["formatted_address"], _sector(g.get("address_components", []))))
    return salida


def metros(a, b):
    """Distancia en línea recta, en metros, entre dos puntos (lat, lon, ...)."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def minutos_entre(a, b, kmh=22, rodeo=1.35):
    """Estimación sin API: línea recta x factor de rodeo / velocidad media urbana."""
    return metros(a, b) / 1000 * rodeo / kmh * 60


def matriz_coordenadas(coords):
    return [[minutos_entre(a, b) for b in coords] for a in coords]

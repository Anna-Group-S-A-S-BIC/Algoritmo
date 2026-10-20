"""Programación de visitas: la prioridad (personas, renovación, criticidad) manda; la distancia ordena el resto.

No hay punto de salida externo: el cronograma de un coordinador arranca en su cliente de mayor prioridad.
"""
import unicodedata
from collections import namedtuple
from dataclasses import dataclass

from .mapas import metros

W_PERS = 0.099      # por cada persona después de la 3.ª
F_REN = 1.5         # multiplicador si NO hay renovación automática y el contrato vence pronto
DIAS_RENOVACION = 60  # "pronto" = vence en 60 días o menos (2 meses)
DIAS_GRACIA = 5       # un contrato vencido hace más de 5 días ya no cuenta
AJUSTE_CRIT = {"critico": 0.50, "estable": 0.25, "excelente": -0.25}  # se suma al peso de visitas
UMBRAL = 1          # urgente si puntaje > UMBRAL (puntaje 1 = sin ningún factor activo)
VISITAS_LLAMADA = 1  # visitas al mes (0.25 semanales en el Operation Planner): se cumplen por llamada, no entran a la ruta
MIN_VISITAS = 3
GEOCERCA_MIN = 10     # m: a esta distancia o menos dos clientes están en el mismo punto
UBICACION_MAX = 50    # m: dos clientes a esta distancia o menos son del mismo grupo de ubicación (edificio, conjunto, sede)
GEOCERCA_MAX = 500    # m: radio máximo de una jornada alrededor de su primera visita
MAX_EXACTO = 12       # hasta estas paradas el orden de una jornada es el óptimo exacto; con más, mejora por 2-opt
DIAS_POR_SEMANA = 5   # días hábiles de una semana: solo para avisar cuando una semana no cabe


@dataclass(frozen=True)
class Cliente:
    nombre: str
    direccion: str
    criticidad: str             # critico | estable | excelente
    renovacion_automatica: bool
    personas: float
    coordinador: str = ""
    visitas_mes: float = 0.0    # visitas que debe recibir al mes: 1, 2, 4, ... (0 = sin dato)
    dias_para_vencer: float = None  # días desde el 1.º del mes programado hasta el fin del contrato (negativo = vencido); None = sin dato


def norm(s):
    """Minúsculas, sin tildes ni espacios sobrantes: para comparar nombres."""
    s = unicodedata.normalize("NFD", str(s).strip().lower())
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def por_llamada(c):
    """Con 0.25 visitas semanales (1 al mes) el coordinador cumple por llamada: no entra al cronograma de ruta."""
    return c.visitas_mes == VISITAS_LLAMADA


def peso_visitas(c, n):
    """Visitas al mes del cliente / número de clientes de la ruta, más el ajuste por criticidad (nunca menor que 0)."""
    if c.criticidad not in AJUSTE_CRIT:
        raise ValueError(f"criticidad inválida en {c.nombre}: {c.criticidad!r}")
    return max(0.0, c.visitas_mes / n + AJUSTE_CRIT[c.criticidad])


def puntaje(c, n):
    """personas x renovación x peso de visitas; `n` = clientes del coordinador en la ruta."""
    f_pers = 1 + W_PERS * max(0, c.personas - 3)
    # sin fecha de fin no se puede descartar que venza pronto: se mantiene el multiplicador
    vence_pronto = c.dias_para_vencer is None or -DIAS_GRACIA <= c.dias_para_vencer <= DIAS_RENOVACION
    f_ren = F_REN if (not c.renovacion_automatica and vence_pronto) else 1
    return f_pers * f_ren * peso_visitas(c, n)


def planificar(clientes, matriz):
    """Devuelve las posiciones (0-based) de `clientes` en el orden de visita.

    matriz[i][j] = minutos de viaje de clientes[i] a clientes[j]. Arranca en el cliente de mayor
    puntaje; si ninguno es urgente, en el más central (menor suma de tiempos hacia los demás).
    """
    n = len(clientes)
    if n < MIN_VISITAS:
        raise ValueError(f"se necesitan al menos {MIN_VISITAS} clientes, hay {n}")
    if len(matriz) != n or any(len(f) != n for f in matriz):
        raise ValueError(f"la matriz debe ser {n}x{n} (un renglón por cliente)")
    pts = [puntaje(c, n) for c in clientes]
    ruta = sorted((i for i in range(n) if pts[i] > UMBRAL), key=lambda i: -pts[i])
    resto = [i for i in range(n) if pts[i] <= UMBRAL]
    if not ruta:
        centro = min(resto, key=lambda i: sum(matriz[i]))
        resto.remove(centro)
        ruta.append(centro)
    actual = ruta[-1]
    while resto:  # vecino más cercano
        i = min(resto, key=lambda i: matriz[actual][i])
        resto.remove(i)
        ruta.append(i)
        actual = i
    return ruta


# metros/geocerca: respecto a la parada anterior (None en la primera de la jornada, salvo que el coordinador salga de su casa:
# entonces son respecto a la casa). sector: localidad según Google; coordenadas: (lat, lon).
Parada = namedtuple("Parada", "cliente metros geocerca ubicacion sector coordenadas puntaje", defaults=(None, None, None))


def geocerca(m):
    return "mismo punto" if m <= GEOCERCA_MIN else ("cerca" if m <= GEOCERCA_MAX else "lejos")


def grupos_de_ubicacion(coords):
    """Grupo de ubicación de cada punto: puntos encadenados por vecino más cercano a UBICACION_MAX metros o menos.

    Devuelve una lista con el id de grupo (el menor índice del grupo) de cada punto. Un grupo es un mismo edificio,
    conjunto o sede: sus clientes se visitan el mismo día.
    """
    n = len(coords)
    raiz = list(range(n))

    def buscar(i):
        while raiz[i] != i:
            raiz[i] = raiz[raiz[i]]
            i = raiz[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if metros(coords[i], coords[j]) <= UBICACION_MAX:
                raiz[max(buscar(i), buscar(j))] = min(buscar(i), buscar(j))
    return [buscar(i) for i in range(n)]


def jornadas(clientes, matriz, coords, sectorizar=True):
    """Reparte el orden de prioridad en jornadas diarias de posiciones (0-based).

    Cada jornada se arma alrededor del cliente de mayor prioridad que falte (el ancla), suma los más cercanos hasta
    MIN_VISITAS y después solo los que caigan dentro de la geocerca (GEOCERCA_MAX metros) del ancla. Una última
    jornada bajo el mínimo se une a la anterior.
    Con `sectorizar` (algoritmo v2) además: entran juntos los clientes de un mismo grupo de ubicación (UBICACION_MAX),
    y los compañeros de la jornada salen de su mismo sector (localidad); si el sector no alcanza para el mínimo, de
    cualquier sector. Sin `sectorizar` se ignoran sectores y grupos.
    `coords[i]` = (lat, lon[, texto[, sector]]). Esto solo decide QUIÉN va en cada jornada; el orden de las paradas
    lo fija optimizar_orden.
    """
    if len(coords) != len(clientes):
        raise ValueError(f"hacen falta {len(clientes)} coordenadas, hay {len(coords)}")
    grupo = grupos_de_ubicacion(coords) if sectorizar else list(range(len(coords)))
    sector = [c[3] if len(c) > 3 and c[3] else None for c in coords] if sectorizar else [None] * len(coords)
    pendientes = planificar(clientes, matriz)

    def sumar(dia, i):
        for j in [i] + [k for k in pendientes if grupo[k] == grupo[i] and k != i]:
            pendientes.remove(j)
            dia.append(j)

    dias = []
    while pendientes:
        dia = []
        sumar(dia, pendientes[0])
        ancla = dia[0]
        while pendientes:
            mismo = [i for i in pendientes if sector[i] == sector[ancla]]
            if len(dia) < MIN_VISITAS:
                cand = mismo or pendientes
            else:
                cand = [i for i in mismo if metros(coords[ancla], coords[i]) <= GEOCERCA_MAX]
            if not cand:
                break
            sumar(dia, min(cand, key=lambda i: matriz[dia[-1]][i]))
        dias.append(dia)
    if len(dias) > 1 and len(dias[-1]) < MIN_VISITAS:
        ultima = dias.pop()
        dias[-1] += ultima
    return dias


def visitas_por_semana(n, desfase=0):
    """Reparte n visitas al mes en 4 semanas lo más parejo posible (1 -> sem 1; 2 -> sem 1 y 3; 4 -> una por semana).

    `desfase` rota el patrón (1 = arranca en la semana 2) para no cargar todas las visitas en la misma semana.
    """
    n = round(n)
    base = [(n * s + 3) // 4 - (n * (s - 1) + 3) // 4 for s in range(1, 5)]
    return [base[(s - desfase) % 4] for s in range(4)]


def optimizar_orden(dia, matriz, salida=None):
    """Reordena una jornada (posiciones) para minimizar los minutos de viaje.

    Sin `salida` el recorrido es abierto y sin punto de partida. Con `salida` (minutos desde la casa del coordinador
    hasta cada cliente, indexada igual que la matriz) la jornada parte de la casa: se cuenta el viaje a la 1.ª parada
    y no se vuelve a casa. Hasta MAX_EXACTO paradas es el óptimo exacto (Held-Karp); con más, mejora el orden dado
    por 2-opt. Nunca devuelve un orden peor que el recibido: si no mejora, deja el mismo.
    """
    n = len(dia)
    if n < 2 or (n == 2 and salida is None):
        return list(dia)
    inicio = (lambda i: salida[i]) if salida is not None else (lambda i: 0.0)
    costo = lambda r: inicio(r[0]) + sum(matriz[a][b] for a, b in zip(r, r[1:]))
    mejor = list(dia)
    if n <= MAX_EXACTO:
        INF = float("inf")
        dp = {(1 << j, j): (inicio(dia[j]), None) for j in range(n)}  # (máscara, última) -> (costo, anterior)
        for mask in range(1, 1 << n):
            for j in range(n):
                if (mask, j) not in dp:
                    continue
                c = dp[(mask, j)][0]
                for k in range(n):
                    if not mask >> k & 1:
                        nuevo = (mask | 1 << k, k)
                        v = c + matriz[dia[j]][dia[k]]
                        if v < dp.get(nuevo, (INF,))[0]:
                            dp[nuevo] = (v, j)
        full = (1 << n) - 1
        j = min(range(n), key=lambda j: dp[(full, j)][0])
        orden, mask = [], full
        while j is not None:
            orden.append(dia[j])
            mask, j = mask ^ (1 << j), dp[(mask, j)][1]
        cand = orden[::-1]
    else:
        cand, mejora = list(dia), True
        while mejora:
            mejora = False
            for i in range(n - 1):
                for k in range(i + 1, n):
                    prueba = cand[:i] + cand[i:k + 1][::-1] + cand[k + 1:]
                    if costo(prueba) < costo(cand) - 1e-9:
                        cand, mejora = prueba, True
    return cand if costo(cand) < costo(mejor) - 1e-9 else mejor


def jornadas_de_semana(clientes, matriz, coords, veces, salida=None, sectorizar=True):
    """Jornadas de una semana; veces[i] = cuántas veces se visita al cliente i esa semana.

    Cada vuelta (1.ª visita de todos, 2.ª visita de los que tienen dos...) se arma por separado, así
    un cliente nunca repite en la misma jornada. Una vuelta con menos de 3 clientes se suma a una
    jornada de la semana donde no esté ninguno; si no hay, queda como jornada corta.
    """
    dias = []
    for vuelta in range(1, max(veces, default=0) + 1):
        idx = [i for i, v in enumerate(veces) if v >= vuelta]
        if len(idx) >= MIN_VISITAS:
            sub = lambda m: [[m[i][j] for j in idx] for i in idx]
            for dia in jornadas([clientes[i] for i in idx], sub(matriz), [coords[i] for i in idx], sectorizar):
                dias.append([idx[k] for k in dia])
            continue
        destino = next((d for d in reversed(dias) if not set(d) & set(idx)), None)
        if destino is not None:
            destino += idx
        else:
            dias.append(idx)
    return [optimizar_orden(d, matriz, salida) for d in dias]


def calendario(clientes, matriz, coords, salida=None, sectorizar=True):
    """Cronograma del mes: 4 semanas, cada una una lista de jornadas (posiciones). Cada cliente suma sus visitas_mes.

    Dentro de cada grupo de clientes con las mismas visitas, los de mayor puntaje arrancan en las primeras semanas.
    """
    n = len(clientes)
    veces = [[0] * n for _ in range(4)]  # veces[semana][cliente]
    grupos = {}
    for i in sorted(range(n), key=lambda i: -puntaje(clientes[i], n)):
        grupos.setdefault(round(clientes[i].visitas_mes), []).append(i)
    for visitas, ids in grupos.items():
        for k, i in enumerate(ids):
            for s, v in enumerate(visitas_por_semana(visitas, k * 4 // len(ids))):
                veces[s][i] = v
    return [jornadas_de_semana(clientes, matriz, coords, veces[s], salida, sectorizar) for s in range(4)]


ALGORITMOS = ("v2", "casa")


def cronograma(clientes, coordinador, matriz_de, coordenadas_de, casa=None, algoritmo="v2"):
    """Cronograma mensual de un coordinador: filtra sus clientes, los georeferencia y arma semanas y jornadas.

    `matriz_de(direcciones)` -> minutos entre ellas; `coordenadas_de(direcciones)` -> (lat, lon, texto, sector) de cada una.
    Dos algoritmos (`algoritmo`):
      "v2"   (por defecto): prioridad + jornadas con geocerca + recorrido óptimo abierto, con sectorización por
             localidad y grupos de ubicación. No usa la casa.
      "casa": igual que v2 pero sin sectorización, y cada jornada parte de `casa` (dirección de la casa del
             coordinador, obligatoria): el recorrido óptimo cuenta el viaje casa -> 1.ª parada y no vuelve a casa.
    Devuelve 4 semanas; cada semana, una lista de jornadas; cada jornada, una lista de Parada.
    """
    if algoritmo not in ALGORITMOS:
        raise ValueError(f"algoritmo {algoritmo!r} desconocido: usa {' o '.join(ALGORITMOS)}")
    if algoritmo == "casa" and not casa:
        raise ValueError("el algoritmo 'casa' necesita la dirección de la casa del coordinador")
    casa = casa if algoritmo == "casa" else None
    sectorizar = algoritmo == "v2"
    propios = [c for c in clientes if norm(c.coordinador) == norm(coordinador) and not por_llamada(c)]
    if not propios:
        raise ValueError(f"no hay clientes activos para el coordinador {coordinador!r}")
    if len(propios) < MIN_VISITAS:
        raise ValueError(f"se necesitan al menos {MIN_VISITAS} clientes, hay {len(propios)}")
    dirs = [c.direccion for c in propios]
    n = len(dirs)
    matriz, coords = matriz_de(dirs + ([casa] if casa else [])), coordenadas_de(dirs + ([casa] if casa else []))
    salida, casa_xy = None, None
    if casa:  # la casa va como un punto más de la consulta; se separa para no tratarla como cliente
        salida, casa_xy = list(matriz[n][:n]), coords[n]
        matriz, coords = [f[:n] for f in matriz[:n]], coords[:n]
    semanas = []
    for semana in calendario(propios, matriz, coords, salida, sectorizar):
        dias = []
        for dia in semana:
            paradas = []
            for k, i in enumerate(dia):
                previo = casa_xy if k == 0 else coords[dia[k - 1]]
                m = None if previo is None else metros(previo, coords[i])
                texto = coords[i][2] if len(coords[i]) > 2 else ""
                sector = coords[i][3] if sectorizar and len(coords[i]) > 3 and coords[i][3] else None
                paradas.append(Parada(propios[i], m, None if m is None else geocerca(m), texto, sector, (coords[i][0], coords[i][1]), puntaje(propios[i], n)))
            dias.append(paradas)
        semanas.append(dias)
    return semanas


def minutos(ruta, matriz, desde=None):
    """Minutos de viaje de recorrer `ruta` (posiciones 0-based). Sin `desde`, el primer cliente es la partida."""
    total = 0
    for i in ruta:
        if desde is not None:
            total += matriz[desde][i]
        desde = i
    return total

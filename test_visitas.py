"""Todas las pruebas del algoritmo. Ejecutar:  python test_visitas.py   (o  python -m unittest -v test_visitas)

Si el Operation Planner (.xlsx) está en datos_prueba/, también se prueba con el Excel real.
Ninguna prueba llama a Google: los tiempos son una estimación en línea recta.
"""
import contextlib
import csv
from dataclasses import replace
import io
import json
import os
import random
import re
import statistics
import tempfile
import unittest
import urllib.error
import urllib.parse
import warnings
from collections import Counter
from pathlib import Path
from unittest import mock

import pandas as pd

import ejecutar
from visitas.reagenda import PQRSEnMemoria, reagendar, requiere_aprobacion, resolver_solicitud, solicitar_reagenda
from visitas.algoritmo import (GEOCERCA_MAX, MIN_VISITAS, UMBRAL, Cliente, calendario, cronograma, geocerca, Parada, grupos_de_ubicacion, jornadas, minutos, optimizar_orden,
                               planificar, puntaje, por_llamada, visitas_por_semana)
from visitas.datos import HOJA, HOJA_CRITICOS, cargar_clientes, por_coordinador, primer_dia_a_programar
from visitas.datos import cargar_casas
from visitas.mapas import geocodificar, matriz_coordenadas, matriz_google, metros

DATOS = Path(__file__).parent / "datos_prueba"
FIXTURE = DATOS / "clientes_activos.csv"
EXCEL_REAL = next(iter(sorted(DATOS.glob("Operation Planner*.xlsx"))), None)

# ---------------------------------------------------------------- generador de clientes aleatorios
ZONAS = {  # (lat, lon) aproximados; "zona" es la columna UBICACIÓN del Excel
    "norte": (4.73, -74.04), "occidente": (4.68, -74.12), "sur": (4.58, -74.13), "centro": (4.62, -74.07),
    "noroccidente": (4.72, -74.10), "centro - occidente": (4.63, -74.10), "centro - oriente": (4.62, -74.06),
    "chia": (4.86, -74.05), "cajica": (4.92, -74.03), "sabana norte": (4.90, -74.03), "cota": (4.81, -74.10),
    "soacha": (4.58, -74.22), "mosquera": (4.71, -74.23), "zipaquira": (5.02, -74.00), "tocancipa": (4.97, -73.91),
    "ubate": (5.31, -73.82), "fusagasuga": (4.34, -74.36), "girardot": (4.30, -74.80), "ricaurte": (4.28, -74.77),
    "tunja": (5.54, -73.36), "medellin": (6.25, -75.57), "envigado": (6.17, -75.59), "barranquilla": (10.96, -74.80),
}
URBANAS = {"norte": 35, "occidente": 15, "sur": 12, "centro": 12, "chia": 8, "cajica": 4, "soacha": 5, "mosquera": 4, "cota": 5}
PERSONAS = [0.5, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 21, 28]
PESOS_PERSONAS = [2, 12, 16, 14, 11, 9, 8, 7, 6, 5, 4, 3, 2]


def posicion(zona, rnd, sigma=0.012):
    lat, lon = ZONAS[zona]
    return lat + rnd.gauss(0, sigma), lon + rnd.gauss(0, sigma)


def generar(n, seed):
    """(clientes, matriz n x n de minutos entre ellos, coordenadas). Reproducible por seed."""
    rnd = random.Random(seed)
    clientes, coords = [], []
    for i in range(n):
        zona = rnd.choices(list(URBANAS), list(URBANAS.values()))[0]
        coords.append(posicion(zona, rnd))
        clientes.append(Cliente(
            nombre=f"S{seed}-{i:03d}",
            direccion=f"Calle {rnd.randint(1, 170)} # {rnd.randint(1, 90)}-{rnd.randint(1, 99)}, {zona}",
            criticidad=rnd.choices(["critico", "estable", "excelente"], [20, 50, 30])[0],
            renovacion_automatica=rnd.random() < 0.55,
            personas=rnd.choices(PERSONAS, PESOS_PERSONAS)[0],
            visitas_mes=rnd.choices([0, 1, 2, 3, 4, 8, 12], [2, 37, 27, 1, 25, 8, 1])[0],
        ))
    return clientes, matriz_coordenadas(coords), coords


# ---------------------------------------------------------------- ayudas
def cl(nombre, crit="estable", auto=True, personas=1, coordinador="", visitas_mes=0):
    return Cliente(nombre, f"dir {nombre}", crit, auto, personas, coordinador, visitas_mes)


def linea(posiciones):
    """Matriz de una calle recta: el cliente i está en posiciones[i]."""
    return [[abs(a - b) for b in posiciones] for a in posiciones]


def revisar_reglas(test, clientes, matriz, ruta):
    """Las reglas del algoritmo, comprobadas sobre una ruta ya calculada."""
    n = len(clientes)
    test.assertEqual(sorted(ruta), list(range(n)))                  # nadie se pierde ni se repite
    pts = [puntaje(c, len(clientes)) for c in clientes]
    k = sum(p > UMBRAL for p in pts)
    test.assertTrue(all(pts[i] > UMBRAL for i in ruta[:k]))         # la prioridad manda sobre la distancia
    test.assertTrue(all(pts[i] <= UMBRAL for i in ruta[k:]))
    test.assertEqual([pts[i] for i in ruta[:k]], sorted((pts[i] for i in ruta[:k]), reverse=True))
    if k:                                                           # parte del cliente de mayor puntaje
        test.assertEqual(pts[ruta[0]], max(pts))
        actual, resto = ruta[k - 1], ruta[k:]
    else:                                                           # sin urgentes: parte del más central
        test.assertEqual(sum(matriz[ruta[0]]), min(sum(f) for f in matriz))
        actual, resto = ruta[0], ruta[1:]
    pendientes = set(resto)
    for i in resto:                                                 # el resto: siempre el más cercano
        test.assertEqual(matriz[actual][i], min(matriz[actual][j] for j in pendientes))
        pendientes.remove(i)
        actual = i


def revisar_jornadas(test, clientes, matriz, coords, dias):
    """Reglas de las jornadas, comprobadas sobre jornadas ya armadas."""
    test.assertEqual(sorted(i for d in dias for i in d), list(range(len(clientes))))  # cada cliente en una sola jornada
    test.assertTrue(all(len(d) >= MIN_VISITAS for d in dias))
    orden, vistos = planificar(clientes, matriz), set()
    for d in dias:                                                  # arrancan siguiendo el orden de prioridad
        test.assertEqual(d[0], next(i for i in orden if i not in vistos))
        vistos.update(d)
    for d in dias[:-2]:                                             # tras el mínimo, solo dentro de la geocerca
        for i in d[MIN_VISITAS:]:
            test.assertLessEqual(metros(coords[d[0]], coords[i]), GEOCERCA_MAX)


def revisar_calendario(test, clientes, semanas):
    """El mes: 4 semanas, y cada cliente recibe exactamente las visitas que tiene definidas."""
    test.assertEqual(len(semanas), 4)
    veces = Counter(i for s in semanas for d in s for i in d)
    for i, c in enumerate(clientes):
        test.assertEqual(veces[i], round(c.visitas_mes), c.nombre)
    for s in semanas:
        for d in s:
            test.assertEqual(len(d), len(set(d)))                   # nadie dos veces el mismo día


def xlsx_de_prueba(ruta, filas):
    cols = ["CENTRO DE COSTO", "STATUS", "COORDINADOR", "CANT. PERSONAS", "RENOVACIÓN AUTOMATICA", "DIRECCIÓN", "CANT. VISITAS SEMANALES",
            "TIEMPO DE VENCIMIENTO", "FECHA AL DIA"]
    with pd.ExcelWriter(ruta) as w:
        pd.DataFrame([f + [None] * (len(cols) - len(f)) for f in filas], columns=cols).to_excel(w, sheet_name=HOJA, index=False)
        pd.DataFrame([["  NEXUS ", "CRITICO"]], columns=["CENTRO DE COSTO", "ESTADO"]).to_excel(w, sheet_name=HOJA_CRITICOS, index=False)


def google_falso(url, cuerpo, api_key):
    """Responde como la Routes API: lista de elementos, 1 minuto por cada salto de índice entre 'dN' y 'dM'."""
    orig = [o["waypoint"]["address"] for o in cuerpo["origins"]]
    dest = [d["waypoint"]["address"] for d in cuerpo["destinations"]]
    assert len(orig) * len(dest) <= 625 and len(orig) + len(dest) <= 50  # límites reales de la API
    google_falso.llamadas += 1
    return [{"originIndex": i, "destinationIndex": j, "condition": "ROUTE_EXISTS",
             "duration": f"{abs(int(o[1:]) - int(d[1:])) * 60}s"}
            for i, o in enumerate(orig) for j, d in enumerate(dest)]


# ---------------------------------------------------------------- puntaje y reglas
class TestPuntaje(unittest.TestCase):
    def test_peso_es_visitas_sobre_clientes_mas_ajuste_de_criticidad(self):
        # 10 clientes en la ruta, 4 visitas al mes: 4/10 = 0.4; critico +0.50, estable +0.25, excelente -0.25
        for crit, esperado in [("critico", 0.9), ("estable", 0.65), ("excelente", 0.15)]:
            with self.subTest(crit=crit):
                self.assertAlmostEqual(puntaje(cl("x", crit, True, 3, visitas_mes=4), 10), esperado)

    def test_el_peso_nunca_es_negativo(self):
        self.assertEqual(puntaje(cl("x", "excelente", True, 3, visitas_mes=1), 10), 0)

    def test_personas_y_renovacion_multiplican(self):
        base = puntaje(cl("x", "estable", True, 3, visitas_mes=4), 4)                    # 4/4 + 0.25 = 1.25
        self.assertAlmostEqual(base, 1.25)
        self.assertAlmostEqual(puntaje(cl("x", "estable", True, 10, visitas_mes=4), 4), base * (1 + 0.099 * 7))
        self.assertAlmostEqual(puntaje(cl("x", "estable", False, 3, visitas_mes=4), 4), base * 1.5)

    def test_mas_visitas_pesa_mas(self):
        self.assertGreater(puntaje(cl("x", visitas_mes=8), 10), puntaje(cl("y", visitas_mes=4), 10))

    def test_con_0_25_semanales_es_por_llamada(self):
        self.assertTrue(por_llamada(cl("x", visitas_mes=1)))
        self.assertFalse(por_llamada(cl("x", visitas_mes=2)))
        self.assertFalse(por_llamada(cl("x", visitas_mes=0)))

    def test_criticidad_invalida(self):
        with self.assertRaises(ValueError):
            puntaje(cl("x", "grave"), 3)


class TestRenovacion(unittest.TestCase):
    def test_multiplicador_solo_si_vence_pronto(self):
        base = puntaje(cl("x", "estable", True, visitas_mes=4), 4)
        for dias, esperado in [(None, 1.5), (-100, 1), (-6, 1), (-5, 1.5), (60, 1.5), (61, 1), (500, 1)]:
            c = cl("x", "estable", False, visitas_mes=4)
            self.assertAlmostEqual(puntaje(replace(c, dias_para_vencer=dias), 4) / base, esperado, msg=dias)

    def test_con_renovacion_automatica_nunca_multiplica(self):
        c = cl("x", "estable", True, visitas_mes=4)
        self.assertEqual(puntaje(replace(c, dias_para_vencer=10), 4), puntaje(c, 4))


class TestOptimizarOrden(unittest.TestCase):
    def test_exacto_encuentra_el_mejor_recorrido(self):
        m = linea([0, 10, 3, 7, 1])  # puntos en una recta: el mejor recorrido abierto es de punta a punta, 10 min
        mejor = optimizar_orden([0, 1, 2, 3, 4], m)
        self.assertEqual(minutos(mejor, m), 10)
        self.assertEqual(sorted(mejor), [0, 1, 2, 3, 4])

    def test_nunca_empeora_y_conserva_las_paradas(self):
        rnd = random.Random(7)
        for n in (3, 5, 8, 12, 15, 25):  # hasta 12 exacto; después 2-opt
            pts = [(rnd.random() * 10, rnd.random() * 10) for _ in range(n)]
            m = [[((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for b in pts] for a in pts]
            dia = list(range(n))
            nuevo = optimizar_orden(dia, m)
            self.assertEqual(sorted(nuevo), dia)
            self.assertLessEqual(minutos(nuevo, m), minutos(dia, m) + 1e-9)

    def test_exacto_supera_o_iguala_al_vecino_mas_cercano(self):
        rnd = random.Random(11)
        for _ in range(30):
            n = 8
            pts = [(rnd.random(), rnd.random()) for _ in range(n)]
            m = [[((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for b in pts] for a in pts]
            vecino, resto = [0], list(range(1, n))
            while resto:
                sig = min(resto, key=lambda i: m[vecino[-1]][i])
                resto.remove(sig)
                vecino.append(sig)
            self.assertLessEqual(minutos(optimizar_orden(vecino, m), m), minutos(vecino, m) + 1e-9)

    def test_jornada_corta_queda_igual(self):
        self.assertEqual(optimizar_orden([4, 2], [[0] * 5] * 5), [4, 2])


class TestPlanificar(unittest.TestCase):
    def test_parte_del_cliente_de_mayor_puntaje(self):
        cs = [cl("a", visitas_mes=4), cl("b", "estable", False, visitas_mes=4), cl("c", "critico", True, 10, visitas_mes=4)]
        self.assertEqual(planificar(cs, linea([0, 1, 2]))[0], 2)

    def test_mas_visitas_pasa_primero_aunque_este_lejos(self):
        cs = [cl("cerca"), cl("lejos", visitas_mes=4), cl("otro")]
        self.assertEqual(planificar(cs, linea([1, 50, 2]))[0], 1)

    def test_urgentes_primero_aunque_esten_lejos(self):
        cs = [cl("cerca"), cl("lejos_A", "critico", visitas_mes=4), cl("lejos_B", "estable", False, visitas_mes=4)]
        orden = [cs[i].nombre for i in planificar(cs, linea([1, 50, 60]))]
        self.assertEqual(orden, ["lejos_B", "lejos_A", "cerca"])  # 2.375, 1.83, y el cercano (0.25) al final

    def test_urgentes_por_puntaje_descendente(self):
        cs = [cl("a", "estable", False, visitas_mes=4), cl("b", "critico", False, visitas_mes=4), cl("c", "critico", visitas_mes=4)]
        orden = [cs[i].nombre for i in planificar(cs, linea([1, 2, 3]))]
        self.assertEqual(orden, ["b", "a", "c"])  # 2.75, 2.375, 1.83

    def test_sin_urgentes_parte_del_mas_central(self):
        # posiciones 35, 10, 20: el del medio (20) es el más central; de ahí, 10 (a 10 min) y luego 35
        self.assertEqual(planificar([cl("x"), cl("y"), cl("z")], linea([35, 10, 20])), [2, 1, 0])

    def test_resto_continua_desde_el_ultimo_urgente(self):
        cs = [cl("u", "critico", visitas_mes=4), cl("p"), cl("q")]
        self.assertEqual(planificar(cs, linea([100, 5, 99])), [0, 2, 1])  # q está pegado a u

    def test_es_permutacion(self):
        cs = [cl(str(i), "critico" if i % 2 else "estable", i % 3 == 0, i) for i in range(12)]
        self.assertEqual(sorted(planificar(cs, linea(list(range(12))))), list(range(12)))

    def test_minutos(self):
        m = linea([0, 10, 25, 40])
        self.assertEqual(minutos([0, 1, 2, 3], m), 40)       # el primero es la partida: no cuesta llegar
        self.assertEqual(minutos([3], m, desde=1), 30)

    def test_errores_de_entrada(self):
        with self.assertRaises(ValueError):
            planificar([cl("a"), cl("b")], linea([0, 1]))              # menos de 3 visitas
        with self.assertRaises(ValueError):
            planificar([cl("a"), cl("b"), cl("c")], linea([0, 1]))     # matriz de otro tamaño
        with self.assertRaises(ValueError):
            planificar([cl("a"), cl("b"), cl("c")], linea([0, 1, 2, 3]))  # trae un nodo de más (¿punto de salida?)


def norte(metros_al_norte):
    """Punto (lat, lon) a tantos metros al norte de un origen en Bogotá."""
    return (4.65 + metros_al_norte / 111195, -74.08)


def jornadas_de(*metros_al_norte, criticos=()):
    """Jornadas para clientes puestos en una línea norte-sur; los índices en `criticos` son críticos (van primero)."""
    n = len(metros_al_norte)
    # visitas al mes que dejan a los críticos sobre el umbral (v/n + 0.5 > 1) y a los estables bajo él (v/n + 0.25 <= 1)
    cs = [cl(str(i), "critico" if i in criticos else "estable", visitas_mes=0.6 * n) for i in range(n)]
    coords = [norte(m) for m in metros_al_norte]
    return jornadas(cs, matriz_coordenadas(coords), coords)


class TestJornadas(unittest.TestCase):
    def test_geocerca_etiquetas(self):
        self.assertEqual([geocerca(m) for m in (0, 10, 11, 500, 501)], ["mismo punto", "mismo punto", "cerca", "cerca", "lejos"])

    def test_puntos_lejanos_jornadas_del_minimo(self):
        self.assertEqual([len(d) for d in jornadas_de(*range(0, 30000, 5000))], [3, 3])  # 6 clientes a 5 km

    def test_puntos_cercanos_una_sola_jornada(self):
        self.assertEqual([len(d) for d in jornadas_de(*range(0, 500, 70))], [8])  # 8 clientes en 490 m

    def test_la_geocerca_es_alrededor_de_la_primera_visita(self):
        # cliente 0 crítico en el origen; 150, 300 y 450 m entran; 600 m ya sale de la geocerca de 500 m.
        # Jornada 2: 600, 750 y 5000 m (el más cercano, aunque lejos); 5300 m queda solo y se une a ella.
        dias = jornadas_de(0, 150, 300, 450, 600, 750, 5000, 5300, criticos=(0,))
        self.assertEqual(dias, [[0, 1, 2, 3], [4, 5, 6, 7]])

    def test_ultima_jornada_bajo_el_minimo_se_une_a_la_anterior(self):
        self.assertEqual([len(d) for d in jornadas_de(0, 5000, 10000, 15000)], [4])

    def test_cada_jornada_arranca_en_el_de_mayor_prioridad_pendiente(self):
        dias = jornadas_de(0, 5000, 10000, 15000, 20000, 25000, criticos=(5,))
        self.assertEqual(dias[0][0], 5)  # el crítico, aunque esté en el extremo

    def test_coordenadas_de_menos(self):
        with self.assertRaises(ValueError):
            jornadas([cl("a"), cl("b"), cl("c")], linea([0, 1, 2]), [norte(0)])


class TestCalendario(unittest.TestCase):
    def test_visitas_por_semana(self):
        for n, esperado in [(0, [0, 0, 0, 0]), (1, [1, 0, 0, 0]), (2, [1, 0, 1, 0]), (3, [1, 1, 1, 0]),
                            (4, [1, 1, 1, 1]), (8, [2, 2, 2, 2]), (12, [3, 3, 3, 3])]:
            with self.subTest(visitas=n):
                self.assertEqual(visitas_por_semana(n), esperado)
                self.assertEqual(sum(visitas_por_semana(n, 2)), n)
        self.assertEqual(visitas_por_semana(2, 1), [0, 1, 0, 1])  # el desfase rota las semanas

    def test_cada_cliente_recibe_sus_visitas_del_mes(self):
        cs = [cl(str(i), visitas_mes=v) for i, v in enumerate([0, 1, 2, 3, 4, 8, 12, 4, 1, 2])]
        coords = [norte(i * 3000) for i in range(len(cs))]
        revisar_calendario(self, cs, calendario(cs, matriz_coordenadas(coords), coords))

    def test_los_clientes_de_una_visita_se_reparten_entre_las_semanas(self):
        cs = [cl(str(i), visitas_mes=1) for i in range(12)]
        coords = [norte(i * 3000) for i in range(12)]
        semanas = calendario(cs, matriz_coordenadas(coords), coords)
        self.assertEqual([sum(map(len, s)) for s in semanas], [3, 3, 3, 3])

    def test_los_de_mayor_puntaje_arrancan_en_las_primeras_semanas(self):
        cs = [cl(str(i), "critico" if i < 3 else "estable", visitas_mes=1) for i in range(12)]
        coords = [norte(i * 3000) for i in range(12)]
        semanas = calendario(cs, matriz_coordenadas(coords), coords)
        self.assertEqual({i for d in semanas[0] for i in d} & {0, 1, 2}, {0, 1, 2})  # los 3 críticos, en la semana 1

    def test_dos_visitas_del_mismo_cliente_en_una_semana_van_en_jornadas_distintas(self):
        cs = [cl("a", visitas_mes=4), cl("b", visitas_mes=4), cl("c", visitas_mes=4), cl("doble", visitas_mes=8)]
        coords = [norte(i * 3000) for i in range(4)]
        semanas = calendario(cs, matriz_coordenadas(coords), coords)
        for s in semanas:
            self.assertEqual([len(d) for d in s], [4, 1])  # la 2.ª visita de "doble" queda sola, bajo el mínimo
            self.assertEqual(s[1], [3])
        revisar_calendario(self, cs, semanas)


class TestCronograma(unittest.TestCase):
    def setUp(self):
        self.clientes = [
            cl("a", coordinador="Ana", visitas_mes=4), cl("b", coordinador="Luis", visitas_mes=4),
            cl("c", "critico", coordinador="Ana", visitas_mes=4), cl("x", coordinador="Luis", visitas_mes=4),
            cl("d", "estable", False, coordinador="Ana", visitas_mes=4), cl("y", coordinador="Luis", visitas_mes=4),
        ]
        self.pos = {"dir a": 0, "dir c": 5, "dir d": 9}
        self.pedidas = []

    def matriz_de(self, dirs):
        self.pedidas.append(dirs)
        return linea([self.pos[d] for d in dirs])  # KeyError si se pide un cliente de otro coordinador

    def coordenadas_de(self, dirs):
        return [(*norte(self.pos[d] * 20), f"ubicación de {d}") for d in dirs]  # 20 m entre unidades

    def test_filtra_por_coordinador_y_pide_solo_esos_clientes(self):
        semanas = cronograma(self.clientes, "Ana", self.matriz_de, self.coordenadas_de)
        self.assertEqual(self.pedidas, [["dir a", "dir c", "dir d"]])  # una sola consulta a Google para todo el mes
        self.assertEqual(len(semanas), 4)
        for semana in semanas:  # 4 visitas al mes = una por semana; crítico, sin renovación, luego el resto
            self.assertEqual([[p.cliente.nombre for p in d] for d in semana], [["d", "c", "a"]])  # orden óptimo de la jornada (recorrido abierto), no el de prioridad

    def test_cliente_de_0_25_semanales_va_por_llamada_y_no_entra_a_la_ruta(self):
        self.clientes[0] = replace(self.clientes[0], visitas_mes=1)  # a: 1 visita al mes
        cs = self.clientes + [cl("e", coordinador="Ana", visitas_mes=4)]
        self.pos["dir e"] = 3
        semanas = cronograma(cs, "Ana", self.matriz_de, self.coordenadas_de)
        self.assertEqual(self.pedidas, [["dir c", "dir d", "dir e"]])
        self.assertNotIn("a", {p.cliente.nombre for s in semanas for d in s for p in d})

    def test_paradas_traen_distancia_geocerca_y_ubicacion(self):
        (dia,) = cronograma(self.clientes, "Ana", self.matriz_de, self.coordenadas_de)[0]
        self.assertEqual((dia[0].metros, dia[0].geocerca), (None, None))     # la primera no tiene anterior
        self.assertAlmostEqual(dia[1].metros, 80, delta=1)                   # d (9) -> c (5): 4 x 20 m
        self.assertEqual(dia[1].geocerca, "cerca")
        self.assertAlmostEqual(dia[2].metros, 100, delta=1)                  # c (5) -> a (0): 5 x 20 m
        self.assertEqual(dia[0].ubicacion, "ubicación de dir d")

    def test_nombre_sin_tildes_ni_mayusculas(self):
        cs = [cl(n, coordinador="Andrés", visitas_mes=4) for n in "abc"]
        semanas = cronograma(cs, " ANDRES ", lambda d: linea(range(len(d))), lambda d: [norte(0)] * len(d))
        self.assertEqual(sum(len(d) for s in semanas for d in s), 12)

    def test_coordinador_inexistente_o_con_pocos_clientes(self):
        with self.assertRaises(ValueError):
            cronograma(self.clientes, "Nadie", self.matriz_de, self.coordenadas_de)
        with self.assertRaises(ValueError):
            cronograma(self.clientes[:2], "Ana", self.matriz_de, self.coordenadas_de)  # solo 1 cliente


class TestSectoresYUbicaciones(unittest.TestCase):
    def test_grupos_de_ubicacion_encadenan_a_50_m(self):
        coords = [norte(0), norte(40), norte(80), norte(500), norte(51 + 600), norte(600)]
        # 0-40-80 se encadenan (cada salto <= 50 m); 500 queda solo; 600 y 651 están a 51 m: no se unen
        self.assertEqual(grupos_de_ubicacion(coords), [0, 0, 0, 3, 4, 5])

    def test_un_grupo_de_ubicacion_va_completo_el_mismo_dia(self):
        # clientes 0 y 1 son del mismo edificio (40 m); aunque el 1 sea de los menos prioritarios entra con el 0
        cs = [cl("a", "critico"), cl("b"), cl("c"), cl("d"), cl("e"), cl("f")]
        pos = [0, 40, 3000, 3100, 6000, 6100]
        coords = [norte(m) for m in pos]
        dias = jornadas(cs, linea(pos), coords)
        self.assertTrue(any({0, 1} <= set(d) for d in dias))

    def test_la_jornada_se_arma_dentro_del_sector(self):
        pos = [0, 300, 400, 100, 150, 200]  # 0 (crítico), 1 y 2 en el sector A; 3, 4 y 5 en el B, aunque 3 y 4 están más cerca del 0
        sector = ["A", "A", "A", "B", "B", "B"]
        cs = [cl(str(i), "critico" if i == 0 else "estable") for i in range(6)]
        coords = [(*norte(m), "", sec) for m, sec in zip(pos, sector)]
        dias = jornadas(cs, linea(pos), coords)
        self.assertEqual(sorted(sorted(d) for d in dias), [[0, 1, 2], [3, 4, 5]])

    def test_si_el_sector_no_alcanza_se_completa_con_otro(self):
        pos = [0, 100, 200, 300]
        cs = [cl(str(i), "critico" if i == 0 else "estable") for i in range(4)]
        coords = [(*norte(m), "", "A" if i == 0 else "B") for i, m in enumerate(pos)]  # el sector A solo tiene 1 cliente
        self.assertEqual([len(d) for d in jornadas(cs, linea(pos), coords)], [4])


class TestCasa(unittest.TestCase):
    def test_optimizar_orden_parte_de_la_casa(self):
        m = linea([0, 10, 20])
        self.assertEqual(optimizar_orden([0, 1, 2], m, salida=[25, 15, 5]), [2, 1, 0])   # la casa queda junto al 2
        self.assertEqual(optimizar_orden([2, 1, 0], m, salida=[25, 15, 5]), [2, 1, 0])   # ya era el mejor: no cambia
        self.assertEqual(optimizar_orden([0, 1], m, salida=[30, 2]), [1, 0])             # con casa también se reordenan 2 paradas

    def test_cronograma_con_casa(self):
        c = TestCronograma()
        c.setUp()
        c.pos["casa Ana"] = 10  # al lado de d (9)
        semanas = cronograma(c.clientes, "Ana", c.matriz_de, c.coordenadas_de, casa="casa Ana", algoritmo="casa")
        self.assertEqual(c.pedidas, [["dir a", "dir c", "dir d", "casa Ana"]])  # la casa viaja en la misma consulta
        (dia,) = semanas[0]
        self.assertEqual([p.cliente.nombre for p in dia], ["d", "c", "a"])        # sale de casa y recorre hacia atrás
        self.assertAlmostEqual(dia[0].metros, 20, delta=1)                        # 1.ª parada: distancia desde la casa
        self.assertEqual(sum(len(d) for s in semanas for d in s), 12)

    def test_v2_ignora_la_casa(self):
        c = TestCronograma()
        c.setUp()
        cronograma(c.clientes, "Ana", c.matriz_de, c.coordenadas_de, casa="casa Ana")  # algoritmo v2 por defecto
        self.assertEqual(c.pedidas, [["dir a", "dir c", "dir d"]])                    # la casa ni se consulta

    def test_algoritmo_casa_exige_casa_y_algoritmo_valido(self):
        c = TestCronograma()
        c.setUp()
        with self.assertRaises(ValueError):
            cronograma(c.clientes, "Ana", c.matriz_de, c.coordenadas_de, algoritmo="casa")
        with self.assertRaises(ValueError):
            cronograma(c.clientes, "Ana", c.matriz_de, c.coordenadas_de, algoritmo="v3")

    def test_sectorizar_separa_localidades_y_sin_sectorizar_no(self):
        pos = [0, 150, 300, 75, 225, 375]  # todo dentro de 500 m y a más de 50 m entre sí; sector A: 0-2, sector B: 3-5
        cs = [cl(str(i), "critico" if i == 0 else "estable") for i in range(6)]
        coords = [(*norte(m), "", "A" if i < 3 else "B") for i, m in enumerate(pos)]
        self.assertEqual(sorted(sorted(d) for d in jornadas(cs, linea(pos), coords, sectorizar=True)), [[0, 1, 2], [3, 4, 5]])
        self.assertEqual([sorted(d) for d in jornadas(cs, linea(pos), coords, sectorizar=False)], [[0, 1, 2, 3, 4, 5]])

    def test_cargar_casas(self):
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "casas.csv")
            with open(ruta, "w", encoding="utf-8") as f:
                f.write("coordinador,direccion\nJuan Roberto,Calle 1 # 2-3\nAna,\n")
            self.assertEqual(cargar_casas(ruta), {"juan roberto": "Calle 1 # 2-3, Colombia"})  # sin dirección: se ignora


def cronograma_de_prueba():
    """4 semanas con 2 jornadas cada una; las del norte son del sector A y las otras del B."""
    def p(nombre, m, sector):
        return Parada(cl(nombre, "estable", True, 4), None, None, "", sector, norte(m)[:2])
    return [[[p(f"a{s}", 0, "A"), p(f"b{s}", 100, "A"), p(f"c{s}", 200, "A")],
             [p(f"d{s}", 9000, "B"), p(f"e{s}", 9100, "B"), p(f"f{s}", 9200, "B")]] for s in range(1, 5)]


class TestReagenda(unittest.TestCase):
    def test_quien_requiere_aprobacion(self):
        self.assertFalse(requiere_aprobacion(cl("x", "critico", True, 5)))      # crítico y más de 4 personas: inmediata
        self.assertTrue(requiere_aprobacion(cl("x", "critico", True, 4)))       # 4 no es "más de 4"
        self.assertTrue(requiere_aprobacion(cl("x", "estable", True, 20)))      # muchas personas pero no crítico
        self.assertTrue(requiere_aprobacion(cl("x", "excelente", True, 1)))

    def test_reagendar_mueve_a_una_jornada_posterior_del_mismo_sector(self):
        sem = cronograma_de_prueba()
        res = reagendar(sem, "a1", 1, 1)
        self.assertEqual((res.semana, res.jornada), (2, 1))                         # la siguiente jornada del sector A
        self.assertEqual([p.cliente.nombre for p in res.semanas[1][0]], ["a2", "b2", "c2", "a1"])  # entra al final de esa jornada
        self.assertIn("a1", [p.cliente.nombre for p in res.semanas[1][0]])
        self.assertNotIn("a1", [p.cliente.nombre for p in res.semanas[0][0]])
        self.assertTrue(res.avisos)                                                  # la jornada de origen queda con 2 visitas
        self.assertIn("a1", [p.cliente.nombre for p in sem[0][0]])                  # el original no se modifica

    def test_no_repite_cliente_en_la_misma_jornada(self):
        sem = cronograma_de_prueba()
        sem[1][0].append(Parada(cl("a1", "estable", True, 4), None, None, "", "A", norte(0)[:2]))  # a1 ya está en la semana 2
        res = reagendar(sem, "a1", 1, 1)
        self.assertNotEqual((res.semana, res.jornada), (2, 1))

    def test_errores(self):
        sem = cronograma_de_prueba()
        with self.assertRaises(ValueError):
            reagendar(sem, "zzz", 1, 1)           # no está ahí
        with self.assertRaises(ValueError):
            reagendar(sem, "d4", 4, 2)            # última jornada del mes: no hay posteriores

    def test_critico_con_mas_de_4_personas_se_reagenda_sin_pqr(self):
        sem, pqrs, correos = cronograma_de_prueba(), PQRSEnMemoria(), []
        c = cl("a1", "critico", True, 5)
        sol, res = solicitar_reagenda(sem, c, 1, 1, "vía bloqueada", "Ana", pqrs, lambda *a: correos.append(a), "directora@x.co")
        self.assertEqual(sol.estado, "aprobada_automatica")
        self.assertIsNotNone(res)
        self.assertEqual((pqrs.solicitudes, correos), ({}, []))                 # no se crea PQR ni se envía correo

    def test_otro_cliente_crea_pqr_y_avisa_a_la_directora(self):
        sem, pqrs, correos = cronograma_de_prueba(), PQRSEnMemoria(), []
        sol, res = solicitar_reagenda(sem, cl("a1", "estable", True, 20), 1, 1, "vía bloqueada", "Ana", pqrs, lambda *a: correos.append(a), "directora@x.co")
        self.assertIsNone(res)                                                  # no se mueve nada hasta que se acepte
        self.assertEqual((sol.tipo, sol.estado, sol.id), ("PQR", "pendiente", "PQR-0001"))
        self.assertEqual(len(correos), 1)
        self.assertEqual(correos[0][0], "directora@x.co")
        self.assertIn("PQR-0001", correos[0][1])
        self.assertIn("vía bloqueada", correos[0][2])
        # la directora acepta, con razón
        res = resolver_solicitud(sol, sem, True, "Se justifica por el bloqueo", pqrs)
        self.assertEqual((sol.estado, pqrs.solicitudes["PQR-0001"].estado, pqrs.solicitudes["PQR-0001"].razon_decision), ("aceptada", "aceptada", "Se justifica por el bloqueo"))
        self.assertNotIn("a1", [p.cliente.nombre for p in res.semanas[0][0]])

    def test_rechazo_no_mueve_y_exige_razon_y_estado_pendiente(self):
        sem, pqrs = cronograma_de_prueba(), PQRSEnMemoria()
        sol, _ = solicitar_reagenda(sem, cl("a1", "estable", True, 2), 1, 1, "x", "Ana", pqrs, lambda *a: None, "d@x.co")
        with self.assertRaises(ValueError):
            resolver_solicitud(sol, sem, True, "  ", pqrs)                       # sin razón no se decide
        self.assertEqual(sol.estado, "pendiente")
        self.assertIsNone(resolver_solicitud(sol, sem, False, "No procede", pqrs))
        self.assertEqual((sol.estado, sol.razon_decision), ("rechazada", "No procede"))
        with self.assertRaises(ValueError):
            resolver_solicitud(sol, sem, True, "otra vez", pqrs)                 # ya resuelta
        with self.assertRaises(ValueError):
            solicitar_reagenda(sem, cl("a1"), 1, 1, " ", "Ana", pqrs, lambda *a: None, "d@x.co")  # motivo obligatorio


# ---------------------------------------------------------------- Google Maps
class TestMapas(unittest.TestCase):
    def test_bloques_y_minutos(self):
        google_falso.llamadas = 0
        m = matriz_google([f"d{i}" for i in range(60)], "KEY", post=google_falso)
        self.assertEqual(google_falso.llamadas, 9)  # ceil(60/25)^2 peticiones
        self.assertTrue(all(m[i][j] == abs(i - j) for i in range(60) for j in range(60)))

    def test_envia_clave_y_direcciones_de_texto(self):
        vistos = []
        matriz_google(["d0", "d1"], "MI_CLAVE", post=lambda u, c, k: vistos.append((u, c, k)) or google_falso(u, c, k))
        url, cuerpo, clave = vistos[0]
        self.assertIn("routes.googleapis.com", url)
        self.assertEqual(clave, "MI_CLAVE")
        self.assertEqual(cuerpo["origins"][1], {"waypoint": {"address": "d1"}})
        self.assertEqual(cuerpo["travelMode"], "DRIVE")

    def test_ruta_inexistente(self):
        sin_ruta = lambda u, c, k: [{"originIndex": 0, "destinationIndex": 1, "condition": "ROUTE_NOT_FOUND"}]
        with self.assertRaises(ValueError):
            matriz_google(["a", "b"], "KEY", post=sin_ruta)

    def test_error_de_toda_la_peticion(self):
        with self.assertRaises(ValueError):
            matriz_google(["a", "b"], "KEY", post=lambda u, c, k: {"error": {"code": 403, "message": "API no habilitada"}})

    def test_error_http_muestra_el_mensaje_de_google(self):
        cuerpo = io.BytesIO(json.dumps({"error": {"message": "Routes API has not been used in project 1 before"}}).encode())
        http = urllib.error.HTTPError("u", 403, "Forbidden", {}, cuerpo)
        with mock.patch("visitas.mapas.urllib.request.urlopen", side_effect=http):
            with self.assertRaises(ValueError) as cm:
                matriz_google(["a", "b"], "KEY")
        self.assertIn("403", str(cm.exception))
        self.assertIn("Routes API has not been used", str(cm.exception))

    def test_geocodificar(self):
        def geocode_falso(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert q["key"] == ["MI_CLAVE"] and q["region"] == ["co"]
            n = int(q["address"][0].split()[-1])
            comp = ([{"long_name": "Bogotá", "types": ["locality"]}, {"long_name": "Usaquén", "types": ["sublocality_level_1", "sublocality"]}] if n == 1
                    else [{"long_name": "Cajicá", "types": ["locality"]}, {"long_name": "Cundinamarca", "types": ["administrative_area_level_1"]}])
            return {"status": "OK", "results": [{"formatted_address": f"Dirección {n}, Bogotá", "address_components": comp,
                                                 "geometry": {"location": {"lat": 4.0 + n, "lng": -74.0}}}]}
        self.assertEqual(geocodificar(["calle 1", "calle 2"], "MI_CLAVE", get=geocode_falso),
                         [(5.0, -74.0, "Dirección 1, Bogotá", "Usaquén"), (6.0, -74.0, "Dirección 2, Bogotá", "Cajicá")])  # localidad de Bogotá o, si no hay, el municipio

    def test_geocodificar_errores_dicen_cual_direccion(self):
        with self.assertRaises(ValueError) as cm:
            geocodificar(["Calle inventada 99"], "K", get=lambda u: {"status": "ZERO_RESULTS", "results": []})
        self.assertIn("Calle inventada 99", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            geocodificar(["x"], "K", get=lambda u: {"status": "REQUEST_DENIED", "error_message": "API no habilitada"})
        self.assertIn("API no habilitada", str(cm.exception))

    def test_metros(self):
        self.assertAlmostEqual(metros((4.0, -74.0), (5.0, -74.0)), 111195, delta=100)  # 1 grado de latitud
        self.assertEqual(metros((4.0, -74.0, "texto"), (4.0, -74.0)), 0)

    def test_matriz_offline_es_razonable(self):
        m = matriz_coordenadas([(4.65, -74.08), (4.65, -74.08), (4.73, -74.04)])
        self.assertEqual(m[0][1], 0)
        self.assertTrue(15 < m[0][2] < 40)  # ~10 km en ciudad: entre 15 y 40 min


# ---------------------------------------------------------------- Excel y ejecutor
FILAS = [
    ["Alfa", "Activo", "Ana", 6, "SI", "Calle 1 # 2-3\nBogotá", 1],
    ["Beta", "Inactivo", "Ana", 9, "SI", "Calle 4"],           # inactivo: se filtra
    ["Gamma", " activo ", "Luis", 2, None, "Carrera 5"],       # renovación vacía -> NO; STATUS con espacios
    ["Delta", "Activo", "Luis", 3, "NO", None],                # sin dirección: se excluye
    ["Nexus", "Activo", "Luis", 4, "NO", "Carrera 7"],         # figura como crítico (otro formato de nombre)
]


class TestCarga(unittest.TestCase):
    def test_filtra_activos_y_mapea_columnas(self):
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "p.xlsx")
            xlsx_de_prueba(ruta, FILAS)
            with warnings.catch_warnings(record=True) as avisos:
                warnings.simplefilter("always")
                cs = cargar_clientes(ruta)
        self.assertEqual([c.nombre for c in cs], ["Alfa", "Gamma", "Nexus"])
        excluidos = [a for a in avisos if "excluidos" in str(a.message)]  # otras librerías pueden avisar de otras cosas
        self.assertEqual(len(excluidos), 1)
        self.assertIn("Delta", str(excluidos[0].message))
        alfa, gamma, nexus = cs
        self.assertEqual(alfa.direccion, "Calle 1 # 2-3 Bogotá, Colombia")
        self.assertTrue(alfa.renovacion_automatica)
        self.assertFalse(gamma.renovacion_automatica)
        self.assertEqual((alfa.personas, alfa.criticidad), (6.0, "estable"))
        self.assertEqual((alfa.visitas_mes, gamma.visitas_mes), (4.0, 0.0))  # 1 semanal = 4 al mes; sin dato -> 0
        self.assertEqual(nexus.criticidad, "critico")
        self.assertEqual(set(por_coordinador(cs)), {"Ana", "Luis"})

    def test_dias_para_vencer(self):
        # el texto del Excel se calculó al 17-oct; el 1.º a programar es el 1-nov (15 días después)
        filas = [["A", "Activo", "Ana", 4, "NO", "Calle 1", 1, "Vence en 75 días ", "2026-10-17"],
                 ["B", "Activo", "Ana", 4, "NO", "Calle 2", 1, "Vence en -3 días", "2026-10-17"],
                 ["C", "Activo", "Ana", 4, "NO", "Calle 3", 1]]  # sin dato
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "p.xlsx")
            xlsx_de_prueba(ruta, filas)
            a, b, c = cargar_clientes(ruta, hoy="2026-11-01")
        self.assertEqual((a.dias_para_vencer, b.dias_para_vencer, c.dias_para_vencer), (60, -18, None))

    def test_primer_dia_a_programar(self):
        from datetime import date
        self.assertEqual(primer_dia_a_programar(date(2026, 10, 17)), date(2026, 11, 1))
        self.assertEqual(primer_dia_a_programar(date(2026, 12, 30)), date(2027, 1, 1))
        self.assertEqual(primer_dia_a_programar(date(2026, 11, 1)), date(2026, 11, 1))

    def test_tabla_de_criticidad_manda(self):
        with tempfile.TemporaryDirectory() as t:
            ruta, tabla = os.path.join(t, "p.xlsx"), os.path.join(t, "c.csv")
            xlsx_de_prueba(ruta, FILAS)
            with open(tabla, "w", encoding="utf-8") as f:
                f.write("cliente,coordinador,criticidad\nA.l-fa,Eva,Excelente\nNexus,Luis,\n")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                alfa, gamma, nexus = cargar_clientes(ruta, criticidad_csv=tabla)
        self.assertEqual((alfa.criticidad, alfa.coordinador), ("excelente", "Ana"))  # nombre con otra puntuación
        self.assertEqual((gamma.criticidad, gamma.coordinador), ("estable", "Luis"))  # ausente: queda lo del Excel; el coordinador nunca sale de la tabla
        self.assertEqual(nexus.criticidad, "critico")  # vacío en la tabla: queda la hoja de críticos

    @unittest.skipUnless(EXCEL_REAL, "pon el Operation Planner (.xlsx) en datos_prueba/")
    def test_excel_real(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cs = cargar_clientes(EXCEL_REAL)
        self.assertEqual(len(cs), 103)  # 105 activos - 2 sin dirección
        with open(FIXTURE, encoding="utf-8") as f:  # el CSV de zonas debe corresponder a este Excel
            self.assertEqual({c.nombre for c in cs}, {r["nombre"] for r in csv.DictReader(f)})

    def test_fixture_de_clientes_reales(self):
        with open(FIXTURE, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
        self.assertEqual(len({f["nombre"] for f in filas}), 103)
        self.assertTrue(all(f["direccion"].endswith(", Colombia") for f in filas))


def correr(*args):
    """Ejecuta ejecutar.main con Google simulado (1 min entre vecinos de la lista); devuelve (stdout, mock)."""
    falso = lambda dirs, key: linea(list(range(len(dirs))))
    puntos = lambda dirs, key: [(4.0 + i / 1000, -74.0, f"ubicación {i}", "Bogotá") for i in range(len(dirs))]  # a ~110 m entre sí
    salida = io.StringIO()
    with warnings.catch_warnings(), mock.patch("ejecutar.matriz_google", side_effect=falso) as g, \
            mock.patch("ejecutar.geocodificar", side_effect=puntos), contextlib.redirect_stdout(salida):
        warnings.simplefilter("ignore")
        ejecutar.main(list(args))
    return salida.getvalue(), g


def visitas(texto):
    return [m.group(1) for m in re.finditer(r"^\s+\d+\. (\S+)", texto, re.M)]


class TestEjecutar(unittest.TestCase):
    def setUp(self):  # sin tabla del mes ni casas por defecto: estas pruebas usan el Excel tal cual
        for nombre in ("CRITICIDAD_POR_DEFECTO", "CASAS_POR_DEFECTO"):
            p = mock.patch(f"ejecutar.{nombre}", None)
            p.start()
            self.addCleanup(p.stop)

    def test_de_punta_a_punta(self):
        filas = [[n, "Activo", "Ana", 1, a, f"Calle {n}", 1] for n, a in [("a", "SI"), ("Nexus", "SI"), ("c", "NO"), ("d", "SI")]]
        filas.append(["otro", "Activo", "Luis", 9, "NO", "Calle otro", 1])
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "p.xlsx")
            xlsx_de_prueba(ruta, filas)
            texto, g = correr(ruta, "--coordinador", "ana", "--api-key", "K")
        self.assertEqual(g.call_args.args[0], ["Calle a, Colombia", "Calle Nexus, Colombia", "Calle c, Colombia", "Calle d, Colombia"])
        nombres = visitas(texto)
        self.assertIn(nombres[:4], (["a", "Nexus", "c", "d"], ["d", "c", "Nexus", "a"]))  # misma jornada de 4 (Nexus crítico la abre), recorrida en el orden óptimo
        self.assertEqual(len(nombres), 16)                       # 4 clientes x 4 visitas al mes (1 semanal x 4)
        self.assertIn("SEMANA 4", texto)
        self.assertIn("Jornada 1 (4 visitas)", texto)
        self.assertEqual(texto.count("ubicación 0"), 1)          # la dirección de Google sale solo la primera vez

    def _planner_de_reagenda(self, t):
        """12 clientes de Ana con 2 visitas al mes (6 por semana; con 1 al mes irían por llamada); Nexus es crítico con 6 personas; hay casa para Ana."""
        filas = [["Nexus", "Activo", "Ana", 6, "SI", "Calle Nexus", 0.5]] + [[f"c{i:02d}", "Activo", "Ana", 1, "SI", f"Calle {i}", 0.5] for i in range(1, 12)]
        ruta, casas = os.path.join(t, "p.xlsx"), os.path.join(t, "casas.csv")
        xlsx_de_prueba(ruta, filas)
        with open(casas, "w", encoding="utf-8") as f:
            f.write("coordinador,direccion\nAna,Calle de la casa\n")
        return ruta, casas

    def test_reagendar_critico_es_inmediato_en_los_dos_algoritmos(self):
        for algoritmo in ("v2", "casa"):
            with self.subTest(algoritmo=algoritmo), tempfile.TemporaryDirectory() as t:
                ruta, casas = self._planner_de_reagenda(t)
                texto, _ = correr(ruta, "--coordinador", "Ana", "--api-key", "K", "--algoritmo", algoritmo, "--casas", casas,
                                  "--reagendar", "Nexus|1|1", "--motivo", "vía bloqueada")
                self.assertIn("REAGENDADA: Nexus pasa a la semana 2, jornada 1", texto)
                antes, despues = texto.split("CRONOGRAMA ACTUALIZADO")
                self.assertEqual((len(visitas(antes)), len(visitas(despues))), (24, 24))  # no se pierde ninguna visita
                self.assertNotIn("Nexus", visitas(despues.split("SEMANA 2")[0]))           # ya no está en la semana 1
                self.assertIn("Nexus", visitas(despues.split("SEMANA 2")[1].split("SEMANA 3")[0]))  # y sí en la semana 2

    def test_reagendar_otro_cliente_crea_pqr_y_no_mueve_nada(self):
        for algoritmo in ("v2", "casa"):
            with self.subTest(algoritmo=algoritmo), tempfile.TemporaryDirectory() as t:
                ruta, casas = self._planner_de_reagenda(t)
                texto, _ = correr(ruta, "--coordinador", "Ana", "--api-key", "K", "--algoritmo", algoritmo, "--casas", casas,
                                  "--reagendar", "c01|1|1", "--motivo", "cliente cerrado", "--directora-email", "directora@x.co")
                self.assertIn("Correo a directora@x.co: [PQR-0001]", texto)
                self.assertIn("pendiente de aceptación de la directora", texto)
                self.assertNotIn("CRONOGRAMA ACTUALIZADO", texto)

    def test_reagendar_errores_claros(self):
        with tempfile.TemporaryDirectory() as t:
            ruta, casas = self._planner_de_reagenda(t)
            base = [ruta, "--coordinador", "Ana", "--api-key", "K", "--casas", casas]
            for extra in (["--reagendar", "c01|1|1"],                                             # falta el motivo
                          ["--reagendar", "c01|1|1", "--motivo", "x"],                            # falta el correo de la directora
                          ["--reagendar", "zzz|1|1", "--motivo", "x", "--directora-email", "d@x.co"],  # cliente que no existe
                          ["--reagendar", "c01-1-1", "--motivo", "x", "--directora-email", "d@x.co"]): # formato mal
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    correr(*base, *extra)

    def test_sin_clave_falla(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            ejecutar.main(["x.xlsx", "--coordinador", "Ana"])

    def test_coordinador_inexistente_falla_y_lista_los_disponibles(self):
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "p.xlsx")
            xlsx_de_prueba(ruta, FILAS)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                ejecutar.main([ruta, "--coordinador", "Nadie", "--api-key", "K"])
        self.assertIn("Ana", err.getvalue())

    def test_error_de_google_se_muestra_sin_traceback_ni_usage(self):
        with tempfile.TemporaryDirectory() as t:
            ruta = os.path.join(t, "p.xlsx")
            xlsx_de_prueba(ruta, [[n, "Activo", "Ana", 1, "SI", f"Calle {n}"] for n in "abc"])
            malo = mock.patch("ejecutar.matriz_google", side_effect=ValueError("Google respondió REQUEST_DENIED: key"))
            with malo, self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                ejecutar.main([ruta, "--coordinador", "Ana", "--api-key", "K"])
        self.assertIn("REQUEST_DENIED", err.getvalue())
        self.assertNotIn("usage", err.getvalue())

    def test_excel_inexistente_da_error_claro(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            ejecutar.main(["planner.xlsx", "--coordinador", "Ana", "--api-key", "K"])
        self.assertIn("planner.xlsx", err.getvalue())
        self.assertIn("comillas", err.getvalue())

    @unittest.skipUnless(EXCEL_REAL, "pon el Operation Planner (.xlsx) en datos_prueba/")
    def test_sin_ruta_usa_el_excel_de_datos_prueba(self):
        texto, g = correr("--coordinador", "Andres Quitian", "--api-key", "K")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            esperado = sum(round(c.visitas_mes) for c in cargar_clientes(EXCEL_REAL) if c.coordinador == "Andres Quitian" and not por_llamada(c))
        self.assertEqual(len(visitas(texto)), esperado)  # todas las visitas del mes de sus 15 clientes

    @unittest.skipUnless(EXCEL_REAL, "pon el Operation Planner (.xlsx) en datos_prueba/")
    def test_con_excel_real_cada_coordinador(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            todos = cargar_clientes(EXCEL_REAL)
        total = 0
        with mock.patch("ejecutar.cargar_clientes", return_value=todos):  # el Excel se lee una sola vez
            for coord, cs in por_coordinador(todos).items():
                with self.subTest(coord=coord):
                    texto, g = correr(str(EXCEL_REAL), "--coordinador", coord, "--api-key", "K")
                    ruta = [c for c in cs if not por_llamada(c)]              # los de 0.25 semanales van por llamada
                    self.assertEqual(len(g.call_args.args[0]), len(ruta))  # solo pidió los clientes de ese coordinador que van en ruta
                    self.assertEqual(len(visitas(texto)), sum(round(c.visitas_mes) for c in ruta))  # y programó todas sus visitas del mes
                    total += len(cs)
        self.assertEqual(total, 103)


# ---------------------------------------------------------------- medición con 100 clientes
class TestCienClientes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clientes, cls.matriz, cls.coords = generar(100, seed=2026)
        cls.ruta = planificar(cls.clientes, cls.matriz)

    def test_reglas(self):
        revisar_reglas(self, self.clientes, self.matriz, self.ruta)

    def test_jornadas(self):
        dias = jornadas(self.clientes, self.matriz, self.coords)
        revisar_jornadas(self, self.clientes, self.matriz, self.coords, dias)
        self.assertGreater(len(dias), 1)

    def test_calendario_del_mes(self):
        semanas = calendario(self.clientes, self.matriz, self.coords)
        revisar_calendario(self, self.clientes, semanas)
        por_semana = [sum(map(len, s)) for s in semanas]
        self.assertLess(max(por_semana), 1.5 * min(por_semana), por_semana)  # el mes queda repartido, no cargado en una semana

    def test_hay_ambos_grupos(self):
        self.assertTrue(0 < sum(puntaje(c, len(self.clientes)) > UMBRAL for c in self.clientes) < 100)

    def test_cola_por_cercania_vence_al_azar(self):
        k = sum(puntaje(c, len(self.clientes)) > UMBRAL for c in self.clientes)
        cola, desde = self.ruta[k:], self.ruta[k - 1]
        self.assertGreaterEqual(len(cola), 5)  # con visitas, pocos clientes quedan sin factores
        rnd, azar = random.Random(1), []
        for _ in range(200):
            mezcla = cola[:]
            rnd.shuffle(mezcla)
            azar.append(minutos(mezcla, self.matriz, desde))
        self.assertLess(minutos(cola, self.matriz, desde), 0.8 * statistics.mean(azar))  # al menos 20% menos viaje que al azar (la cola es corta, ~8 clientes)

    def test_determinista(self):
        self.assertEqual(self.ruta, planificar(self.clientes, self.matriz))


class TestCienSemillas(unittest.TestCase):
    def test_reglas_en_100_escenarios(self):
        for seed in range(100):
            n = random.Random(seed).randint(3, 35)
            with self.subTest(seed=seed, n=n):
                cs, m, co = generar(n, seed)
                revisar_reglas(self, cs, m, planificar(cs, m))
                revisar_jornadas(self, cs, m, co, jornadas(cs, m, co))
                revisar_calendario(self, cs, calendario(cs, m, co))


class TestClientesReales(unittest.TestCase):
    """Clientes activos reales por coordinador; la distancia es una aproximación por zona (UBICACIÓN)."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
        cls.grupos = {}
        for f in filas:
            c = Cliente(f["nombre"], f["direccion"], f["criticidad"], f["renovacion_automatica"] == "1",
                        float(f["personas"]), f["coordinador"], float(f["visitas_mes"]))
            zona = f["zona"].strip().lower()
            pos = posicion(zona if zona in ZONAS else "norte", random.Random(c.nombre))
            cls.grupos.setdefault(c.coordinador, []).append((c, pos))

    def test_cada_coordinador(self):
        self.assertEqual(len(self.grupos), 7)
        for coord, items in self.grupos.items():
            with self.subTest(coord=coord):
                cs = [c for c, _ in items]
                co = [p for _, p in items]
                m = matriz_coordenadas(co)
                revisar_reglas(self, cs, m, planificar(cs, m))
                revisar_jornadas(self, cs, m, co, jornadas(cs, m, co))
                revisar_calendario(self, cs, calendario(cs, m, co))

    def test_reporta_cuantos_quedan_para_la_distancia(self):
        todos = [c for items in self.grupos.values() for c, _ in items]
        print(f"\n[real] {sum(puntaje(c, len(todos)) <= UMBRAL for c in todos)} de {len(todos)} clientes activos quedan sin puntaje de urgencia (los ordena la distancia)")
        self.assertEqual(len(todos), 103)


if __name__ == "__main__":
    unittest.main(verbosity=2)

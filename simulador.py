"""Simulador en tiempo real: cómo responde la operación de visitas con 10% de clientes críticos, 60% estables y 30% excelentes.

Cada "tick" es un día hábil. Cada día los clientes acumulan visitas pendientes (visitas_mes / 20), pueden tener un
incidente (más probable cuanto más crítico) y cada coordinador atiende, por puntaje de prioridad, hasta CAPACIDAD visitas.

Uso:  python simulador.py [--dias 60] [--segundos 0.3] [--semilla 7]   (--segundos 0 = sin pausa)
"""
import argparse
import csv
import math
import os
import random
import time
from dataclasses import replace
from pathlib import Path

from visitas.algoritmo import Cliente, puntaje

CSV = Path(__file__).parent / "datos_prueba" / "clientes_activos.csv"
PORCENTAJES = {"critico": 0.10, "estable": 0.60, "excelente": 0.30}  # crítico es un mínimo: se redondea hacia arriba
P_INCIDENTE = {"critico": 0.06, "estable": 0.02, "excelente": 0.005}  # por cliente y día
DIAS_HABILES_MES = 20
CAPACIDAD = 5  # visitas por coordinador y día


def cargar(ruta=CSV):
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return [Cliente(r["nombre"], r["direccion"], r["criticidad"], r["renovacion_automatica"] == "1",
                        float(r["personas"]), r["coordinador"], float(r["visitas_mes"]))
                for r in csv.DictReader(f)]


def asignar_criticidad(clientes, rng):
    """Reparte la criticidad al azar respetando 10/60/30 (críticos: al menos el 10%)."""
    n = len(clientes)
    n_crit, n_exc = math.ceil(PORCENTAJES["critico"] * n), round(PORCENTAJES["excelente"] * n)
    niveles = ["critico"] * n_crit + ["excelente"] * n_exc + ["estable"] * (n - n_crit - n_exc)
    rng.shuffle(niveles)
    return [replace(c, criticidad=k) for c, k in zip(clientes, niveles)]


class Simulacion:
    def __init__(self, clientes, rng):
        self.rng, self.dia, self.total, self.todos = rng, 0, len(clientes), clientes
        self.clientes = [c for c in clientes if c.visitas_mes > 1]  # 0 = sin visitas; 1 al mes = por llamada, fuera de la ruta
        self.pendiente = {c.nombre: 0.0 for c in self.clientes}
        self.debidas = dict.fromkeys(PORCENTAJES, 0.0)   # visitas que correspondían, por criticidad
        self.hechas = dict.fromkeys(PORCENTAJES, 0)
        self.incidentes = dict.fromkeys(PORCENTAJES, 0)
        self.por_coord = {}
        for c in self.clientes:
            self.por_coord.setdefault(c.coordinador, []).append(c)

    def paso(self):
        self.dia += 1
        for c in self.clientes:
            nuevo = c.visitas_mes / DIAS_HABILES_MES
            if self.rng.random() < P_INCIDENTE[c.criticidad]:
                nuevo += 1
                self.incidentes[c.criticidad] += 1
            self.pendiente[c.nombre] += nuevo
            self.debidas[c.criticidad] += nuevo
        for cs in self.por_coord.values():
            n = len(cs)
            urgentes = sorted((c for c in cs if self.pendiente[c.nombre] >= 1),
                              key=lambda c: puntaje(c, n) * self.pendiente[c.nombre], reverse=True)
            for c in urgentes[:CAPACIDAD]:
                self.pendiente[c.nombre] -= 1
                self.hechas[c.criticidad] += 1

    def tablero(self):
        total = self.total
        lineas = [f"Día hábil {self.dia}   clientes: {total} (en ruta: {len(self.clientes)})   capacidad: {CAPACIDAD} visitas/coordinador/día", ""]
        lineas.append(f"{'criticidad':<10} {'clientes':>8} {'%':>6} {'cumplimiento':>13} {'pendientes':>11} {'incidentes':>11}")
        for k in PORCENTAJES:
            nombres = {c.nombre for c in self.clientes if c.criticidad == k}
            n_k = sum(c.criticidad == k for c in self.todos)
            cumpl = 100 * self.hechas[k] / self.debidas[k] if self.debidas[k] else 100
            pend = sum(self.pendiente[n] for n in nombres)
            lineas.append(f"{k:<10} {n_k:>8} {100 * n_k / total:>5.1f}% {cumpl:>12.1f}% {pend:>11.1f} {self.incidentes[k]:>11}")
        return "\n".join(lineas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=60)
    ap.add_argument("--segundos", type=float, default=0.3, help="pausa real entre días simulados")
    ap.add_argument("--semilla", type=int, default=None)
    a = ap.parse_args(argv)
    rng = random.Random(a.semilla)
    sim = Simulacion(asignar_criticidad(cargar(), rng), rng)
    os.system("")  # activa los códigos ANSI en la consola de Windows
    for _ in range(a.dias):
        sim.paso()
        print("\033[H\033[J" + sim.tablero(), flush=True)
        time.sleep(a.segundos)


if __name__ == "__main__":
    main()

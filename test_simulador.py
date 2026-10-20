import random

from simulador import Simulacion, asignar_criticidad, cargar


def test_reparto_10_60_30_y_simulacion_corre():
    rng = random.Random(1)
    cs = asignar_criticidad(cargar(), rng)
    n = len(cs)
    cuenta = {k: sum(c.criticidad == k for c in cs) for k in ("critico", "estable", "excelente")}
    assert cuenta["critico"] >= 0.10 * n
    assert abs(cuenta["excelente"] - 0.30 * n) <= 1
    assert abs(cuenta["estable"] - 0.60 * n) <= 2
    sim = Simulacion(cs, rng)
    for _ in range(20):
        sim.paso()
    assert sum(sim.hechas.values()) > 0

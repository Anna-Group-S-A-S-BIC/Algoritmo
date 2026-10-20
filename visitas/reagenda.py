"""Reagendación de visitas no realizadas.

Regla: si el cliente es crítico y tiene más de 4 personas, la reagendación es inmediata; en cualquier otro caso
se crea una solicitud de tipo PQR en el módulo de PQRS y se notifica por correo a la directora, que debe aceptarla
(o rechazarla) dejando la razón. Solo al aceptarse se mueve la visita.

La conexión real con el módulo de PQRS y con el correo no está aquí: `pqrs` y `notificar` se inyectan (ver
ClientePQRS). Los eventos de la solicitud los guarda el módulo de PQRS, que es la fuente de verdad.
"""
import datetime
from dataclasses import dataclass

from .algoritmo import MIN_VISITAS, Parada, geocerca, norm
from .mapas import metros

PERSONAS_REAGENDA_INMEDIATA = 4  # "más de 4 personas" (estricto)


@dataclass
class SolicitudPQR:
    cliente: str
    coordinador: str
    semana: int          # semana (1-4) y jornada (1..n) de la visita que no se pudo hacer
    jornada: int
    motivo: str
    fecha: str
    estado: str = "pendiente"   # pendiente | aceptada | rechazada | aprobada_automatica
    razon_decision: str = ""
    id: str = ""
    tipo: str = "PQR"


@dataclass
class Reagenda:
    semanas: list        # cronograma nuevo (el original no se modifica)
    semana: int          # dónde quedó la visita (1-4)
    jornada: int
    avisos: list


class PQRSEnMemoria:
    """Sustituto para pruebas y demos del módulo de PQRS. El real debe ofrecer lo mismo: crear y resolver."""

    def __init__(self):
        self.solicitudes = {}

    def crear(self, solicitud):
        solicitud.id = f"PQR-{len(self.solicitudes) + 1:04d}"
        self.solicitudes[solicitud.id] = solicitud
        return solicitud.id

    def resolver(self, id_, estado, razon):
        self.solicitudes[id_].estado, self.solicitudes[id_].razon_decision = estado, razon


def requiere_aprobacion(cliente):
    """False (reagendación inmediata) solo para clientes críticos con más de 4 personas."""
    return not (cliente.criticidad == "critico" and cliente.personas > PERSONAS_REAGENDA_INMEDIATA)


def reagendar(semanas, nombre, semana, jornada):
    """Mueve la visita no realizada de `nombre` (semana y jornada 1-based) a la jornada posterior más conveniente.

    Candidatas: jornadas posteriores del mes donde el cliente no esté ya. Se prefiere la del mismo sector (localidad)
    y, entre esas, la que tenga una parada más cercana; la visita entra al final de la jornada elegida (el orden
    óptimo de esa jornada no se recalcula). Devuelve un Reagenda; el cronograma recibido no se modifica.
    """
    nuevas = [[list(d) for d in s] for s in semanas]
    try:
        origen = nuevas[semana - 1][jornada - 1]
    except IndexError:
        raise ValueError(f"no existe la semana {semana}, jornada {jornada}") from None
    parada = next((p for p in origen if norm(p.cliente.nombre) == norm(nombre)), None)
    if parada is None:
        raise ValueError(f"{nombre!r} no está en la semana {semana}, jornada {jornada}")
    posteriores = [(s, j) for s in range(semana, 5) for j in range(1, len(nuevas[s - 1]) + 1) if (s, j) > (semana, jornada)]
    candidatas = [(s, j) for s, j in posteriores if all(norm(p.cliente.nombre) != norm(nombre) for p in nuevas[s - 1][j - 1])]
    if not candidatas:
        raise ValueError(f"no hay jornadas posteriores este mes para reagendar a {nombre!r}: pasa al mes siguiente")

    def clave(sj):
        paradas = nuevas[sj[0] - 1][sj[1] - 1]
        otro_sector = not any(p.sector and p.sector == parada.sector for p in paradas) if parada.sector else False
        dist = min((metros(parada.coordenadas, p.coordenadas) for p in paradas if p.coordenadas and parada.coordenadas), default=float("inf"))
        return (otro_sector, dist, sj)

    s, j = min(candidatas, key=clave)
    avisos = []
    origen.remove(parada)
    if origen and len(origen) < MIN_VISITAS:
        avisos.append(f"la jornada {jornada} de la semana {semana} queda con {len(origen)} visitas, bajo el mínimo de {MIN_VISITAS}")
    destino = nuevas[s - 1][j - 1]
    previa = destino[-1] if destino else None
    m = metros(previa.coordenadas, parada.coordenadas) if previa and previa.coordenadas and parada.coordenadas else None
    destino.append(parada._replace(metros=m, geocerca=None if m is None else geocerca(m)))
    return Reagenda(nuevas, s, j, avisos)


def solicitar_reagenda(semanas, cliente, semana, jornada, motivo, coordinador, pqrs, notificar, directora_email, hoy=None):
    """Punto de entrada del botón "Reagendar" del frontend. Devuelve (solicitud, Reagenda o None).

    Crítico con más de 4 personas: se reagenda ya (solicitud 'aprobada_automatica'). Otro cliente: se crea la PQR en el
    módulo de PQRS, se avisa a la directora por correo y queda 'pendiente' hasta resolver_solicitud.
    """
    if not motivo or not motivo.strip():
        raise ValueError("el motivo de la reagendación es obligatorio")
    sol = SolicitudPQR(cliente.nombre, coordinador, semana, jornada, motivo.strip(), str(hoy or datetime.date.today()))
    if not requiere_aprobacion(cliente):
        res = reagendar(semanas, cliente.nombre, semana, jornada)
        sol.estado, sol.razon_decision = "aprobada_automatica", "Cliente crítico con más de 4 personas: reagendación inmediata"
        return sol, res
    sol.id = pqrs.crear(sol)
    notificar(directora_email, f"[{sol.id}] PQR: aceptar reagendación de {cliente.nombre}",
              f"{coordinador} solicita reagendar la visita de {cliente.nombre} (semana {semana}, jornada {jornada}).\n"
              f"Motivo: {sol.motivo}\nAcéptela o recházela, con su razón, en el módulo de PQRS.")
    return sol, None


def resolver_solicitud(solicitud, semanas, aceptada, razon, pqrs):
    """Decisión de la directora. La razón es obligatoria (aceptar o rechazar). Si acepta, mueve la visita."""
    if solicitud.estado != "pendiente":
        raise ValueError(f"la solicitud {solicitud.id} ya está {solicitud.estado}")
    if not razon or not razon.strip():
        raise ValueError("la razón de la decisión es obligatoria")
    res = reagendar(semanas, solicitud.cliente, solicitud.semana, solicitud.jornada) if aceptada else None
    solicitud.estado, solicitud.razon_decision = ("aceptada" if aceptada else "rechazada"), razon.strip()
    pqrs.resolver(solicitud.id, solicitud.estado, solicitud.razon_decision)
    return res

"""Genera el cronograma mensual de visitas de un coordinador.

Filtra del Excel los clientes activos de ese coordinador, los georeferencia con Google Maps y arma el mes:
4 semanas de jornadas diarias, donde cada cliente recibe las visitas que tiene definidas.

Uso:
  python ejecutar.py --coordinador "Juan Roberto" --api-key TU_CLAVE
  python ejecutar.py "C:\\ruta con espacios\\Operation Planner.xlsx" --coordinador "Juan Roberto" --api-key TU_CLAVE
Sin ruta usa el Operation Planner que esté en datos_prueba/. Si la ruta tiene espacios, va entre comillas.
La clave también se puede dar con la variable de entorno GOOGLE_MAPS_API_KEY.
"""
import argparse
import datetime
import os
import sys
import warnings
from pathlib import Path

from visitas.algoritmo import ALGORITMOS, DIAS_POR_SEMANA, MIN_VISITAS, cronograma, norm, por_llamada
from visitas.datos import cargar_casas, cargar_clientes, primer_dia_a_programar
from visitas.mapas import geocodificar, matriz_google
from visitas.reagenda import PQRSEnMemoria, requiere_aprobacion, solicitar_reagenda

EXCEL_POR_DEFECTO = next(iter(sorted((Path(__file__).parent / "datos_prueba").glob("Operation Planner*.xlsx"))), None)


CRITICIDAD_POR_DEFECTO = next(iter(sorted((Path(__file__).parent / "datos_prueba").glob("criticidad*.csv"))), None)


CASAS_POR_DEFECTO = next(iter(sorted((Path(__file__).parent / "datos_prueba").glob("casas*.csv"))), None)


def fallar(mensaje):
    print(f"Error: {mensaje}", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", nargs="?", default=EXCEL_POR_DEFECTO, help="Excel del Operation Planner (por defecto, el de datos_prueba/)")
    ap.add_argument("--coordinador", required=True)
    ap.add_argument("--criticidad", default=CRITICIDAD_POR_DEFECTO, help="CSV cliente,coordinador,criticidad del mes (por defecto, el de datos_prueba/)")
    ap.add_argument("--algoritmo", choices=ALGORITMOS, default="v2", help="v2 (por defecto): prioridad + sectorización por localidad y zonas. casa: cada jornada parte de la casa del coordinador (necesita --casas)")
    ap.add_argument("--casas", default=CASAS_POR_DEFECTO, help="CSV coordinador,direccion con la casa de cada coordinador: cada jornada parte de ahí (por defecto, el casas*.csv de datos_prueba/)")
    ap.add_argument("--fecha", type=datetime.date.fromisoformat, default=None, help="día 1 del mes a programar, AAAA-MM-DD: desde ahí se cuentan los días para vencer cada contrato (por defecto, el próximo día 1)")
    ap.add_argument("--reagendar", metavar="CLIENTE|SEMANA|JORNADA", help="reagenda la visita que no se pudo hacer (funciona con los dos algoritmos). Ej.: \"Navarra|1|2\"")
    ap.add_argument("--motivo", help="motivo de la reagendación (obligatorio con --reagendar)")
    ap.add_argument("--directora-email", help="correo de la directora, a quien se avisa cuando la reagendación requiere su aceptación (PQR)")
    ap.add_argument("--api-key", default=os.environ.get("GOOGLE_MAPS_API_KEY"))
    a = ap.parse_args(argv)
    if not a.api_key:
        ap.error("falta la clave de Google Maps (--api-key o GOOGLE_MAPS_API_KEY)")
    if not a.xlsx:
        fallar("indica la ruta del Excel: no hay ningún Operation Planner en datos_prueba/")
    if not Path(a.xlsx).is_file():
        fallar(f"no encuentro el Excel {str(a.xlsx)!r} (carpeta actual: {Path.cwd()}). Si la ruta tiene espacios, ponla entre comillas.")
    a.fecha = a.fecha or primer_dia_a_programar()
    with warnings.catch_warnings(record=True) as avisos:
        warnings.simplefilter("always")
        clientes = cargar_clientes(a.xlsx, criticidad_csv=a.criticidad, hoy=a.fecha)
    for w in avisos:
        if "excluidos" in str(w.message):
            print(f"Aviso: {w.message}")
    disponibles = sorted({c.coordinador for c in clientes})
    if norm(a.coordinador) not in {norm(d) for d in disponibles}:
        fallar(f"coordinador {a.coordinador!r} no encontrado. Disponibles: {', '.join(disponibles)}")
    casa = None
    if a.algoritmo == "casa":
        if not a.casas or not Path(a.casas).is_file():
            fallar(f"el algoritmo 'casa' necesita el archivo de casas (--casas); no encuentro {str(a.casas)!r}")
        casa = cargar_casas(a.casas).get(norm(a.coordinador))
        if not casa:
            fallar(f"{a.coordinador} no tiene casa en {Path(a.casas).name}: agrégala (coordinador,direccion) o usa --algoritmo v2")
    try:
        semanas = cronograma(clientes, a.coordinador,
                             lambda dirs: matriz_google(dirs, a.api_key),
                             lambda dirs: geocodificar(dirs, a.api_key), casa=casa, algoritmo=a.algoritmo)
    except (ValueError, OSError) as e:  # ValueError: pocos clientes, rechazo de Google o dirección sin ubicar; OSError: sin conexión
        fallar(str(e))
    todos = [c for c in clientes if norm(c.coordinador) == norm(a.coordinador)]
    propios = [c for c in todos if not por_llamada(c)]
    total = sum(len(d) for s in semanas for d in s)
    print(f"Cronograma del mes de {propios[0].coordinador}: {len(propios)} clientes, {total} visitas (algoritmo {a.algoritmo}; contratos medidos desde el {a.fecha})")
    if casa:
        print(f"Cada jornada sale de la casa del coordinador: {casa}")
    llamadas = [c.nombre for c in todos if por_llamada(c)]
    if llamadas:
        print(f"Por llamada (0.25 visitas semanales), fuera de la ruta: {', '.join(llamadas)}")
    sin = [c.nombre for c in propios if round(c.visitas_mes) <= 0]
    if sin:
        print(f"Aviso: sin visitas definidas en el Excel, no se programan: {', '.join(sin)}")
    imprimir(semanas, casa)
    if a.reagendar:
        reagendar_desde_cli(a, propios, semanas, casa)


def imprimir(semanas, casa):
    vistos = set()
    for n, semana in enumerate(semanas, 1):
        exceso = f"  - NO CABE: excede los {DIAS_POR_SEMANA} días hábiles" if len(semana) > DIAS_POR_SEMANA else ""
        print(f"\nSEMANA {n}: {len(semana)} jornadas, {sum(map(len, semana))} visitas{exceso}")
        for k, dia in enumerate(semana, 1):
            corto = f", bajo el mínimo de {MIN_VISITAS}" if len(dia) < MIN_VISITAS else ""
            sectores = ", ".join(dict.fromkeys(p.sector for p in dia if p.sector))
            print(f"  Jornada {k} ({len(dia)} visitas{corto})" + (f" - {sectores}" if sectores else ""))
            for j, p in enumerate(dia, 1):
                de = "de la casa" if (j == 1 and casa) else "del anterior"
                cerca = "" if p.metros is None else f"  a {p.metros:.0f} m {de} ({p.geocerca})"
                print(f"    {j:2}. {p.cliente.nombre}  (puntaje {p.puntaje:.3f}){cerca}")
                if p.cliente.nombre not in vistos:  # la dirección que entendió Google, solo la primera vez
                    print(f"        {p.ubicacion}")
                    vistos.add(p.cliente.nombre)



def reagendar_desde_cli(a, propios, semanas, casa):
    """--reagendar CLIENTE|SEMANA|JORNADA --motivo ...: mismo flujo para los dos algoritmos (ver visitas/reagenda.py).

    Crítico con más de 4 personas: se mueve ya y se imprime el cronograma nuevo. Otro cliente: se crea la PQR y se avisa
    a la directora (aquí con el módulo de PQRS y el correo simulados: solo se imprimen); el cronograma no cambia.
    """
    try:
        nombre, semana, jornada = (x.strip() for x in a.reagendar.split("|"))
        semana, jornada = int(semana), int(jornada)
    except ValueError:
        fallar("--reagendar va como CLIENTE|SEMANA|JORNADA, por ejemplo \"Navarra|1|2\"")
    if not a.motivo:
        fallar("--reagendar necesita --motivo")
    cliente = next((c for c in propios if norm(c.nombre) == norm(nombre)), None)
    if cliente is None:
        fallar(f"{nombre!r} no es cliente de {a.coordinador}")
    if requiere_aprobacion(cliente) and not a.directora_email:
        fallar(f"{cliente.nombre} requiere aceptación de la directora (PQR): indica --directora-email")
    try:
        sol, res = solicitar_reagenda(semanas, cliente, semana, jornada, a.motivo, a.coordinador, PQRSEnMemoria(),
                                      lambda para, asunto, cuerpo: print(f"\nCorreo a {para}: {asunto}"), a.directora_email)
    except ValueError as e:
        fallar(str(e))
    if res is None:
        print(f"Solicitud {sol.id} (tipo {sol.tipo}) pendiente de aceptación de la directora; el cronograma no cambia.")
        return
    print(f"\nREAGENDADA: {cliente.nombre} pasa a la semana {res.semana}, jornada {res.jornada} ({sol.razon_decision}).")
    for aviso in res.avisos:
        print(f"Aviso: {aviso}")
    print("\nCRONOGRAMA ACTUALIZADO")
    imprimir(res.semanas, casa)


if __name__ == "__main__":
    main()

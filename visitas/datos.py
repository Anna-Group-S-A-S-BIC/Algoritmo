"""Carga de clientes desde el Excel del Operation Planner."""
import csv
import datetime
import re
import warnings

import pandas as pd

from .algoritmo import Cliente, norm

HOJA = "CLIENTES ACTIVOS  2025"
HOJA_CRITICOS = "CRONOGRAMA VISITAS CLIENTES CRI"
COL_VISITAS = "CANT. VISITAS SEMANALES"
COL_VENCE = "TIEMPO DE VENCIMIENTO"  # texto "Vence en 75 días" (negativo = ya vencido), contado desde COL_FECHA
COL_FECHA = "FECHA AL DIA"
SEMANAS_POR_MES = 4  # visitas al mes = visitas semanales x 4


def _clave(nombre):
    """Nombre sin tildes, puntuación ni espacios: 'Bahía  Solero' == 'bahia-solero'."""
    return re.sub(r"[^a-z0-9]", "", norm(nombre))


def primer_dia_a_programar(hoy=None):
    """El cronograma se planea la semana anterior al día 1: los días se cuentan desde el próximo día 1 (o desde hoy, si es 1)."""
    hoy = hoy or datetime.date.today()
    return hoy if hoy.day == 1 else (hoy.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)


def cargar_casas(ruta):
    """CSV coordinador,direccion -> {coordinador sin tildes ni mayúsculas: dirección de su casa}. Filas sin dirección se ignoran."""
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return {norm(r["coordinador"]): " ".join(r["direccion"].split()) + ", Colombia"
                for r in csv.DictReader(f) if r.get("coordinador") and (r.get("direccion") or "").strip()}


def cargar_criticidad(ruta):
    """CSV cliente,coordinador,criticidad -> {clave: (coordinador, criticidad o '')}."""
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return {_clave(r["cliente"]): (r["coordinador"].strip(), norm(r["criticidad"])) for r in csv.DictReader(f)}


def cargar_clientes(xlsx, hoja=HOJA, hoja_criticos=HOJA_CRITICOS, criticidad_csv=None, hoy=None):
    """Clientes con STATUS = Activo, con coordinador, dirección y CANT. PERSONAS.

    Criticidad: 'critico' si el cliente figura en la hoja de críticos, si no 'estable' (el Excel
    no trae criticidad por cliente). Renovación automática vacía se toma como NO. `hoy` es el día 1 del mes que se programa (por defecto,
    el próximo día 1): desde ahí se cuentan los días hasta el fin del contrato (columna TIEMPO DE VENCIMIENTO).
    Con `criticidad_csv` (tabla del mes) manda su criticidad; el coordinador siempre es el del Excel
    (la columna coordinador de la tabla no es fiable). Vacíos o ausentes conservan lo del Excel.
    """
    d = pd.read_excel(xlsx, sheet_name=hoja)
    d.columns = [str(c).strip() for c in d.columns]
    d = d[d["STATUS"].astype(str).str.strip().str.lower() == "activo"]
    completos = d.dropna(subset=["COORDINADOR", "DIRECCIÓN", "CANT. PERSONAS"])
    if len(completos) < len(d):
        faltan = sorted(set(d["CENTRO DE COSTO"]) - set(completos["CENTRO DE COSTO"]))
        warnings.warn(f"{len(faltan)} clientes activos sin coordinador/dirección/personas, excluidos: {faltan}")

    criticos = set()
    if hoja_criticos in pd.ExcelFile(xlsx).sheet_names:
        c = pd.read_excel(xlsx, sheet_name=hoja_criticos)
        criticos = {norm(n) for n in c.loc[c["ESTADO"].astype(str).str.strip().str.upper() == "CRITICO", "CENTRO DE COSTO"]}

    hoy = pd.Timestamp(hoy or primer_dia_a_programar())

    def dias_para_vencer(r):
        n = re.search(r"-?\d+", str(r.get(COL_VENCE)))
        if not n:
            return None
        al_dia = pd.to_datetime(r.get(COL_FECHA), errors="coerce")  # a esa fecha se calculó el texto del Excel
        return float(n.group()) - (hoy - (al_dia if pd.notna(al_dia) else pd.Timestamp(datetime.date.today()))).days

    tabla = cargar_criticidad(criticidad_csv) if criticidad_csv else {}

    def de_tabla(nombre, i):
        return tabla.get(_clave(nombre), ("", ""))[i]

    return [
        Cliente(
            nombre=str(r["CENTRO DE COSTO"]).strip(),
            direccion=" ".join(str(r["DIRECCIÓN"]).split()) + ", Colombia",
            criticidad=de_tabla(r["CENTRO DE COSTO"], 1) or ("critico" if norm(r["CENTRO DE COSTO"]) in criticos else "estable"),
            renovacion_automatica=str(r["RENOVACIÓN AUTOMATICA"]).strip().upper() == "SI",
            personas=float(r["CANT. PERSONAS"]),
            coordinador=str(r["COORDINADOR"]).strip(),
            dias_para_vencer=dias_para_vencer(r),
            visitas_mes=0.0 if pd.isna(r.get(COL_VISITAS)) else float(r[COL_VISITAS]) * SEMANAS_POR_MES,
        )
        for _, r in completos.iterrows()
    ]


def por_coordinador(clientes):
    grupos = {}
    for c in clientes:
        grupos.setdefault(c.coordinador, []).append(c)
    return grupos

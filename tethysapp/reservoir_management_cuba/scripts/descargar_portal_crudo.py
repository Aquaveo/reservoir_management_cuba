"""Descarga CRUDA de la serie historica de un embalse desde el portal SGIA del INRH
(https://portal-sgia.hidro.gob.cu).

Este script NO limpia nada. Escribe tal cual lo que devuelve el portal:

  * todas las filas, incluidas las fechas repetidas
  * todos los campos, con el nombre que trae el origen
  * en el mismo orden en que el portal las entrega
  * sin convertir unidades, sin rellenar huecos, sin descartar nada

La unica pregunta que resuelve es la de paginacion: el portal solo deja pedir
un mes a la vez, asi que el script itera mes por mes y pega las respuestas.

Agrega dos columnas al final, con guion bajo delante para que se distingan de
los campos del origen:

    _consulta   el mes que se pidio (AAAA-MM) y la pagina, p.ej. "2015-01 p1"
    _orden      posicion de la fila dentro de la descarga completa

Sirven para rastrear de donde salio cada fila y para conservar el orden
original aunque despues ordenes el CSV en Excel. Si no las quieres, usa
--sin-rastro.

USO
---
    python descargar_portal_crudo.py                    # Zaza, 1993 a hoy
    python descargar_portal_crudo.py --ide 217          # Mayari
    python descargar_portal_crudo.py --desde 2010       # probar mas atras
    python descargar_portal_crudo.py --todos            # los 13 priorizados

SALIDA
------
    <Embalse>_portal_crudo.csv

UNIDADES DEL ORIGEN (no se tocan; se documentan aqui nada mas)
--------------------------------------------------------------
    nivel ........... m
    volumen ......... hm3
    entrega ......... hm3/dia
    vertimiento ..... m3/s       (x 0.0864 para pasar a hm3/dia)
    desembalse ...... sin confirmar; el INRH lo rotula hm3/dia, pero hay
                      casos que solo cuadran si estuviera en m3/s

Requiere solo la biblioteca estandar de Python 3.
"""

import argparse
import csv
import datetime as dt
import json
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


# --------------------------------------------------------------- rutas del proyecto


def nombre_seguro(nombre):
    """Nombre de carpeta/archivo sin tildes ni caracteres especiales: Mayarí -> Mayari.

    Tiene que coincidir con el campo Embalse del shapefile (que va sin tildes),
    porque de ahi salen las carpetas de GEOGLOWS y del balance.
    """
    s = unicodedata.normalize("NFKD", str(nombre)).encode("ascii", "ignore").decode("ascii")
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in s).strip()

def carpeta_proyecto():
    """Raiz del proyecto = la carpeta que contiene a Scripts/.

    Permite que los scripts vivan en Scripts/ y escriban en las carpetas de datos
    sin que haya que pasarles rutas a mano.
    """
    aqui = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(aqui).lower() == "scripts":
        return os.path.dirname(aqui)
    return aqui


def carpeta_datos(*partes, crear=True):
    """Ruta a una subcarpeta de datos del proyecto, creandola si hace falta."""
    ruta = os.path.join(carpeta_proyecto(), *partes)
    if crear:
        os.makedirs(ruta, exist_ok=True)
    return ruta


CRUDO = ("Operative_Forecast", "Raw_Data")
LIMPIO = ("Operative_Forecast", "Clean_Data")


BASE = "https://portal-sgia.hidro.gob.cu/reportserver/reportserver/httpauthexport"
PARAMS_FIJOS = {
    "user": "geoportal",
    "apikey": "geoportal",
    "format": "json",
    "download": "false",
    "key": "embalse_serie_historica",
}

PRIORIZADOS = {
    108: "Zaza", 217: "Mayari", 37: "Alacranes", 70: "Cauto El Paso",
    36: "Hanabanilla", 195: "Protesta de Baragua", 64: "Carlos Manuel de Cespedes",
    163: "Jimaguayu", 117: "Aviles", 162: "Porvenir", 220: "La Yaya (GTM)",
    69: "Bueycito", 99: "Mamposton",
}



# ------------------------------------------------------- descarga incremental

def _ruta_destino(carpeta, nombre, salida=None):
    seguro = nombre_seguro(nombre)
    destino = os.path.join(carpeta, seguro)
    os.makedirs(destino, exist_ok=True)
    return os.path.join(destino, salida or (seguro + "_portal_crudo.csv")), seguro


def leer_crudo(ruta):
    """Lee un crudo anterior. Devuelve (filas, campos) o (None, None)."""
    if not os.path.exists(ruta):
        return None, None
    try:
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            lector = csv.DictReader(f)
            campos = [c for c in (lector.fieldnames or []) if not c.startswith("_")]
            return list(lector), campos
    except Exception as e:
        print("  ! no se pudo leer el crudo anterior: %s" % e)
        return None, None


VOLATILES = {"total_count", "porcientollenado"}


def _norm(v):
    """Valor comparable: los numeros como numero, el resto como texto."""
    if v is None:
        return ""
    t = str(v).strip()
    try:
        return repr(float(t))
    except ValueError:
        return t


def fusionar(viejas, nuevas, campos):
    """Une lo que ya habia con lo recien bajado, SIN perder ni duplicar nada.

    La fusion es por CONTENIDO COMPLETO de la fila, no por fecha. El portal
    devuelve legitimamente varios registros para una misma fecha (las lecturas
    cada 2 horas de los dias de crecida), asi que deduplicar por fecha se
    cargaria justo los datos de los eventos.

    De paso detecta un caso que conviene no perderse: una fila con la misma
    fecha y hora que antes pero con otros valores. Eso significa que el portal
    REVISO un dato viejo. No se decide nada automaticamente — se avisa, y manda
    lo nuevo, que es lo que el portal considera vigente.
    """
    # Las filas viejas vienen del CSV (texto) y las nuevas del JSON (numeros):
    # se comparan normalizadas, para que "0.0" y 0, o "1180.0" y 1180, sean lo
    # mismo. Y se dejan fuera los campos que el portal recalcula en cada
    # consulta sin que cambie el dato: total_count (paginacion) y
    # porcientollenado (el llenado ACTUAL del embalse, repetido en cada fila).
    clave = lambda r: tuple(_norm(r.get(c)) for c in campos if c not in VOLATILES)
    fh = lambda r: (_norm(r.get("fecha")), _norm(r.get("hora")))

    viejas = viejas or []
    por_clave = {clave(r): r for r in viejas}
    por_fh = {}
    for r in viejas:
        por_fh.setdefault(fh(r), []).append(r)

    revisadas, agregadas = [], 0
    for r in nuevas:
        k = clave(r)
        if k in por_clave:
            continue                                  # identica: ya estaba
        previas = por_fh.get(fh(r), [])
        if previas:                                   # misma fecha+hora, otro valor
            revisadas.append((fh(r), previas[0], r))
        por_clave[k] = r
        agregadas += 1

    # Orden estable: por fecha, luego por hora, luego por el orden de llegada.
    salida = list(por_clave.values())
    salida.sort(key=lambda r: ((r.get("fecha") or ""), (r.get("hora") or "")))
    return salida, agregadas, revisadas


def pedir(ide, anno, mes, pagina, intentos=3, espera=5, timeout=60):
    """Una llamada al portal. Devuelve la lista de registros tal cual."""
    q = dict(PARAMS_FIJOS)
    q.update({
        "pagesize": "500", "page": str(pagina),
        "p_mes": "%02d" % mes, "p_anno": str(anno),
        "p_provincia": "", "p_cuenca": "", "p_acuifero": "",
        "p_embalse": str(ide),
    })
    url = BASE + "?" + urllib.parse.urlencode(q)
    for intento in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                lote = json.loads(r.read().decode("utf-8"))
            return lote if isinstance(lote, list) else []
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            if intento == intentos:
                print("    ! %04d-%02d p%d fallo tras %d intentos: %s"
                      % (anno, mes, pagina, intentos, e))
                return []
            time.sleep(espera * intento)
    return []


def descargar(ide, desde, hasta, carpeta, salida=None, rastro=True, pausa=0.4,
              incremental=True, margen=60):
    hoy = dt.date.today()

    # --- modo incremental: solo lo que falta ---
    nombre_previo = PRIORIZADOS.get(ide) or ("IdE%d" % ide)
    ruta_previa, _ = _ruta_destino(carpeta, nombre_previo, salida)
    viejas, campos_previos = (leer_crudo(ruta_previa) if incremental else (None, None))
    if viejas:
        ult = max((r.get("fecha") or "") for r in viejas)
        corte = (dt.date.fromisoformat(ult) - dt.timedelta(days=margen)) if ult else None
        if corte:
            desde = max(desde, corte.year)
            print("Ya habia %d filas hasta %s. Se rebajan los ultimos %d dias "
                  "(desde %d) y se fusiona." % (len(viejas), ult, margen, desde))
    else:
        print("Embalse IdE %d - de %d a %d" % (ide, desde, hasta))

    filas = []          # registros crudos, en orden
    campos = []         # nombres de campo, en orden de aparicion

    for anno in range(desde, hasta + 1):
        n_anno = 0
        for mes in range(1, 13):
            if anno == hoy.year and mes > hoy.month:
                break
            pagina = 1
            while True:
                lote = pedir(ide, anno, mes, pagina)
                if not lote:
                    break
                for r in lote:
                    for k in r:                     # descubrir campos sin perder ninguno
                        if k not in campos:
                            campos.append(k)
                    r["_consulta"] = "%04d-%02d p%d" % (anno, mes, pagina)
                    filas.append(r)
                n_anno += len(lote)
                # el portal informa el total en total_count del primer registro
                total = lote[0].get("total_count")
                if len(lote) < 500 or not total or n_anno >= int(total):
                    break
                pagina += 1
                if pagina > 20:
                    break
                time.sleep(pausa)
            time.sleep(pausa)
        print("  %d: %d filas" % (anno, n_anno))

    if not filas and not viejas:
        print("\nNo se obtuvo ninguna fila. Revisa el IdE o la conexion.")
        return None

    nombre = ((filas[0].get("embalse") if filas else None)
              or (viejas[0].get("embalse") if viejas else None)
              or PRIORIZADOS.get(ide) or ("IdE%d" % ide)).strip()
    ruta, seguro = _ruta_destino(carpeta, nombre, salida)

    if not campos:
        campos = campos_previos or []
    nuevas_n = len(filas)
    if viejas:
        filas, agregadas, revisadas = fusionar(viejas, filas, campos)
        print("\n  filas que ya estaban ... %d" % len(viejas))
        print("  bajadas ahora ......... %d" % nuevas_n)
        print("  nuevas de verdad ...... %d" % agregadas)
        if revisadas:
            print("  ! el portal REVISO %d registros ya guardados (manda el valor nuevo):"
                  % len(revisadas))
            for (f, h), ant, nue in revisadas[:8]:
                print("      %s %s  nivel %s -> %s | volumen %s -> %s"
                      % (f, h, ant.get("nivel"), nue.get("nivel"),
                         ant.get("volumen"), nue.get("volumen")))

    for i, r in enumerate(filas, 1):
        r["_orden"] = i

    cols = list(campos) + (["_consulta", "_orden"] if rastro else [])
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)

    # Resumen en pantalla. Solo informativo: el archivo no se filtra por esto.
    fechas = [r.get("fecha") for r in filas if r.get("fecha")]
    unicas = set(fechas)
    rep = len(fechas) - len(unicas)
    print("\n--- %s ---" % nombre)
    print("  filas escritas ..... %d" % len(filas))
    print("  fechas distintas ... %d" % len(unicas))
    print("  filas repetidas .... %d  (fechas que aparecen mas de una vez)" % rep)
    print("  campos ............. %d  -> %s" % (len(campos), ", ".join(campos)))
    print("\n  -> %s" % ruta)
    print("\n  Archivo sin filtrar: no se descarto ninguna fila ni se convirtio ninguna unidad.")
    return ruta


def main():
    p = argparse.ArgumentParser(
        description="Descarga cruda, sin filtrar, de la serie diaria de un embalse del portal SGIA.")
    p.add_argument("--ide", type=int, default=108, help="IdE del embalse (108 = Zaza)")
    p.add_argument("--desde", type=int, default=1993,
                   help="anno inicial (def. 1993, que es donde arranca el portal)")
    p.add_argument("--hasta", type=int, default=dt.date.today().year, help="anno final")
    p.add_argument("--salida", default=None, help="nombre del CSV de salida")
    p.add_argument("--carpeta", default=None,
                   help="carpeta donde escribir (def. Operative_Forecast/Raw_Data)")
    p.add_argument("--todos", action="store_true", help="los 13 embalses priorizados")
    p.add_argument("--completo", action="store_true",
                   help="rebajar toda la serie en vez de solo lo nuevo")
    p.add_argument("--margen", type=int, default=60,
                   help="dias hacia atras que se rebajan en el modo incremental (def. 60)")
    p.add_argument("--sin-rastro", action="store_true",
                   help="no agregar las columnas _consulta y _orden")
    a = p.parse_args()

    carpeta = a.carpeta or carpeta_datos(*CRUDO)
    print("Escribiendo en: %s\n" % carpeta)

    ides = sorted(PRIORIZADOS) if a.todos else [a.ide]
    for i, ide in enumerate(ides):
        if i:
            print("\n" + "=" * 60 + "\n")
        descargar(ide, a.desde, a.hasta, carpeta,
                  a.salida if not a.todos else None, rastro=not a.sin_rastro,
                  incremental=not a.completo, margen=a.margen)


if __name__ == "__main__":
    main()

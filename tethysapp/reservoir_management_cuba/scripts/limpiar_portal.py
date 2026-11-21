"""Limpia una descarga cruda del portal SGIA y produce la serie diaria para el balance.

NO toca la red. Recibe el CSV que produjo descargar_portal_crudo.py y deriva todo
de el, asi que el proceso es reproducible y se puede repetir sin volver a descargar.

Orden de las etapas (el orden importa: la curva va primero porque es una propiedad
del vaso y no depende de ninguna decision posterior):

  1. Ajusta la curva nivel-volumen con TODOS los registros crudos, de forma robusta.
     De su pendiente sale el area del espejo de agua, que no hay que pedirle a nadie.
  2. Separa la epoca mensual (registros esporadicos de anos viejos) de la diaria.
  3. Aparta las fechas con varios registros a un archivo de eventos.
  4. Elige una fila por dia: la de las 08:00 completa; si no hay, la unica que exista.
  5. Rejilla de calendario con NaN explicito y horas reales entre lecturas.
  6. Detecta errores de digitacion contra la curva y contra los dias vecinos.
  7. Valida la entrega contra el techo de la ficha y contra el balance en recesion.
  8. Calcula Qin. Lo recorta en cero PERO deja bandera de cada dia recortado.

Que se corrige solo y que no
---------------------------
Se corrige automaticamente SOLO donde el criterio fisico es concluyente:

  * el par nivel/volumen contradice la curva y los vecinos dicen cual de los dos
    esta mal (un digito cambiado)
  * la entrega supera lo que el embalse perdio ese dia en recesion sin vertimiento,
    y dividir por una potencia de diez la devuelve al rango posible (los x10 / x1000)

Todo lo demas se MARCA y se deja intacto. El script no inventa datos donde el
criterio no alcanza para decidir. Cada valor cambiado queda registrado en el
archivo de correcciones con el original, el nuevo, la regla y el motivo.

Advertencias que hay que tener presentes al usar la salida
----------------------------------------------------------
  * El Qin sale SESGADO BAJO. El balance no cierra: falta una salida no reportada
    (filtracion y/o extraccion no contabilizada) estimada entre 12 y 19 mm/dia
    sobre el espejo de agua, de la cual la evaporacion explica 4-6. Por eso ~40%
    de los dias darian Qin negativo y el recorte en cero infla la media ~43%.
    La serie sirve para los cuantiles altos (p90 en adelante, donde el sesgo pesa
    menos del 23%) y NO sirve para el extremo de estiaje.
  * El volumen no se mide: el portal lo calcula del nivel con una tabla. Si esa
    tabla esta desactualizada por sedimentacion, todos los dV salen inflados en
    la misma proporcion. No hay batimetria para verificarlo.

USO
---
    python limpiar_portal.py Zaza_sondeo.csv
    python limpiar_portal.py Zaza_sondeo.csv --carpeta salida
    python limpiar_portal.py *_portal_crudo.csv          # varios de una vez

SALIDAS (prefijo = nombre del embalse)
--------------------------------------
    <E>_diario_limpio.csv    una fila por dia natural; es la serie del balance
    <E>_correcciones.csv     cada valor que se cambio, con su motivo
    <E>_alertas.csv          lo sospechoso que se dejo intacto, para revisar
    <E>_eventos.csv          los registros subdiarios de los dias de crecida
    <E>_mensual_viejo.csv    la epoca mensual, si la hay
    <E>_curva.csv            la curva nivel-volumen-area ajustada

Requiere pandas y numpy.
"""

import argparse
import csv
import glob
import os
import sys
import unicodedata

import numpy as np
import pandas as pd

# Campos que cambian dia a dia. El resto del CSV es ficha del embalse y se repite
# igual en cada fila (incluido porcientollenado, que es el llenado de HOY, no una serie).
COLS_SERIE = ["fecha", "hora", "nivel", "volumen", "entrega", "vertimiento",
              "desembalse", "llenadoretenible"]

NUM = ["nivel", "volumen", "entrega", "vertimiento", "desembalse", "llenadoretenible"]

# Margen por debajo del nivel muerto que se acepta como lectura valida, en
# fraccion de la altura util. Los embalses si bajan del NM en sequia fuerte.
MARGEN_NM = 0.15

# Tolerancia del par nivel/volumen contra la curva, en fraccion del volumen.
# El 90% de las observaciones cae EXACTAMENTE sobre la tabla, asi que un 1% ya
# es mucho margen.
TOL_CURVA = 0.01

# Cuanto puede alejarse de sus vecinos un valor ya corregido para aceptarlo.
TOL_CONTINUIDAD = 0.02
MARGEN_CRESTA = 0.5         # m bajo el vertimiento sostenido mas bajo: ahi empieza la cresta
MARGEN_FUERA = 1.0         # m fuera de la curva a partir de los cuales un nivel es sospechoso



def nombre_seguro(nombre):
    """Nombre de carpeta/archivo sin tildes ni caracteres especiales: Mayarí -> Mayari.

    Tiene que coincidir con el campo Embalse del shapefile (que va sin tildes),
    porque de ahi salen las carpetas de GEOGLOWS y del balance.
    """
    s = unicodedata.normalize("NFKD", str(nombre)).encode("ascii", "ignore").decode("ascii")
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in s).strip()

def un_solo_digito(a, b, dec=2):
    """True si dos numeros se escriben igual salvo en UN digito.

    Es la firma del error de tecleo, y es una prueba INDEPENDIENTE de los
    vecinos: sirve cuando dos dias seguidos estan corrompidos y cada uno
    envenena la referencia del otro.
    """
    fa, fb = ("%%.%df" % dec) % a, ("%%.%df" % dec) % b
    if len(fa) != len(fb):
        return False
    return sum(x != y for x, y in zip(fa, fb)) == 1

# Multiplos de la entrega garantizada diaria a partir de los cuales se marca
# (FACTOR_ALERTA) y a partir de los cuales se corrige la coma (FACTOR_CORRIGE).
# El hueco entre los dos es deliberado: ahi viven los casos que no sabemos
# resolver (los x10 dudosos, el bloque de 2019) y se dejan como estan.
# Cuantas veces puede la entrega declarada superar la perdida real de volumen,
# sostenida en una quincena, antes de considerarla imposible. En los embalses
# sanos la razon no pasa de 3.4 ni en su peor dia.
FACTOR_RAZON = 4.0

# Magnitud absurda para un error aislado de tecleo, en multiplos del p75 propio.
FACTOR_ABSURDO = 20.0

# Hasta cuanto puede valer un valor ya corregido, en multiplos del p75 propio.
FACTOR_NORMAL = 3.0

# Cuantas veces el cambio diario tipico (p99) tiene que saltar un valor respecto
# de sus DOS vecinos para considerarlo un registro con la fecha corrompida.
FACTOR_SALTO = 4.0

# Tope de seguridad para el relleno, en dias. El criterio que de verdad manda es
# fisico (ver `rellenar_huecos`); esto solo evita inventar semanas enteras aunque
# el nivel casi no se haya movido.
MAX_HUECO = 15

# Ventana de revision: los ultimos N dias se recalculan en cada corrida y lo
# anterior se da por firme. La serie se calcula SIEMPRE entera (cuesta segundos),
# asi que los dias del borde nunca se juzgan a medias; la ventana solo decide
# que se vuelve a escribir. Asi lo viejo no se mueve, y aun asi el script puede
# avisarte si se habria movido.
VENTANA_REVISION = 30



# --------------------------------------------------------------- rutas del proyecto

def carpeta_proyecto():
    """Raiz del proyecto = la carpeta que contiene a Scripts/."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(aqui).lower() == "scripts":
        return os.path.dirname(aqui)
    return aqui


def carpeta_datos(*partes, crear=True):
    ruta = os.path.join(carpeta_proyecto(), *partes)
    if crear:
        os.makedirs(ruta, exist_ok=True)
    return ruta


CRUDO = ("Operative_Forecast", "Raw_Data")
LIMPIO = ("Operative_Forecast", "Clean_Data")


# ------------------------------------------------------- correcciones a mano

def aplicar_correcciones_manuales(S, ruta, correcciones):
    """Aplica las correcciones que escribiste tu, y que mandan sobre todo lo demas.

    El limpiador REGENERA la serie limpia en cada corrida, asi que editar a mano
    el archivo de salida no sirve: la proxima pasada lo sobrescribe. Por eso las
    decisiones manuales van en su propio archivo, que este script LEE y NUNCA
    escribe:

        Clean_Data/<Embalse>_correcciones_manuales.csv

    Formato (una fila por valor; el motivo es para que dentro de un ano se sepa
    por que se hizo):

        fecha,campo,valor,motivo
        2022-09-29,nivel,33.94,el Excel y los vecinos coinciden; la curva no
        2019-05-07,volumen,,dato dudoso de la fuente: se deja vacio

    Dejar `valor` en blanco borra el dato de ese dia. Se aplican al final, sobre
    todo lo que hayan hecho las reglas automaticas.
    """
    if not os.path.exists(ruta):
        return S

    # Se lee a mano y no con read_csv porque este archivo lo escribe una persona:
    # el motivo va a traer comas sin comillas, y eso tumba el lector estricto.
    # Se parte en los tres primeros separadores y el resto es el motivo.
    filas = []
    try:
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            lector = csv.reader(f)
            cab = next(lector, None)
            if not cab or [c.strip().lower() for c in cab[:2]] != ["fecha", "campo"]:
                print("  ! %s no empieza con las columnas fecha,campo,valor,motivo"
                      % os.path.basename(ruta))
                return S
            for cols in lector:
                if not cols or not cols[0].strip() or cols[0].lstrip().startswith("#"):
                    continue
                cols = (cols + ["", "", ""])[:max(4, len(cols))]
                filas.append({"fecha": cols[0].strip(), "campo": cols[1].strip(),
                              "valor": cols[2].strip() if len(cols) > 2 else "",
                              "motivo": ", ".join(c.strip() for c in cols[3:]).strip(", ")})
    except Exception as e:
        print("  ! no se pudo leer %s: %s" % (os.path.basename(ruta), e))
        return S
    if not filas:
        return S
    M = pd.DataFrame(filas)
    M["fecha"] = pd.to_datetime(M["fecha"], errors="coerce")
    n = 0
    for r in M.dropna(subset=["fecha"]).itertuples():
        if r.fecha not in S.index:
            print("  ! correccion manual para %s: esa fecha no esta en la serie"
                  % r.fecha.date())
            continue
        if r.campo not in S.columns:
            print("  ! correccion manual: no existe el campo '%s'" % r.campo)
            continue
        antes = S.loc[r.fecha, r.campo]
        nuevo = getattr(r, "valor", "")
        if nuevo is None or str(nuevo).strip() == "":
            nuevo = np.nan
        else:
            try:
                nuevo = float(nuevo)          # los campos de la serie son numericos
            except ValueError:
                pass
        S.loc[r.fecha, r.campo] = nuevo
        correcciones.append(dict(fecha=r.fecha, campo=r.campo, original=antes,
                                 corregido=nuevo, regla="correccion manual",
                                 motivo=getattr(r, "motivo", "") or "sin motivo anotado"))
        n += 1
    if n:
        print("  correcciones manuales aplicadas: %d  (de %s)"
              % (n, os.path.basename(ruta)))
    return S


# --------------------------------------------------------------------- utilidades

def _num(df, cols=NUM):
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _hora_h(serie):
    """Hora del dia en horas decimales. Acepta '8:00:00' y '08:00:00'."""
    t = pd.to_datetime(serie.astype(str).str.strip(), format="mixed", errors="coerce")
    return t.dt.hour + t.dt.minute / 60.0 + t.dt.second / 3600.0


def _es_0800(serie):
    h = _hora_h(serie)
    return (h - 8.0).abs() < 1e-9


def rango_nivel(ficha, crudo):
    """Rango de nivel fisicamente posible para ESTE embalse, de su propia ficha.

    No se puede usar un rango fijo: Hanabanilla opera entre 340 y 368 m sobre el
    mar y Cauto El Paso entre 19 y 27. Un filtro universal o deja pasar la basura
    o se lleva el embalse entero.

    El piso es el nivel muerto con un margen (en sequia se baja de el) y el techo
    es la cota de corona, por encima de la cual el agua ya no esta en el vaso.
    """
    nm, corona = ficha.get("nm", np.nan), ficha.get("cotacorona", np.nan)
    if np.isfinite(nm) and np.isfinite(corona) and corona > nm:
        return nm - MARGEN_NM * (corona - nm), corona
    v = crudo.nivel.dropna()
    if not len(v):
        return -np.inf, np.inf
    q1, q3 = v.quantile(.25), v.quantile(.75)      # sin ficha, rango robusto
    return q1 - 6 * (q3 - q1), q3 + 6 * (q3 - q1)


# ------------------------------------------------------------- 1. curva del vaso

def ajustar_curva(crudo, lo, hi, area_max_km2=None):
    """Curva nivel -> volumen y area del espejo, ajustada de forma robusta.

    El portal calcula el volumen del nivel con una tabla lineal por tramos, asi
    que el modelo correcto es interpolacion lineal, no un polinomio. Lo unico
    que hay que hacer es sacar los puntos corrompidos antes de interpolar.

    Dos filtros:
      1. para cada nivel se toma la MODA del volumen, no la media ni la mediana.
         En una tabla de consulta el valor correcto es el que mas se repite; la
         mediana se contamina cuando el nivel se vio pocas veces y una de ellas
         trae un typo.
      2. solo entran los niveles observados dos o mas veces. Un nivel visto una
         sola vez puede ser el typo mismo, y no hay con que contrastarlo.
      3. recorrido monotono con tope de area: el volumen no puede bajar cuando
         sube el nivel, ni crecer mas rapido que el area maxima de la ficha.
    """
    ok = crudo[crudo.nivel.between(lo, hi) & (crudo.volumen > 0)]
    if len(ok) < 100:
        return None

    g = ok.groupby("nivel")["volumen"].agg(
        n="count", v=lambda x: x.mode().iloc[0]).sort_index()
    firme = g[g.n >= 2]
    if len(firme) < 20:                      # serie corta: hay que aceptar los unicos
        firme = g
    h, V = firme.index.values.astype(float), firme.v.values.astype(float)

    # 3. recorrido monotono con tope de area
    amax = (area_max_km2 * 1.5) if area_max_km2 and np.isfinite(area_max_km2) else np.inf
    keep = [0]
    for i in range(1, len(h)):
        j = keep[-1]
        dh = h[i] - h[j]
        if dh <= 0:
            continue
        pend = (V[i] - V[j]) / dh
        if 0 < pend <= amax:
            keep.append(i)
    h, V = h[keep], V[keep]
    if len(h) < 20:
        return None

    area = np.gradient(V, h)              # hm3/m == km2
    return pd.DataFrame({"nivel": h, "volumen": V, "area_km2": area})


def vol_de_nivel(curva, niv):
    return np.interp(niv, curva.nivel.values, curva.volumen.values,
                     left=np.nan, right=np.nan)


def nivel_de_vol(curva, vol):
    return np.interp(vol, curva.volumen.values, curva.nivel.values,
                     left=np.nan, right=np.nan)


def area_de_nivel(curva, niv):
    # El area si se extrapola plana fuera del rango observado: perder el dia
    # entero del balance por estar un centimetro arriba de la tabla seria peor.
    return np.interp(niv, curva.nivel.values, curva.area_km2.values)


# ------------------------------------------------- 2-4. epocas, eventos, seleccion

def separar_epocas(crudo):
    """Devuelve (diario, mensual). La frontera se decide por cobertura, no a mano.

    Un ano con menos de la mitad de los dias cubiertos no es una serie diaria
    incompleta: es otra cosa, y mezclarla con la diaria solo ensucia el balance.
    """
    por_ano = crudo.groupby(crudo.fecha.dt.year)["fecha"].nunique()
    buenos = por_ano[por_ano > 182].index
    if len(buenos) == 0:
        return crudo, crudo.iloc[0:0]
    corte = int(min(buenos))
    return crudo[crudo.fecha.dt.year >= corte], crudo[crudo.fecha.dt.year < corte]


def elegir_del_dia(grupo):
    """Una fila por fecha: la de las 08:00 COMPLETA, o la unica que exista.

    No se mezclan campos entre filas. Mezclarlos parece razonable cuando la fila
    de las 08:00 trae un campo en cero, pero se comprobo contra el Excel que en
    esos casos el cero suele ser correcto, y que las filas de otras horas son las
    que cargan los valores x1000. La fila entera, o nada.
    """
    g8 = grupo[_es_0800(grupo.hora)]
    if len(g8):
        return g8.iloc[0], "0800"
    return grupo.iloc[0], "unica"


def construir_diario(diario_crudo):
    """Una fila por fecha, vectorizado.

    Antes esto era un bucle por fecha que volvia a parsear la hora en cada
    vuelta: 4894 parseos para Zaza, 14 segundos. Parseando una sola vez para
    todo el marco y ordenando, baja a menos de un segundo.

    El orden es (fecha, no-es-0800, hora): dentro de cada dia primero las filas
    de las 08:00 y luego el resto por hora ascendente, asi que quedarse con la
    primera de cada fecha es exactamente la regla acordada.
    """
    d = diario_crudo.copy()
    h = _hora_h(d.hora)
    d["_h"] = h.fillna(99.0)
    d["_no0800"] = (h - 8.0).abs() >= 1e-9       # False (0) para las de las 08:00
    n = d.groupby("fecha")["fecha"].transform("size")

    d = d.sort_values(["fecha", "_no0800", "_h"], kind="mergesort")
    S = d.drop_duplicates("fecha", keep="first").copy()
    S["regla_fila"] = np.where(S["_no0800"].values, "unica", "0800")
    S["n_registros_dia"] = n.loc[S.index].values
    S = S.drop(columns=["_h", "_no0800"]).set_index("fecha").sort_index()
    return S[~S.index.duplicated()]


def rejilla_calendario(S):
    """Una fila por dia natural, con NaN explicito en los dias sin dato.

    Sin esto el dV se calcula a traves de los huecos como si fueran un dia, que
    es precisamente el error que infla el caudal de entrada despues de un corte.
    """
    idx = pd.date_range(S.index.min(), S.index.max(), freq="D")
    S = S.reindex(idx)
    S.index.name = "fecha"
    S["relleno"] = ""
    return S


def medir_intervalos(S):
    """Horas reales transcurridas desde la lectura anterior que de verdad existe.

    Se calcula al final, despues de descartar registros falsos y de rellenar,
    porque las dos cosas cambian cual es "la lectura anterior".
    """
    hh = _hora_h(S.hora).fillna(8.0)
    ts = S.index.to_series() + pd.to_timedelta(hh, unit="h")
    ts = ts.where(S.nivel.notna())
    previa = ts.shift().ffill()
    S["horas_desde_anterior"] = (ts - previa).dt.total_seconds() / 3600.0
    return S


# ------------------------------------------- 6b. registros con la fecha corrompida

def descartar_fechas_corrompidas(S, curva, correcciones):
    """Elimina lecturas validas en si mismas pero imposibles en esa fecha.

    Son el caso mas traicionero, porque TODAS las pruebas internas las aprueban:
    el par nivel/volumen cae exacto sobre la curva, la entrega es razonable, la
    hora existe. Lo unico que falla es la fecha. En Zaza aparecieron cuatro
    lecturas de noviembre de 2019 fechadas el 1 al 4 de enero de 2015.

    La firma es: el valor se despega de sus DOS vecinos, y los dos vecinos entre
    si son continuos. O sea, la serie quedaria lisa si ese dia no existiera.

    Se DESCARTA, no se reubica. Mover el registro a la fecha que uno crea
    correcta es adivinar; borrar un dato que sabemos falso es defendible. La
    fila original queda completa en el archivo de correcciones, y el dia pasa a
    ser un hueco que el relleno puede cubrir si es corto.
    """
    n = S.nivel
    tipico = float(n.diff().abs().quantile(0.99))
    if not np.isfinite(tipico) or tipico <= 0:
        return S

    v_esp = vol_de_nivel(curva, n.values)
    en_curva = np.abs(S.volumen.values - v_esp) / np.maximum(v_esp, 1e-9) < TOL_CURVA

    prev, sig = n.shift(), n.shift(-1)
    salta_atras = (n - prev).abs() > FACTOR_SALTO * tipico
    salta_adel = (n - sig).abs() > FACTOR_SALTO * tipico
    vecinos_lisos = (sig - prev).abs() < 2 * tipico

    malo = en_curva & salta_atras & salta_adel & vecinos_lisos & n.notna()
    for f in S.index[malo.fillna(False)]:
        fila = S.loc[f]
        correcciones.append(dict(
            fecha=f, campo="registro completo",
            original="nivel %.2f / volumen %.3f / entrega %s"
                     % (fila.nivel, fila.volumen, fila.entrega),
            corregido="descartado", regla="fecha corrompida",
            motivo="el par cae en la curva pero es incompatible con los dos dias "
                   "vecinos (%.2f y %.2f), que entre si son continuos"
                   % (prev[f], sig[f])))
        for c in NUM + ["hora"]:
            if c in S.columns:
                S.loc[f, c] = np.nan
    return S


# -------------------------------------------------------------- 6c. relleno de huecos

def rellenar_huecos(S, curva, max_dias=MAX_HUECO, alertas=None):
    """Rellena los huecos que el propio embalse demuestra que son rellenables.

    El criterio no es un numero fijo de dias sino FISICO: se rellena mientras el
    nivel, a traves del hueco, no se haya movido mas rapido de lo que ese embalse
    se mueve en un dia corriente. Cada vaso trae su propio ritmo (el p99 de su
    cambio diario), asi que uno grande y lento admite huecos largos y uno chico y
    nervioso admite pocos.

    SIEMPRE se rellena: un dia sin dato no ayuda a nadie, y una recta entre los
    vecinos es la mejor estimacion disponible. Cuando el nivel se movio a traves
    del hueco mas rapido de lo que ese embalse se mueve en un dia corriente
    (el p99 de su cambio diario), se rellena IGUAL pero se deja una alerta: la
    recta puede estar escondiendo un evento que subio y volvio a bajar dentro.
    Lo unico que se respeta es el tope de dias: un hueco muy largo no se inventa.

    El nivel se interpola linealmente y el volumen se DERIVA de la curva, para
    que el par quede consistente por construccion. La entrega, el vertimiento y
    el desembalse se arrastran del ultimo valor conocido.

    Los dias rellenados quedan marcados: `interpolado` hasta 3 dias y
    `interpolado_largo` por encima, para poder excluir los largos del ajuste de
    la correccion de sesgo sin perderlos de la serie.
    """
    falta = S.nivel.isna()
    if not falta.any():
        return S

    tasa = float(S.nivel.diff().abs().quantile(0.99))
    if not np.isfinite(tasa) or tasa <= 0:
        tasa = np.inf

    grupo = (falta != falta.shift()).cumsum()
    for _, idx in S.groupby(grupo).groups.items():
        if not falta[idx[0]]:
            continue
        i0, i1 = S.index.get_loc(idx[0]), S.index.get_loc(idx[-1])
        if i0 == 0 or i1 == len(S) - 1:
            continue                                   # hueco en un extremo
        if pd.isna(S.nivel.iloc[i0 - 1]) or pd.isna(S.nivel.iloc[i1 + 1]):
            continue
        n0, n1 = S.nivel.iloc[i0 - 1], S.nivel.iloc[i1 + 1]
        n = len(idx)
        ritmo = abs(n1 - n0) / (n + 1)

        if n > max_dias:
            if alertas is not None:
                alertas.append(dict(
                    fecha=idx[0], campo="hueco", valor="%d dias" % n,
                    motivo="%d dias, por encima del tope de %d: no se rellena"
                           % (n, max_dias)))
            continue
        if ritmo > tasa and alertas is not None:
            alertas.append(dict(
                fecha=idx[0], campo="hueco", valor="%d dias" % n,
                motivo=("%d dias interpolados con el nivel moviendose %.3f m/dia contra "
                        "un ritmo normal de %.3f: la recta puede esconder un evento"
                        % (n, ritmo, tasa))))

        paso = (n1 - n0) / (n + 1)
        etiqueta = "interpolado" if n <= 3 else "interpolado_largo"
        for k, f in enumerate(idx, start=1):
            niv = round(n0 + paso * k, 2)
            S.loc[f, "nivel"] = niv
            S.loc[f, "volumen"] = round(float(vol_de_nivel(curva, niv)), 3)
            S.loc[f, "hora"] = S.hora.iloc[i0 - 1]
            for c in ("entrega", "vertimiento", "desembalse", "llenadoretenible"):
                if c in S.columns:
                    S.loc[f, c] = S[c].iloc[i0 - 1]
            S.loc[f, "relleno"] = etiqueta
    return S


# ------------------------------------------------------- 6. digitacion con la curva

def corregir_digitacion(S, curva, correcciones, alertas):
    """Repara el par nivel/volumen cuando contradice la curva del vaso.

    Si el par no cae sobre la curva, uno de los dos numeros esta mal. Para saber
    cual, se mira la continuidad con los dias vecinos: el que sigue la tendencia
    es el bueno, y el otro se recalcula con la curva. Si ninguno de los dos es
    claramente continuo, no se toca nada y se levanta una alerta.
    """
    v_esp = vol_de_nivel(curva, S.nivel.values)
    desvio = np.abs(S.volumen.values - v_esp) / np.maximum(v_esp, 1e-9)
    # Un nivel fuera del rango de la curva no tiene volumen esperado (NaN) y se
    # escapaba sin revisar: Mayari 2015-10-12, 94.93 m con el volumen de 84.93.
    # Se revisa cuando el nivel queda a mas de MARGEN_FUERA de la curva y su
    # volumen SI cae dentro: unos centimetros por encima de lo observado es una
    # crecida posible (Zaza 2018-05-28, 38.24 m), metros por encima con un
    # volumen ordinario no lo es.
    v, h = S.volumen.values, S.nivel.values
    lejos = (h > curva.nivel.max() + MARGEN_FUERA) | (h < curva.nivel.min() - MARGEN_FUERA)
    fuera = (np.isnan(v_esp) & lejos & np.isfinite(v)
             & (v >= curva.volumen.min()) & (v <= curva.volumen.max()))
    sospechoso = np.where((np.isfinite(desvio) & (desvio > TOL_CURVA)) | fuera)[0]

    niv_i = S.nivel.interpolate(limit_area="inside")
    vol_i = S.volumen.interpolate(limit_area="inside")
    # Referencia de continuidad: el promedio de los vecinos inmediatos.
    niv_ref = (S.nivel.shift() + S.nivel.shift(-1)) / 2
    vol_ref = (S.volumen.shift() + S.volumen.shift(-1)) / 2

    for i in sospechoso:
        f = S.index[i]
        nv, vl = S.nivel.iloc[i], S.volumen.iloc[i]
        nr, vr = niv_ref.iloc[i], vol_ref.iloc[i]
        if not (np.isfinite(nr) and np.isfinite(vr)):
            continue
        # cual de los dos se despega de sus vecinos
        salto_n = abs(nv - nr) / max(abs(nr), 1e-9)
        salto_v = abs(vl - vr) / max(abs(vr), 1e-9)

        cand = campo = None
        nota = ""
        # PRIMERO la firma de tecleo: si uno de los dos se arregla cambiando un
        # solo digito, ese es el que se tecleo mal. No depende de los vecinos,
        # asi que funciona aunque el dia de al lado tambien este corrompido.
        cand_n = float(nivel_de_vol(curva, vl)) if np.isfinite(vl) else np.nan
        cand_v = float(v_esp[i])
        dig_n = np.isfinite(cand_n) and un_solo_digito(nv, cand_n, 2)
        dig_v = np.isfinite(cand_v) and un_solo_digito(vl, cand_v, 3)

        if dig_n or dig_v:
            # Si las dos arreglan con un digito, decide cual de los dos campos
            # se despega mas de sus vecinos. Aqui no hace falta el margen de 4x
            # que se exige mas abajo: la firma de tecleo ya establecio que hay
            # un error, y solo queda repartir la culpa.
            elige_nivel = dig_n and (not dig_v or salto_n >= salto_v)
            if elige_nivel:
                cand, campo, orig, ref = cand_n, "nivel", nv, nr
                nota = "un solo digito de diferencia: %.2f en vez de %.2f" % (cand_n, nv)
            else:
                cand, campo, orig, ref = cand_v, "volumen", vl, vr
                nota = "un solo digito de diferencia: %.3f en vez de %.3f" % (cand_v, vl)
        elif salto_v > 4 * salto_n:                  # el volumen seria el raro
            cand, campo, orig, ref = cand_v, "volumen", vl, vr
            nota = "el nivel sigue a los vecinos y el volumen no"
        elif salto_n > 4 * salto_v:                  # el nivel seria el raro
            cand, campo, orig, ref = cand_n, "nivel", nv, nr
            nota = "el volumen sigue a los vecinos y el nivel no"

        # PRUEBA DE ACEPTACION: una correccion solo vale si devuelve el valor a la
        # vecindad. Si el numero "corregido" sigue lejos de los dias contiguos, lo
        # unico que sabemos es que el dato esta mal, no cual es el bueno.
        if cand is not None and np.isfinite(cand) and np.isfinite(ref):
            # El portal reporta el nivel al centimetro; una correccion no puede
            # tener mas resolucion que el dato original.
            cand = round(cand, 2 if campo == "nivel" else 3)
            nuevo_salto = abs(cand - ref) / max(abs(ref), 1e-9)
            viejo_salto = abs(orig - ref) / max(abs(ref), 1e-9)
            por_digito = nota.startswith("un solo digito")
            if por_digito or (nuevo_salto < TOL_CONTINUIDAD and nuevo_salto < viejo_salto / 3):
                correcciones.append(dict(fecha=f, campo=campo, original=orig,
                                         corregido=cand,
                                         regla="curva nivel-volumen", motivo=nota))
                S.iat[i, S.columns.get_loc(campo)] = cand
                continue
            alertas.append(dict(fecha=f, campo=campo, valor=orig,
                                motivo="fuera de la curva; el valor que daria la curva (%.3f) "
                                       "tampoco empalma con los vecinos" % cand))
            continue

        alertas.append(dict(fecha=f, campo="nivel/volumen", valor="%.2f / %.3f" % (nv, vl),
                            motivo="el par no cae en la curva y los vecinos no dicen cual falla"))
    return S


# --------------------------------------------------- 7. entrega: techo y balance

def corregir_entrega(S, ficha, correcciones, alertas):
    """Corrige las entregas con la coma de lugar. El juez es el balance, no la ficha.

    La entrega garantizada de la ficha NO sirve como escala: en Jimaguayu vale
    0.373 hm3/dia y la operacion normal de ese embalse tiene un p75 de 0.557, o
    sea que supera el "maximo" de la ficha todos los dias. Usarla como umbral
    dividia por diez 128 valores perfectamente validos.

    Lo que si discrimina es cuanto se aleja la entrega declarada de lo que el
    embalse esta perdiendo de verdad, sostenido en el tiempo:

        razon = entrega tipica de la quincena / caida de volumen tipica

    En los seis embalses sanos esa razon no pasa de 3.4 ni en su peor dia. El
    bloque de mayo-julio de 2019 en Zaza vive entre 4.4 y 7.4. La frontera es
    real y es fisica: declarar que se entrega cuatro veces mas agua de la que el
    vaso pierde, durante semanas, no puede ser cierto.

    Dos disparadores:
      * razon local por encima de FACTOR_RAZON (error sostenido, de escala)
      * magnitud absurda contra el propio embalse (error aislado de tecleo)

    Y la division se acepta solo si el resultado cae dentro de lo que ese embalse
    entrega normalmente Y vuelve la razon a lo creible.
    """
    pos = S.entrega[S.entrega > 0]
    if not len(pos):
        return S
    escala = float(pos.quantile(0.75))          # robusta: no la mueven los atipicos
    if not np.isfinite(escala) or escala <= 0:
        return S
    absurdo = FACTOR_ABSURDO * escala
    normal_max = FACTOR_NORMAL * escala

    eg = ficha.get("entregagarantizada", np.nan)
    techo = (eg / 365.0) if np.isfinite(eg) and eg > 0 else np.nan

    dV = S.volumen.diff()
    vert = S.vertimiento.fillna(0) * 0.0864
    caida = (-dV).where((dV < 0) & (vert < 1e-9))
    caida_loc = caida.rolling(15, center=True, min_periods=4).median()
    ent_loc = S.entrega.rolling(15, center=True, min_periods=4).median()
    razon = ent_loc / caida_loc
    # Referencia de continuidad: la entrega tipica de los dias vecinos, contando
    # solo los que ya estan en rango normal. Es lo que desempata entre 4.32 y
    # 0.432 cuando la fisica deja pasar las dos.
    ref_loc = S.entrega.where(S.entrega <= normal_max).rolling(
        15, center=True, min_periods=3).median()

    for i in range(len(S)):
        e = S.entrega.iloc[i]
        if not np.isfinite(e) or e <= 0:
            continue
        # GUARDA: nunca se toca un valor que ya esta dentro de lo que este
        # embalse entrega normalmente, diga lo que diga cualquier otra prueba.
        # Es lo que evitaba dividir por diez el 1.227 de Zaza o el 1.15 de
        # Jimaguayu, que son operacion corriente.
        if e <= normal_max:
            continue
        r = razon.iloc[i]
        sostenido = np.isfinite(r) and r > FACTOR_RAZON
        aislado = e > absurdo
        if not (sostenido or aislado):
            continue

        f = S.index[i]
        # Que potencia de diez. No la primera que "quepa", sino la que deja el
        # valor por debajo de lo que el embalse esta perdiendo de verdad: la
        # entrega sale de ese agua. Con 432 hm3, "la primera que cabe" daba 4.32
        # y la fisica dice 0.432.
        cl = caida_loc.iloc[i]
        tope = min(normal_max, cl) if (np.isfinite(cl) and cl > 0) else normal_max
        posibles = [e / 10 ** k for k in (1, 2, 3, 4) if e / 10 ** k <= tope]
        elegido = None
        if posibles:
            rl = ref_loc.iloc[i]
            if np.isfinite(rl):
                # entre las que la fisica permite, la que empalma con los vecinos
                elegido = min(posibles, key=lambda v: abs(v - rl))
            else:
                elegido = posibles[0]

        if elegido is None:
            alertas.append(dict(fecha=f, campo="entrega", valor=e,
                                motivo=("entrega sostenidamente %.1f veces la perdida real "
                                        "del embalse; ninguna potencia de diez la deja "
                                        "coherente" % r) if sostenido else
                                       ("%.0f veces la entrega habitual de este embalse "
                                        "(p75 %.3f); ninguna potencia de diez la deja "
                                        "coherente" % (e / escala, escala))))
            continue

        if sostenido:
            motivo = ("la quincena declara %.1f veces lo que el embalse pierde (%.3f hm3/dia); "
                      "/%d queda en %.3f" % (r, cl, int(round(e / elegido)), elegido))
        else:
            motivo = ("%.0f veces la entrega habitual (p75 %.3f); /%d queda en %.3f"
                      % (e / escala, escala, int(round(e / elegido)), elegido))
        if np.isfinite(techo):
            motivo += " (entrega garantizada diaria de la ficha: %.3f)" % techo
        correcciones.append(dict(fecha=f, campo="entrega", original=e,
                                 corregido=round(elegido, 4),
                                 regla="potencia de diez", motivo=motivo))
        S.iat[i, S.columns.get_loc("entrega")] = round(elegido, 4)
    return S


# -------------------------------------------------------------------- 8. el balance

def calcular_qin(S, curva):
    """Qin = dV + entrega + vertimiento + desembalse, dividido por el intervalo REAL.

    Dividir por 86400 a ciegas sobreestima el caudal cuando el dia anterior se
    leyo a otra hora. La columna horas_desde_anterior arregla eso.

    El recorte en cero se aplica porque un caudal de entrada negativo no existe,
    pero deja bandera: esos dias son la medicion de la salida que falta, y
    borrarlos sin rastro es perder la unica pista que hay sobre ella.
    """
    S["area_km2"] = area_de_nivel(curva, S.nivel.values)

    # El dV se mide contra la ULTIMA LECTURA REAL, no contra el renglon anterior
    # del calendario. Si no, cada hueco de un dia se lleva por delante tambien el
    # dia siguiente, y con 140 huecos eso son 140 dias de balance tirados.
    prev_vol = S.volumen.ffill().shift()
    S["dV_hm3"] = S.volumen - prev_vol

    horas = S.horas_desde_anterior.where(S.horas_desde_anterior.between(6, 72))
    dias = horas / 24.0
    # entrega y vertimiento son tasas diarias: sobre un intervalo de 48 h salieron
    # dos dias de entrega, no uno.
    S["Qin_hm3"] = (S.dV_hm3
                    + (S.entrega.fillna(0)
                       + S.vertimiento.fillna(0) * 0.0864
                       + S.desembalse.fillna(0)) * dias)
    S["Qin_cms"] = S.Qin_hm3 * 1e6 / (horas * 3600)
    S["Qin_hm3"] = S.Qin_hm3 / dias          # se reporta como tasa diaria
    S["Qin_recortado"] = (S.Qin_cms < 0).fillna(False)
    S.loc[S.Qin_recortado, "Qin_cms"] = 0.0
    S.loc[S.Qin_recortado, "Qin_hm3"] = 0.0
    return S


def agrupar_alertas(alertas):
    """Colapsa dias consecutivos con la misma alerta en un solo renglon.

    Un valor erroneo que se repite semanas seguidas no son cuarenta problemas
    distintos: es uno solo, persistente, y verlo como un rango dice mucho mas
    que verlo cuarenta veces. Los errores sostenidos suelen venir de la fuente,
    no de quien teclea.
    """
    if not alertas:
        return pd.DataFrame(columns=["desde", "hasta", "dias", "campo", "valor", "motivo"])
    A = pd.DataFrame(alertas).sort_values(["campo", "fecha"]).reset_index(drop=True)
    clave = A.campo.astype(str) + "|" + A.valor.astype(str) + "|" + A.motivo.astype(str)
    salto = (clave != clave.shift()) | (A.fecha.diff().dt.days.fillna(99) > 1)
    A["g"] = salto.cumsum()
    out = A.groupby("g").agg(desde=("fecha", "min"), hasta=("fecha", "max"),
                             dias=("fecha", "size"), campo=("campo", "first"),
                             valor=("valor", "first"), motivo=("motivo", "first"))
    return out.sort_values("desde").reset_index(drop=True)

# --------------------------------------------- 7b. vertimiento: cresta y x1000

def umbral_vertimiento(S):
    """Nivel por debajo del cual el embalse no vierte, sacado de sus propios datos.

    Se toman las rachas de 3 o mas dias seguidos vertiendo (un vertimiento
    sostenido es real; uno suelto puede ser un error) y su nivel minimo, menos
    MARGEN_CRESTA. Antes se apartan los dias en que el vertimiento es igual a la
    entrega del mismo dia: son la entrega copiada en la columna equivocada y, si
    entraran, bajarian el umbral (Zaza 2015-01-17 a 25, a 33.5 m).
    Devuelve NaN si el embalse no tiene ninguna racha sostenida.
    """
    copia = _vert_copia_entrega(S)
    vierte = (S.vertimiento > 0) & ~copia
    racha = (vierte != vierte.shift()).cumsum()
    largo = vierte.groupby(racha).transform("sum")
    sostenido = vierte & (largo >= 3)
    if not sostenido.any():
        return np.nan
    return float(S.nivel[sostenido].min()) - MARGEN_CRESTA


def _vert_copia_entrega(S):
    e, v = S.entrega, S.vertimiento
    return (v > 0) & (e > 0) & ((v - e).abs() <= 0.005 * e)


def corregir_vertimiento(S, correcciones, alertas):
    """Revisa el vertimiento, que entra entero al Qin observado.

    El vertimiento no se usa en el pronostico (ahi se calcula), pero si en el
    Qin historico: Qin = dV + entrega + vertimiento + desembalse. Un error aqui
    es un pico falso (o un pico perdido) de caudal de entrada, justo en la cola
    alta que mas pesa en la correccion de sesgo. Dos reglas:

      1. por debajo de la cresta no se vierte. Un vertimiento con el nivel bajo
         el umbral de umbral_vertimiento() se pone en cero. Ejemplos: Zaza
         2019-06-05, 400.366 m3/s a 29.22 m (el valor exacto del 2018-06-04);
         Zaza 2015-01, la entrega copiada a 33.5 m.
      2. coma de lugar. Si el valor es mil veces menor (o mayor) que el de sus
         vecinos con vertimiento y por mil (o entre mil) cae junto a ellos, se
         corrige. Ejemplo: Mayari 2025-10-30, 0.516 m3/s a 91.20 m entre 479.7
         y 391.8 (el huracan).
    """
    if "vertimiento" not in S or not (S.vertimiento > 0).any():
        return S
    umbral = umbral_vertimiento(S)
    copia = _vert_copia_entrega(S)

    # 1. por debajo de la cresta
    if np.isfinite(umbral):
        bajo = (S.vertimiento > 0) & (S.nivel < umbral)
        for f in S.index[bajo]:
            v = float(S.at[f, "vertimiento"])
            motivo = ("vierte %.3f m3/s con el nivel en %.2f m, por debajo de la cresta "
                      "(umbral %.2f m)" % (v, S.at[f, "nivel"], umbral))
            if copia.at[f]:
                motivo += "; es la entrega del dia copiada"
            correcciones.append(dict(fecha=f, campo="vertimiento", original=v, corregido=0.0,
                                     regla="vertimiento bajo la cresta", motivo=motivo))
            S.at[f, "vertimiento"] = 0.0

    # 2. coma de lugar: contra los vecinos con vertimiento (hasta 3 dias)
    v = S.vertimiento.where(S.vertimiento > 0)
    ant = v.shift(1).combine_first(v.shift(2)).combine_first(v.shift(3))
    sig = v.shift(-1).combine_first(v.shift(-2)).combine_first(v.shift(-3))
    ref = pd.concat([ant, sig], axis=1).mean(axis=1, skipna=False)
    for f in S.index[v.notna() & ref.notna()]:
        x, r = float(v.at[f]), float(ref.at[f])
        for fac in (1000.0, 1.0 / 1000.0):
            if abs(np.log(x / r)) > np.log(300) and abs(np.log(x * fac / r)) < np.log(3):
                correcciones.append(dict(
                    fecha=f, campo="vertimiento", original=x, corregido=round(x * fac, 3),
                    regla="vertimiento coma de lugar",
                    motivo="%.3f m3/s entre vecinos de %.1f; por %s queda %.1f"
                           % (x, r, "mil" if fac > 1 else "1/1000", x * fac)))
                S.at[f, "vertimiento"] = round(x * fac, 3)
                break
    return S


def marcar_calidad(S, correcciones):
    fechas_corr = {c["fecha"] for c in correcciones}
    cal = pd.Series("ok", index=S.index, dtype=object)
    cal[S.nivel.isna()] = "sin_dato"
    rell = S.relleno.fillna("").astype(str)
    cal[rell != ""] = rell[rell != ""]
    cal[S.index.isin(fechas_corr)] = "corregido"
    cal[(S.regla_fila == "unica") & S.nivel.notna() & (cal == "ok")] = "sin_0800"
    cal[S.horas_desde_anterior.notna() &
        ~S.horas_desde_anterior.between(22, 26) & (cal == "ok")] = "intervalo_irregular"
    S["calidad"] = cal
    return S



# ------------------------------------------------- congelar lo ya establecido

def _leer_previo(ruta, col_fecha="fecha"):
    if not os.path.exists(ruta):
        return None
    try:
        D = pd.read_csv(ruta, encoding="utf-8-sig")
        if col_fecha not in D.columns:
            return None
        D[col_fecha] = pd.to_datetime(D[col_fecha], errors="coerce")
        return D.dropna(subset=[col_fecha])
    except Exception:
        return None


def empalmar(S, previo, corte, cols):
    """Conserva lo anterior al corte tal como estaba y recalcula de ahi en adelante."""
    viejo = previo[previo.fecha < corte].set_index("fecha")
    nuevo = S[S.index >= corte]
    falta = [c for c in cols if c not in viejo.columns]
    for c in falta:
        viejo[c] = np.nan
    return pd.concat([viejo[cols], nuevo[cols]]).sort_index()


def divergencias(S, previo, corte, campos=("nivel", "volumen", "entrega")):
    """Dias congelados en los que el calculo de hoy habria dado otra cosa.

    No se cambian: se cuentan. Es la unica forma de saber si congelar la
    historia esta escondiendo algo.
    """
    v = previo[previo.fecha < corte].set_index("fecha")
    com = v.index.intersection(S.index)
    if not len(com):
        return 0, None
    # Se compara con el MISMO formato con que se escribe el CSV. Comparar
    # numeros no sirve: el archivo guarda 4 decimales, asi que una tolerancia
    # fina delata el redondeo y no el dato. Y redondear con pandas tampoco,
    # porque usa redondeo al par mientras que "%.4f" redondea hacia arriba, y
    # los empates exactos salen marcados como si hubieran cambiado.
    fmt = lambda x: "" if pd.isna(x) else "%.4f" % x
    dif = pd.Series(False, index=com)
    for c in campos:
        if c in v.columns and c in S.columns:
            a = pd.to_numeric(v.loc[com, c], errors="coerce").map(fmt)
            b = pd.to_numeric(S.loc[com, c], errors="coerce").map(fmt)
            dif |= (a != b)
    return int(dif.sum()), (com[dif] if dif.any() else None)


# ------------------------------------------------------------------------ proceso

def procesar(ruta, carpeta, dias=VENTANA_REVISION, rehacer=False):
    crudo = pd.read_csv(ruta, encoding="utf-8-sig")
    crudo["fecha"] = pd.to_datetime(crudo["fecha"], errors="coerce")
    crudo = _num(crudo).dropna(subset=["fecha"])

    nombre = str(crudo["embalse"].iloc[0]).strip() if "embalse" in crudo else "embalse"
    seguro = nombre_seguro(nombre)
    ficha = {c: crudo[c].iloc[0] for c in crudo.columns if crudo[c].nunique(dropna=False) == 1}

    print("=" * 68)
    print("%s  (%d filas crudas, %s a %s)"
          % (nombre, len(crudo), crudo.fecha.min().date(), crudo.fecha.max().date()))

    # El area al NAM de la ficha viene en hectareas; sirve de tope para el ajuste.
    a_nam = ficha.get("areanam", np.nan)
    tope = (a_nam / 100.0) if np.isfinite(a_nam) and a_nam > 0 else None

    lo, hi = rango_nivel(ficha, crudo)
    # La curva se ajusta SOLO con la epoca diaria. Las lecturas viejas y
    # esporadicas (Mayari 2010-2012) mezclan tablas de volumen distintas y la
    # contaminan justo en los niveles donde no hay otros datos.
    curva = ajustar_curva(separar_epocas(crudo)[0], lo, hi, area_max_km2=tope)
    if curva is None:
        print("  ! no hay puntos suficientes para la curva nivel-volumen; se omite")
        return
    print("  curva nivel-volumen: %d puntos, de %.2f a %.2f m | area de %.1f a %.1f km2"
          % (len(curva), curva.nivel.min(), curva.nivel.max(),
             curva.area_km2.min(), curva.area_km2.max()))
    for cota, ref, nom in [(ficha.get("nm"), ficha.get("areanm"), "NM"),
                           (ficha.get("nan"), ficha.get("areanan"), "NAN"),
                           (ficha.get("nam"), ficha.get("areanam"), "NAM")]:
        if cota and ref and np.isfinite(cota) and np.isfinite(ref):
            if not (curva.nivel.min() <= cota <= curva.nivel.max()):
                continue                      # fuera de lo observado: extrapolar no dice nada
            est = float(area_de_nivel(curva, cota))
            if np.isfinite(est):
                print("    cota %6.2f (%-3s): curva %6.1f km2 | ficha %6.1f km2"
                      % (cota, nom, est, ref / 100.0))

    fuera = (~crudo.nivel.between(lo, hi)) & crudo.nivel.notna()
    if fuera.any():
        print("  lecturas de nivel fuera del rango fisico [%.2f, %.2f]: %d (se descartan)"
              % (lo, hi, int(fuera.sum())))
        crudo = crudo[~fuera]

    diario_crudo, mensual = separar_epocas(crudo)
    if len(mensual):
        print("  epoca mensual apartada: %d filas, hasta %s"
              % (len(mensual), mensual.fecha.max().date()))
    print("  epoca diaria: %s en adelante" % diario_crudo.fecha.min().date())

    dup = diario_crudo.groupby("fecha").filter(lambda g: len(g) > 1)
    print("  registros subdiarios apartados: %d filas en %d fechas"
          % (len(dup), dup.fecha.nunique() if len(dup) else 0))

    S = construir_diario(diario_crudo)
    S = rejilla_calendario(S)

    ruta_manual = os.path.join(carpeta, seguro + "_correcciones_manuales.csv")
    correcciones, alertas = [], []
    # Las correcciones a mano se aplican DOS veces. Aqui, para que las reglas
    # automaticas trabajen ya sobre el dato bueno: si no, el relleno interpola
    # HACIA la lectura mala (Cauto El Paso 2013-04-25 contra el 21.16 del dia 26).
    # Y otra vez al final, para que sigan teniendo la ultima palabra.
    S = aplicar_correcciones_manuales(S, ruta_manual, [])
    S = corregir_digitacion(S, curva, correcciones, alertas)
    S = descartar_fechas_corrompidas(S, curva, correcciones)
    S = rellenar_huecos(S, curva, alertas=alertas)
    S = corregir_entrega(S, ficha, correcciones, alertas)
    S = corregir_vertimiento(S, correcciones, alertas)
    S = aplicar_correcciones_manuales(S, ruta_manual, correcciones)
    S = medir_intervalos(S)
    S = calcular_qin(S, curva)
    S = marcar_calidad(S, correcciones)

    salida = [c for c in COLS_SERIE if c in S.columns and c != "fecha"]
    salida += ["regla_fila", "n_registros_dia", "horas_desde_anterior",
               "area_km2", "dV_hm3", "Qin_hm3", "Qin_cms", "Qin_recortado",
               "relleno", "calidad"]

    # Una carpeta por embalse: Clean_Data/<Embalse>/<Embalse>_*.csv
    destino = os.path.join(carpeta, seguro)
    os.makedirs(destino, exist_ok=True)
    base = os.path.join(destino, seguro)

    # --- ventana de revision: lo anterior al corte se conserva como estaba ---
    previo = None if rehacer else _leer_previo(base + "_diario_limpio.csv")
    corte = None
    if previo is not None and len(previo):
        corte = previo.fecha.max() - pd.Timedelta(days=dias)
        n_div, cuales = divergencias(S, previo, corte)
        S_escribir = empalmar(S, previo, corte, salida)
        print("\n  ventana de revision: se recalculan los dias desde %s (%d dias)"
              % (corte.date(), dias))
        print("  dias congelados: %d" % (previo.fecha < corte).sum())
        if n_div:
            print("  ! AVISO: en %d dias congelados el calculo de hoy daria otro valor." % n_div)
            print("           no se tocaron. para rehacerlos: --rehacer")
            print("           primeros: %s"
                  % ", ".join(str(d.date()) for d in cuales[:6]))
        # el registro de auditoria tiene que coincidir con la serie que se escribe
        for nom, nuevas in (("_correcciones.csv", pd.DataFrame(correcciones)),
                            ("_alertas.csv", agrupar_alertas(alertas))):
            col = "fecha" if nom == "_correcciones.csv" else "desde"
            ant = _leer_previo(base + nom, col)
            if nuevas is not None and len(nuevas) and col in nuevas.columns:
                nuevas = nuevas[pd.to_datetime(nuevas[col]) >= corte]
            if ant is not None and len(ant):
                ant = ant[ant[col] < corte]
                nuevas = pd.concat([ant, nuevas], ignore_index=True).sort_values(col)
            nuevas.to_csv(base + nom, index=False, encoding="utf-8-sig", lineterminator="\n")
    else:
        S_escribir = S[salida]
        pd.DataFrame(correcciones).to_csv(base + "_correcciones.csv", index=False,
                                          encoding="utf-8-sig", lineterminator="\n")
        agrupar_alertas(alertas).to_csv(base + "_alertas.csv", index=False,
                                        encoding="utf-8-sig", lineterminator="\n")

    S_escribir.to_csv(base + "_diario_limpio.csv", encoding="utf-8-sig",
                      lineterminator="\n", float_format="%.4f")
    curva.to_csv(base + "_curva.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    if len(dup):
        dup.to_csv(base + "_eventos.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    if len(mensual):
        mensual.to_csv(base + "_mensual_viejo.csv", index=False,
                       encoding="utf-8-sig", lineterminator="\n")

    n = len(S)
    q = S.Qin_cms.dropna()
    print("\n  serie diaria ......... %d dias (%s a %s)"
          % (n, S.index.min().date(), S.index.max().date()))
    print("  dias sin dato ........ %d (%.1f%%)" % ((S.calidad == "sin_dato").sum(),
                                                    100 * (S.calidad == "sin_dato").mean()))
    print("  fila de las 08:00 .... %d (%.1f%%)" % ((S.regla_fila == "0800").sum(),
                                                    100 * (S.regla_fila == "0800").mean()))
    print("  valores corregidos ... %d" % len(correcciones))
    print("  alertas sin tocar .... %d" % len(alertas))
    if len(q):
        print("  Qin: mediana %.1f | p90 %.1f | p99 %.1f m3/s"
              % (q.median(), q.quantile(.9), q.quantile(.99)))
        print("  dias recortados a cero: %d (%.1f%%)  <- medida de la salida que falta"
              % (S.Qin_recortado.sum(), 100 * S.Qin_recortado.mean()))
    print("\n  -> %s_diario_limpio.csv" % seguro)


def main():
    p = argparse.ArgumentParser(
        description="Limpia una descarga cruda del portal SGIA y arma la serie diaria.")
    p.add_argument("archivos", nargs="*",
                   help="CSV crudo(s); por omision, todos los de Operative_Forecast/Raw_Data")
    p.add_argument("--carpeta", default=None,
                   help="carpeta de salida (def. Operative_Forecast/Clean_Data)")
    p.add_argument("--dias", type=int, default=VENTANA_REVISION,
                   help="ventana de revision en dias (def. %d)" % VENTANA_REVISION)
    p.add_argument("--rehacer", action="store_true",
                   help="recalcular TODA la serie, ignorando la ventana")
    a = p.parse_args()

    if a.archivos:
        rutas = []
        for patron in a.archivos:
            rutas.extend(sorted(glob.glob(patron)) or [patron])
    else:
        raiz = carpeta_datos(*CRUDO)
        rutas = sorted(glob.glob(os.path.join(raiz, "*", "*.csv"))) + \
                sorted(glob.glob(os.path.join(raiz, "*.csv")))
        if not rutas:
            sys.exit("No hay ningun CSV en %s" % raiz)
    if not rutas:
        sys.exit("No se encontro ningun archivo.")

    carpeta = a.carpeta or carpeta_datos(*LIMPIO)
    print("Escribiendo en: %s" % carpeta)

    for r in rutas:
        if not os.path.exists(r):
            print("! no existe: %s" % r)
            continue
        procesar(r, carpeta, dias=a.dias, rehacer=a.rehacer)


if __name__ == "__main__":
    main()

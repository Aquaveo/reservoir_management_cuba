# -*- coding: utf-8 -*-
"""
pronosticar.py
Pronostico de nivel y volumen de un embalse a 14 dias, con el mismo metodo de
los Colabs (Pronostico_Embalse_<E>.ipynb), y una pagina HTML para manejar las
salidas y descargar los resultados.

    python pronosticar.py --embalse Zaza
    python pronosticar.py --todos

Lo que hace, para cada embalse:

  1. Lee la serie limpia   Operative_Forecast/Clean_Data/<E>/<E>_diario_limpio.csv
  2. Curva nivel-volumen y ley del aliviadero, a partir de la serie limpia.
  3. Descarga de GEOGLOWS la retrospectiva, el pronostico vigente y los
     pronosticos emitidos en los ultimos ~30 dias (estos se guardan y no se
     vuelven a bajar).
  4. Corrige el sesgo (MFDC-QM, miembro por miembro) y ajusta las condiciones
     iniciales (AR(1) del error).
  5. Escribe la pagina   Operative_Forecast/Pronosticos/<E>/<E>_pronostico_<AAAAMMDD>.html

La pagina se abre con doble clic, sin internet. Ahi se eligen la version del
pronostico, se editan entrega, desembalse y vertimiento dia por dia (el balance
se recalcula en el momento) y se descarga el .zip con los resultados, igual
que en el Colab.

Las fichas de los embalses (tramos, nivel muerto, aliviadero, ...) estan en
embalses_pronostico.json, junto a este script.

Necesita:  pip install geoglows statsmodels pandas numpy plotly
"""

import os
import json
import time
import argparse
import warnings
import datetime as dt
import webbrowser
import pathlib

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

AQUI = os.path.dirname(os.path.abspath(__file__))
CMS_A_HM3 = 0.0864           # m3/s sostenidos un dia -> hm3
VERSIONES = [
    ("Raw", "(a) GEOGLOWS"),
    ("Bias_Correction", "(b) GEOGLOWS con sesgo corregido"),
    ("Bias_Correction+Assimilation", "(c) GEOGLOWS con sesgo corregido + ajuste de condiciones iniciales"),
]


# ============================================================ configuracion

def leer_config(ruta):
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["general"], cfg["embalses"]


# ============================================================ curva nivel-volumen

def ajustar_curva(df, lo, hi, area_max_km2=None):
    """Curva observada: moda del volumen por nivel, recorrido monotono con tope de area."""
    ok = df[df.nivel.between(lo, hi) & (df.volumen > 0)]
    g = ok.groupby("nivel")["volumen"].agg(
        n="count", v=lambda x: x.mode().iloc[0]).sort_index()
    firme = g[g.n >= 2]
    if len(firme) < 20:
        firme = g
    h, V = firme.index.values.astype(float), firme.v.values.astype(float)
    amax = area_max_km2 * 1.5 if area_max_km2 else np.inf
    keep = [0]
    for i in range(1, len(h)):
        j = keep[-1]
        dh = h[i] - h[j]
        if dh <= 0:
            continue
        if 0 < (V[i] - V[j]) / dh <= amax:
            keep.append(i)
    h, V = h[keep], V[keep]
    return pd.DataFrame({"nivel": h, "volumen": V, "area_km2": np.gradient(V, h)})


def extender_curva(curva, ficha, paso=0.01):
    """Prolonga la curva hasta el nivel muerto y hasta el NAM con los anclajes de la ficha."""
    curva = curva.sort_values("nivel").reset_index(drop=True)
    n0, v0 = curva.nivel.iloc[0], curva.volumen.iloc[0]
    n1, v1 = curva.nivel.iloc[-1], curva.volumen.iloc[-1]
    tramos = [curva[["nivel", "volumen"]]]
    nm, vnm = ficha["nm"], ficha["volumennm"]
    if nm < n0:
        g = np.arange(round(nm, 2), n0, paso)
        tramos.append(pd.DataFrame({"nivel": g, "volumen": np.interp(g, [nm, n0], [vnm, v0])}))
    nam, vnam = ficha["nam"], ficha["volumennam"]
    if nam > n1:
        g = np.arange(n1 + paso, nam + paso / 2, paso)
        tramos.append(pd.DataFrame({"nivel": g, "volumen": np.interp(g, [n1, nam], [v1, vnam])}))
    out = pd.concat(tramos).drop_duplicates("nivel").sort_values("nivel").reset_index(drop=True)
    out["area_km2"] = np.gradient(out.volumen.values, out.nivel.values)
    return out


# ============================================================ GEOGLOWS

def dia_balance(idx_utc, zona, hora):
    # se quita la zona ANTES de redondear (el floor sobre un indice con zona
    # revienta la noche del cambio de hora)
    local = pd.DatetimeIndex(idx_utc).tz_convert(zona).tz_localize(None)
    return (local + pd.Timedelta(hours=24 - hora)).floor("D")


def a_diario_balance(df, zona, hora, horas_minimas=12):
    """De paso de 1 o 3 horas (UTC) a la media del dia de balance (08:00 a 08:00 local)."""
    g = df.copy()
    g.index = pd.DatetimeIndex(g.index)
    if g.index.tz is None:
        g.index = g.index.tz_localize("UTC")
    completo = pd.date_range(g.index.min(), g.index.max(), freq="h")
    g = (g.reindex(g.index.union(completo))
           .interpolate(method="time", limit_area="inside")
           .reindex(completo))
    dia = dia_balance(g.index, zona, hora)
    horas = pd.Series(1, index=dia).groupby(level=0).sum()
    g.index = dia
    g.index.name = "fecha"
    out = g.groupby(level="fecha").mean()
    return out.loc[out.index.isin(horas[horas >= horas_minimas].index)]


def sumar_tramos(df):
    if isinstance(df.index, pd.MultiIndex) and "river_id" in (df.index.names or []):
        return df.groupby(level="time").sum(min_count=1)
    return df


class Geoglows:
    """Descargas de GEOGLOWS con copia local en <cache>.

    Los pronosticos ya emitidos no cambian: se guardan la primera vez y despues
    se leen del disco. La retrospectiva y el pronostico vigente se bajan en
    cada corrida; si la descarga falla se usa la ultima copia guardada.
    Con sin_descarga=True solo se usa lo guardado.
    """

    def __init__(self, tramos, zona, hora, cache, sin_descarga=False):
        self.tramos, self.zona, self.hora = tramos, zona, hora
        self.cache, self.sin_descarga = cache, sin_descarga
        os.makedirs(os.path.join(cache, "emisiones"), exist_ok=True)
        self._gg = None

    def _geoglows(self):
        if self._gg is None:
            import geoglows
            self._gg = geoglows
        return self._gg

    def _bajar_pronostico(self, fecha=None, intentos=3):
        for k in range(1, intentos + 1):
            try:
                gg = self._geoglows()
                d = (gg.data.forecast_ensembles(self.tramos, date=fecha) if fecha
                     else gg.data.forecast_ensembles(self.tramos))
                d = sumar_tramos(d)
                d = d[[c for c in d.columns if str(c).startswith("ensemble_")]]
                d[d < 0] = 0
                idx = pd.DatetimeIndex(d.index)
                emitido = (idx.tz_localize(None) if idx.tz is not None else idx).min().normalize()
                d = a_diario_balance(d, self.zona, self.hora)
                d = d.reset_index().rename(columns={"index": "fecha"})
                return d, emitido
            except Exception as e:
                if k == intentos:
                    print("  ! pronostico %s: fallo la descarga (%s)" % (fecha or "vigente", str(e)[:100]))
                    return None, None
                time.sleep(2)

    @staticmethod
    def _guardar(d, ruta):
        d.assign(fecha=pd.to_datetime(d.fecha).dt.strftime("%Y-%m-%d")).to_csv(ruta, index=False)

    @staticmethod
    def _leer(ruta):
        return pd.read_csv(ruta, parse_dates=["fecha"])

    def retrospectiva(self):
        ruta = os.path.join(self.cache, "retrospectiva.csv")
        if not self.sin_descarga:
            try:
                r = sumar_tramos(self._geoglows().data.retro_daily(self.tramos))
                s = r.iloc[:, 0] if r.shape[1] == 1 else r.sum(axis=1)
                idx = pd.DatetimeIndex(s.index)
                s.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
                pd.DataFrame({"fecha": s.index.strftime("%Y-%m-%d"), "sim": s.values}).to_csv(ruta, index=False)
            except Exception as e:
                print("  ! retrospectiva: fallo la descarga (%s); se usa la copia guardada" % str(e)[:100])
        if not os.path.exists(ruta):
            raise SystemExit("No hay retrospectiva de GEOGLOWS (ni descargada ni guardada en %s)" % ruta)
        r = pd.read_csv(ruta, parse_dates=["fecha"])
        return pd.Series(r.sim.values, index=pd.DatetimeIndex(r.fecha), name="sim")

    def pronostico(self, fecha=None):
        """Pronostico vigente (fecha=None) o el emitido en fecha (Timestamp)."""
        if fecha is not None:
            ruta = os.path.join(self.cache, "emisiones", fecha.strftime("%Y%m%d") + ".csv")
            if os.path.exists(ruta):
                return self._leer(ruta), fecha
            if self.sin_descarga:
                return None, None
            d, _ = self._bajar_pronostico(fecha.strftime("%Y%m%d"))
            if d is not None:
                self._guardar(d, ruta)
            return d, fecha
        # vigente
        if not self.sin_descarga:
            d, emitido = self._bajar_pronostico()
            if d is not None:
                self._guardar(d, os.path.join(self.cache, "emisiones", emitido.strftime("%Y%m%d") + ".csv"))
                return d, emitido
            print("  ! se usa el ultimo pronostico guardado")
        guardados = sorted(os.listdir(os.path.join(self.cache, "emisiones")))
        if not guardados:
            raise SystemExit("No hay pronostico de GEOGLOWS (ni descargado ni guardado en %s)" % self.cache)
        f = pd.Timestamp(guardados[-1][:8])
        return self._leer(os.path.join(self.cache, "emisiones", guardados[-1])), f


# ============================================================ correccion de sesgo

def qm(x, s, o):
    n = len(s)
    p = np.arange(1, n + 1) / (n + 1.0)                 # Weibull
    su, inv = np.unique(s, return_inverse=True)
    pu = np.bincount(inv, weights=p) / np.bincount(inv)  # empates: prob. media
    return np.interp(np.interp(x, su, pu), p, o)


def corregir(x, s, o):
    x = np.asarray(x, float)
    y = np.full_like(x, np.nan)
    ok = np.isfinite(x)
    lo_, hi_ = s[0], s[-1]
    yc = qm(np.clip(x[ok], lo_, hi_), s, o)
    fac = np.ones_like(yc)
    alto, bajo = x[ok] > hi_, x[ok] < lo_
    fac[alto] = x[ok][alto] / hi_ if hi_ > 0 else 1.0
    fac[bajo] = x[ok][bajo] / lo_ if lo_ > 0 else 0.0
    y[ok] = yc * fac
    return y


def corregir_pronostico(pron, curvas):
    out = pron.copy()
    miembros = [c for c in pron.columns if c != "fecha"]
    for i, f in enumerate(pd.to_datetime(pron.fecha)):
        s, o = curvas[f.month]
        out.loc[i, miembros] = corregir(pron.loc[i, miembros].to_numpy(float), s, o)
    return out


# ============================================================ salidas

def ultimo_obs(col):
    s = col.dropna()
    return float(s.iloc[-1]) if len(s) else 0.0


# ============================================================ balance (referencia)
# La pagina HTML hace este mismo calculo en JavaScript (pronostico/nucleo.js).
# Esta version en Python se usa para comprobar que las dos dan lo mismo.

class Embalse:
    def __init__(self, curva, v_min, v_max, vertedero, tabla_gasto):
        self.niv = curva.nivel.values.astype(float)
        self.vol = curva.volumen.values.astype(float)
        self.area = curva.area_km2.values.astype(float)
        self.v_min, self.v_max = v_min, v_max
        self.vert, self.tabla = vertedero, tabla_gasto

    def nivel_de_vol(self, v):
        return np.interp(v, self.vol, self.niv)

    def area_de_vol(self, v):
        return np.interp(v, self.vol, self.area)

    def gasto(self, carga):
        if self.tabla is not None:
            return np.interp(self.vert["cresta"] + carga, self.tabla.nivel.values,
                             self.tabla.gasto_m3s.values, left=0.0)
        return self.vert["a"] * carga ** self.vert["b"]

    def propagar(self, Q, V0, entrega, desembalse, vertimiento, evap_mm=0.0, pasos=24):
        n_dias, n_miem = Q.shape
        V = np.full(n_miem, float(V0))
        oV, oE, oD, oS = (np.empty((n_dias, n_miem)) for _ in range(4))
        for d in range(n_dias):
            qin = Q[d] * CMS_A_HM3
            ev = evap_mm * self.area_de_vol(V) / 1000.0 if evap_mm else 0.0
            if np.isfinite(vertimiento[d]):
                vert = np.full(n_miem, float(vertimiento[d]) * CMS_A_HM3)
                vert = np.minimum(vert, np.maximum(0.0, V + qin - ev - self.v_min))
                disp = np.maximum(0.0, V + qin - ev - vert - self.v_min)
                ent = np.minimum(entrega[d], disp)
                des = np.minimum(desembalse[d], disp - ent)
                V = V + qin - ev - ent - des - vert
                extra = np.maximum(0.0, V - self.v_max)
                V, vert = V - extra, vert + extra
            elif not self.vert:
                disp = np.maximum(0.0, V + qin - ev - self.v_min)
                ent = np.minimum(entrega[d], disp)
                des = np.minimum(desembalse[d], disp - ent)
                Vt = V + qin - ev - ent - des
                vert = np.maximum(0.0, Vt - self.v_max)
                V = Vt - vert
            else:
                disp = np.maximum(0.0, V + qin - ev - self.v_min)
                ent = np.minimum(entrega[d], disp)
                des = np.minimum(desembalse[d], disp - ent)
                neto = (qin - ev - ent - des) / pasos
                vert = np.zeros(n_miem)
                for _ in range(pasos):
                    carga = np.maximum(self.nivel_de_vol(V) - self.vert["cresta"], 0.0)
                    s = self.gasto(carga) * CMS_A_HM3 / pasos
                    s = np.minimum(s, np.maximum(V + neto - self.vert["v_cresta"], 0.0))
                    V, vert = V + neto - s, vert + s
                extra = np.maximum(0.0, V - self.v_max)
                V, vert = V - extra, vert + extra
            oV[d], oE[d], oD[d], oS[d] = V, ent, des, vert
        return oV, self.nivel_de_vol(oV), oE, oD, oS


def tabla_balance(fechas, Q, V, N, EN, DE, VE, ent_pedida, des_pedida):
    res = pd.DataFrame({"fecha": fechas})
    for q in (10, 25, 50, 75, 90):
        res["nivel_p%02d" % q] = np.nanpercentile(N, q, axis=1)
    for q in (10, 25, 50, 75, 90):
        res["volumen_p%02d" % q] = np.nanpercentile(V, q, axis=1)
    vivos = np.isfinite(N)
    res["entrega_pedida"] = ent_pedida
    res["entrega_p50"] = np.nanmedian(np.where(vivos, EN, np.nan), axis=1)
    res["desembalse_pedido"] = des_pedida
    res["desembalse_p50"] = np.nanmedian(np.where(vivos, DE, np.nan), axis=1)
    res["vertimiento_p50"] = np.nanmedian(np.where(vivos, VE, np.nan), axis=1)
    res["vertimiento_max"] = np.nanmax(np.where(vivos, VE, np.nan), axis=1)
    res["salida_total_p50"] = res.entrega_p50 + res.desembalse_p50 + res.vertimiento_p50
    res["Qin_p50_cms"] = np.nanmedian(Q, axis=1)
    res["miembros_que_vierten"] = (VE > 0).sum(axis=1)
    res["miembros_vivos"] = vivos.sum(axis=1)
    return res


# ============================================================ pronostico de un embalse

def calcular_datos(nombre, ficha_cfg, gen, carpeta, emision=None, hasta=None, sin_descarga=False):
    """Todo el calculo de un embalse, sin escribir nada salvo la copia de GEOGLOWS.

    Devuelve (datos, ctx): datos es el diccionario que usa la pagina (y la app
    Tethys); ctx guarda lo necesario para escribir los CSV por defecto y la
    serie limpia. Devuelve (None, None) si no se puede pronosticar.
    """
    ficha = {k: float(v) for k, v in ficha_cfg["ficha"].items()}
    tramos = [int(t) for t in ficha_cfg["tramos"]]
    vertedero = dict(ficha_cfg["vertedero"]) if ficha_cfg.get("vertedero") else None
    vmax_cfg = ficha_cfg.get("vmax")
    zona, hora = gen["zona"], int(gen["hora_lectura"])
    ventana_ar, dias_previos = int(gen["ventana_ar"]), int(gen["dias_previos"])
    evap = float(gen.get("evaporacion_mm", 0.0))

    base = os.path.abspath(carpeta)
    ruta_limpio = os.path.join(base, "Clean_Data", nombre, "%s_diario_limpio.csv" % nombre)
    if not os.path.exists(ruta_limpio):
        print("  ! no existe %s" % ruta_limpio)
        return None, None

    # ---------------------------------------------------- 1. serie limpia
    limpio = pd.read_csv(ruta_limpio, parse_dates=["fecha"], encoding="utf-8-sig")
    limpio["Qin_recortado"] = limpio.Qin_recortado.astype(str).str.lower().eq("true")
    if hasta:           # para reproducir un dia pasado
        limpio = limpio[limpio.fecha <= pd.Timestamp(hasta)].reset_index(drop=True)
    print("serie limpia: %d dias, %s a %s" % (len(limpio), limpio.fecha.min().date(), limpio.fecha.max().date()))

    # ---------------------------------------------------- 2. curva y aliviadero
    lo = ficha["nm"] - 0.15 * (ficha["cotacorona"] - ficha["nm"])
    hi = ficha["cotacorona"]
    curva_obs = ajustar_curva(limpio.dropna(subset=["nivel", "volumen"]), lo, hi,
                              area_max_km2=ficha["areanam"] / 100.0)
    curva = extender_curva(curva_obs, ficha)
    v_min = ficha["volumennm"]
    tabla_gasto = None
    if vertedero and vertedero.get("tabla"):
        ruta_t = vertedero["tabla"]
        if not os.path.isabs(ruta_t):
            ruta_t = os.path.join(base, "Clean_Data", nombre, ruta_t)
        tabla_gasto = pd.read_csv(ruta_t).sort_values("nivel")
        vertedero.setdefault("cresta", float(tabla_gasto.nivel[tabla_gasto.gasto_m3s > 0].min()))
        vertedero["v_cresta"] = float(np.interp(vertedero["cresta"], curva.nivel, curva.volumen))
        v_max = ficha["volumennam"]
        txt_vert = "aliviadero libre, curva de gasto oficial (%s), cresta %.2f m" % (os.path.basename(ruta_t), vertedero["cresta"])
    elif vertedero:
        tramos_carga = list(np.round(np.arange(0, 1.01, 0.1), 2)) + [1.25, 1.5, 2.0, 5.0]
        dv = limpio[(limpio.fecha >= pd.Timestamp(vertedero["desde"])) & limpio.calidad.isin(["ok", "sin_0800"])]
        dv = dv[(dv.nivel > vertedero["cresta"]) & (dv.vertimiento > 0)]
        carga = dv.nivel - vertedero["cresta"]
        tv = (dv.assign(H=carga).groupby(pd.cut(carga, tramos_carga), observed=True)
                .agg(dias=("H", "size"), H=("H", "median"), Q=("vertimiento", "median")))
        tv = tv[tv.dias >= 5]
        b_v, la_v = np.polyfit(np.log(tv.H), np.log(tv.Q), 1)
        vertedero.update(a=float(np.exp(la_v)), b=float(b_v),
                         v_cresta=float(np.interp(vertedero["cresta"], curva.nivel, curva.volumen)))
        v_max = ficha["volumennam"]
        txt_vert = ("aliviadero libre: Q = %.2f (h - %.2f)^%.3f m3/s (%d dias vertiendo desde %s)"
                    % (vertedero["a"], vertedero["cresta"], vertedero["b"], len(dv), vertedero["desde"]))
    else:
        v_max = float(vmax_cfg) if vmax_cfg is not None else ficha["volumenautorizado"]
        txt_vert = ("compuertas: techo en %.1f hm3 (%s)"
                    % (v_max, "vmax" if vmax_cfg is not None else "volumen autorizado"))
    emb = Embalse(curva, v_min, v_max, vertedero, tabla_gasto)
    print("curva: %d puntos observados (%.2f a %.2f m), extendida %.2f a %.2f m"
          % (len(curva_obs), curva_obs.nivel.min(), curva_obs.nivel.max(), curva.nivel.min(), curva.nivel.max()))
    print(txt_vert)

    # condicion inicial
    ini = limpio.dropna(subset=["volumen"]).iloc[-1]
    F0, V0 = ini.fecha, float(ini.volumen)

    # ---------------------------------------------------- 3. GEOGLOWS
    cache = os.path.join(base, "Forecast_Data", "GEOGLOWS", nombre)
    gg = Geoglows(tramos, zona, hora, cache, sin_descarga=sin_descarga)
    retro = gg.retrospectiva()
    print("retrospectiva: %d dias, %s a %s" % (len(retro), retro.index.min().date(), retro.index.max().date()))
    fecha_em = pd.Timestamp(emision) if emision else None
    pron_raw, EMISION = gg.pronostico(fecha_em)
    if pron_raw is None:
        print("  ! no hay pronostico emitido el %s" % emision)
        return None, None
    SELLO = EMISION.strftime("%Y%m%d")
    MIEMBROS = [c for c in pron_raw.columns if c != "fecha"]
    print("pronostico emitido %s: %d dias x %d miembros" % (EMISION.date(), len(pron_raw), len(MIEMBROS)))
    avisos = []
    if (EMISION - F0).days > 1:
        avisos.append("La ultima lectura del embalse (%s) tiene %d dias de atraso respecto al pronostico."
                      % (F0.date(), (EMISION - F0).days))

    archivo = {}
    fechas_arc = pd.date_range(F0 - pd.Timedelta(days=ventana_ar + 1), EMISION - pd.Timedelta(days=1), freq="D")
    faltan = [f for f in fechas_arc if not os.path.exists(os.path.join(cache, "emisiones", f.strftime("%Y%m%d") + ".csv"))]
    if faltan and not sin_descarga:
        print("bajando %d pronosticos emitidos que no estaban guardados..." % len(faltan))
    for f in fechas_arc:
        d, _ = gg.pronostico(f)
        if d is not None:
            archivo[f] = d
    print("pronosticos emitidos para el AR(1): %d de %d" % (len(archivo), len(fechas_arc)))

    # ---------------------------------------------------- 4. correccion de sesgo
    obs_ref = limpio.set_index("fecha").Qin_cms
    par = pd.concat({"sim": retro, "obs": obs_ref}, axis=1, join="inner").dropna()
    curvas = {m: (np.sort(par.sim[par.index.month == m].to_numpy(float)),
                  np.sort(par.obs[par.index.month == m].to_numpy(float))) for m in range(1, 13)}
    pron_bc = corregir_pronostico(pron_raw, curvas)
    archivo_bc = {f: corregir_pronostico(d, curvas) for f, d in archivo.items()}

    # ---------------------------------------------------- 5. condiciones iniciales
    ok = limpio.Qin_cms.notna() & ~limpio.Qin_recortado
    obs_ar = limpio[ok].set_index("fecha").Qin_cms
    obs_ar = obs_ar[(obs_ar.index > F0 - pd.Timedelta(days=ventana_ar)) & (obs_ar.index <= F0)]
    emitidos = dict(archivo_bc)
    emitidos[EMISION] = pron_bc
    inits = pd.DatetimeIndex(sorted(emitidos))

    def m52_de(dia):
        for t in inits[inits <= dia][::-1]:
            d = emitidos[t]
            v = d.loc[pd.to_datetime(d.fecha) == dia, "ensemble_52"]
            if len(v) and np.isfinite(v.iloc[0]):
                return float(v.iloc[0]), t
        return np.nan, None

    filas = []
    for dia, q in obs_ar.items():
        v, t = m52_de(dia)
        if np.isfinite(v):
            filas.append((dia, v - q))
    ar = {"pares": len(filas)}
    if len(filas) < 5:
        pron_ic = pron_bc.copy()
        avisos.append("Solo %d pares de error para el AR(1): la version (c) queda igual a la (b)." % len(filas))
    else:
        from statsmodels.tsa.arima.model import ARIMA
        err = np.array([e for _, e in filas])
        fit = ARIMA(err, order=(1, 0, 0)).fit()
        MU, PHI = float(fit.params[0]), float(fit.params[1])
        E_NOW, T_NOW = float(err[-1]), pd.Timestamp(filas[-1][0])
        dt_dias = (pd.to_datetime(pron_bc.fecha) - T_NOW).dt.days.to_numpy(float)
        e_hat = MU + (PHI ** dt_dias) * (E_NOW - MU)
        Qc = pron_bc[MIEMBROS].to_numpy(float) - e_hat[:, None]
        pron_ic = pron_bc.copy()
        pron_ic[MIEMBROS] = np.where(np.isfinite(Qc), np.maximum(Qc, 0.0), np.nan)
        ar.update(mu=MU, phi=PHI, e_now=E_NOW, t_now=str(T_NOW.date()))
        print("AR(1): %d pares | mu %.2f  phi %.3f  e_now %.2f m3/s" % (len(filas), MU, PHI, E_NOW))
        if PHI < 0:
            avisos.append("El AR(1) dio phi negativo: la correccion cambia de signo dia a dia.")

    # ---------------------------------------------------- 6. salidas por defecto
    sel = pron_raw.fecha > F0
    fechas_bal = pron_raw.fecha[sel].reset_index(drop=True)
    n_bal = len(fechas_bal)
    if n_bal == 0:
        print("  ! el pronostico no llega mas alla de la ultima lectura (%s)" % F0.date())
        return None, None
    ent_base = np.full(n_bal, ultimo_obs(limpio.entrega))
    des_base = np.full(n_bal, ultimo_obs(limpio.desembalse))
    if vertedero:
        ver_base = np.full(n_bal, np.nan)           # lo resuelve el aliviadero
    else:
        ver_base = np.full(n_bal, ultimo_obs(limpio.vertimiento))

    PRON = {"Raw": pron_raw, "Bias_Correction": pron_bc, "Bias_Correction+Assimilation": pron_ic}
    Q = {k: p[p.fecha > F0][MIEMBROS].to_numpy(float) for k, p in PRON.items()}

    # ---------------------------------------------------- 7. pagina
    obs = limpio.dropna(subset=["volumen"])
    obs = obs[obs.fecha > F0 - pd.Timedelta(days=dias_previos)]

    def lista(a, nd=None):
        return [None if not np.isfinite(x) else (float(x) if nd is None else round(float(x), nd))
                for x in np.asarray(a, float)]

    vert_js = None
    if vertedero:
        vert_js = {"cresta": vertedero["cresta"], "v_cresta": vertedero["v_cresta"]}
        if tabla_gasto is not None:
            vert_js["tabla"] = {"nivel": lista(tabla_gasto.nivel, 6), "gasto": lista(tabla_gasto.gasto_m3s, 6)}
        else:
            vert_js.update(a=vertedero["a"], b=vertedero["b"])

    datos = {
        "embalse": nombre,
        "emision": str(EMISION.date()),
        "sello": SELLO,
        "generado": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "f0": str(F0.date()),
        "hora0": str(ini.get("hora", "")),
        "calidad0": str(ini.get("calidad", "")),
        "nivel0": float(ini.nivel),
        "v0": V0,
        "fechas": [str(f.date()) for f in fechas_bal],
        "miembros": MIEMBROS,
        "versiones": [{"clave": k, "titulo": t} for k, t in VERSIONES],
        "Q": {k: [[None if not np.isfinite(x) else float(x) for x in fila] for fila in q] for k, q in Q.items()},
        "curva": {"nivel": [float(x) for x in curva.nivel], "volumen": [float(x) for x in curva.volumen],
                  "area": [float(x) for x in curva.area_km2]},
        "v_min": v_min, "v_max": v_max,
        "vertedero": vert_js,
        "evap_mm": evap,
        "base": {"entrega": lista(ent_base), "desembalse": lista(des_base),
                 "vertimiento": [None if not np.isfinite(x) else float(x) for x in ver_base]},
        "obs": {"fecha": [str(f.date()) for f in obs.fecha], "nivel": lista(obs.nivel, 4),
                "volumen": lista(obs.volumen, 4), "entrega": lista(obs.entrega.fillna(0.0), 4),
                "desembalse": lista(obs.desembalse.fillna(0.0), 4),
                "vertimiento": lista(obs.vertimiento.fillna(0.0), 4)},
        "info": {"vertedero": txt_vert, "ar": ar, "avisos": avisos,
                 "curva": "%d puntos observados, %.2f a %.2f m" % (len(curva_obs), curva_obs.nivel.min(), curva_obs.nivel.max()),
                 "retro": "%s a %s" % (retro.index.min().date(), retro.index.max().date()),
                 "emisiones_ar": len(archivo)},
    }

    ctx = {"emb": emb, "Q": Q, "fechas_bal": fechas_bal, "MIEMBROS": MIEMBROS,
           "ent": ent_base, "des": des_base, "ver": ver_base, "evap": evap,
           "PRON": PRON, "F0": F0, "SELLO": SELLO, "limpio": limpio, "base": base}
    return datos, ctx


def pronosticar(nombre, ficha_cfg, gen, args):
    t0 = time.time()
    print("\n=== %s ===" % nombre)
    datos, ctx = calcular_datos(nombre, ficha_cfg, gen, args.carpeta, emision=args.emision,
                                hasta=args.hasta, sin_descarga=args.sin_descarga)
    if datos is None:
        return None
    SELLO = ctx["SELLO"]
    destino = os.path.join(ctx["base"], "Pronosticos", nombre)
    os.makedirs(destino, exist_ok=True)
    ruta_html = os.path.join(destino, "%s_pronostico_%s.html" % (nombre, SELLO))
    escribir_pagina(datos, ruta_html)
    print("pagina: %s  (%.0f s)" % (ruta_html, time.time() - t0))

    if args.csv:     # resultados con las salidas por defecto, sin pasar por la pagina
        escribir_csv_defecto(datos, ctx["emb"], ctx["Q"], ctx["fechas_bal"], ctx["MIEMBROS"], ctx["ent"],
                             ctx["des"], ctx["ver"], ctx["evap"], os.path.join(destino, "csv_%s" % SELLO),
                             nombre, SELLO, ctx["PRON"], ctx["F0"])
    return ruta_html


def escribir_csv_defecto(datos, emb, Q, fechas_bal, MIEMBROS, ent, des, ver, evap, carpeta, nombre, SELLO, PRON, F0):
    fechas = fechas_bal.dt.strftime("%Y-%m-%d")
    for clave, _ in VERSIONES:
        d = os.path.join(carpeta, clave)
        os.makedirs(d, exist_ok=True)
        V, N, EN, DE, VE = emb.propagar(Q[clave], datos["v0"], ent, des, ver, evap)
        res = tabla_balance(fechas.values, Q[clave], V, N, EN, DE, VE, ent, des)
        res.round(4).to_csv(os.path.join(d, "%s_balance_%s.csv" % (nombre, SELLO)), index=False)
        mi = pd.DataFrame(N, columns=MIEMBROS); mi.insert(0, "fecha", res.fecha)
        mi.round(4).to_csv(os.path.join(d, "%s_balance_%s_miembros.csv" % (nombre, SELLO)), index=False)
        mv = pd.DataFrame(V, columns=MIEMBROS); mv.insert(0, "fecha", res.fecha)
        mv.round(4).to_csv(os.path.join(d, "%s_balance_%s_miembros_volumen.csv" % (nombre, SELLO)), index=False)
        p = PRON[clave]
        p = p[p.fecha > F0].reset_index(drop=True)
        p.assign(fecha=p.fecha.dt.strftime("%Y-%m-%d")).round(4).to_csv(
            os.path.join(d, "%s_pronostico_%s.csv" % (nombre, SELLO)), index=False)


def escribir_pagina(datos, ruta):
    carpeta = os.path.join(AQUI, "pronostico")
    with open(os.path.join(carpeta, "plantilla.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(carpeta, "nucleo.js"), encoding="utf-8") as f:
        nucleo = f.read()
    with open(os.path.join(carpeta, "pagina.js"), encoding="utf-8") as f:
        pagina = f.read()
    with open(os.path.join(carpeta, "pagina.css"), encoding="utf-8") as f:
        estilos = f.read()
    try:
        from plotly.offline import get_plotlyjs
        plotly_js = get_plotlyjs()
    except ImportError:
        raise SystemExit("Falta plotly:  pip install plotly")
    js_datos = json.dumps(datos, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    html = html.replace("/*__PLOTLY__*/", plotly_js.replace("</script", "<\\/script"))
    html = html.replace("/*__CSS__*/", estilos)
    html = html.replace("/*__NUCLEO__*/", nucleo)
    html = html.replace("/*__PAGINA__*/", pagina)
    html = html.replace("/*__DATOS__*/", "const DATOS = " + js_datos.replace("</", "<\\/") + ";")
    html = html.replace("__TITULO__", "%s - pronostico %s" % (datos["embalse"], datos["emision"]))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(html)


# ============================================================ main

def main():
    p = argparse.ArgumentParser(description="Pronostico de nivel y volumen de embalses con GEOGLOWS, con pagina HTML.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--embalse", help="nombre como en embalses_pronostico.json (p. ej. Zaza)")
    g.add_argument("--todos", action="store_true", help="todos los embalses del archivo de configuracion")
    p.add_argument("--carpeta", default=os.path.join(AQUI, "..", "Operative_Forecast"),
                   help="carpeta Operative_Forecast (por defecto ..\\Operative_Forecast)")
    p.add_argument("--config", default=os.path.join(AQUI, "embalses_pronostico.json"))
    p.add_argument("--emision", default=None, help="AAAA-MM-DD: usar un pronostico pasado en vez del vigente")
    p.add_argument("--hasta", default=None, help="AAAA-MM-DD: usar la serie limpia solo hasta esa fecha")
    p.add_argument("--sin-descarga", action="store_true",
                   help="no descargar GEOGLOWS; usar solo lo guardado en Forecast_Data\\GEOGLOWS")
    p.add_argument("--csv", action="store_true",
                   help="ademas de la pagina, escribir los CSV con las salidas por defecto")
    p.add_argument("--no-abrir", action="store_true", help="no abrir la pagina en el navegador")
    args = p.parse_args()

    gen, embalses = leer_config(args.config)
    nombres = list(embalses) if args.todos else [args.embalse]
    for n in nombres:
        if n not in embalses:
            raise SystemExit("'%s' no esta en %s. Embalses: %s" % (n, args.config, ", ".join(embalses)))
    paginas = []
    for n in nombres:
        try:
            r = pronosticar(n, embalses[n], gen, args)
            if r:
                paginas.append(r)
        except SystemExit:
            raise
        except Exception as e:
            print("  ! %s: %s" % (n, e))
            if not args.todos:
                raise
    if paginas and not args.no_abrir and not args.todos:
        webbrowser.open(pathlib.Path(os.path.abspath(paginas[0])).as_uri())
    print("\nListo: %d pagina(s)." % len(paginas))


if __name__ == "__main__":
    main()

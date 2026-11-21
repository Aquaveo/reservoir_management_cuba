"""Pruebas del calculo, sin Django:  python -m pytest tethysapp/reservoir_management_cuba/tests

La comparacion completa con los Colabs (mismos CSV byte a byte) se hizo aparte;
aqui se cuidan las reglas que no se pueden romper.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datos as D          # noqa: E402
import pronosticar as P    # noqa: E402


def curva_lineal():
    niv = np.linspace(10.0, 30.0, 201)
    vol = (niv - 10.0) * 10.0          # 10 hm3 por metro
    return pd.DataFrame({'nivel': niv, 'volumen': vol, 'area_km2': np.full_like(niv, 10.0)})


def test_piso_recorta_la_entrega():
    emb = P.Embalse(curva_lineal(), v_min=20.0, v_max=150.0, vertedero=None, tabla_gasto=None)
    Q = np.zeros((5, 3))
    V, N, EN, DE, VE = emb.propagar(Q, 30.0, np.full(5, 8.0), np.full(5, 1.0), np.full(5, 0.0))
    assert np.all(V >= 20.0 - 1e-9)
    assert EN.sum(axis=0)[0] <= 10.0 + 1e-9         # solo habia 10 hm3 sobre el piso
    assert np.all(DE[:2] <= EN[:2] + 1e-9)           # la entrega va antes que el desembalse


def test_techo_con_compuertas():
    emb = P.Embalse(curva_lineal(), v_min=0.0, v_max=100.0, vertedero=None, tabla_gasto=None)
    Q = np.full((4, 2), 500.0)                       # 43.2 hm3/dia
    V, N, EN, DE, VE = emb.propagar(Q, 90.0, np.zeros(4), np.zeros(4), np.full(4, np.nan))
    assert np.all(V <= 100.0 + 1e-9)
    assert VE[0, 0] > 0


def test_aliviadero_libre_vierte_sobre_la_cresta():
    vert = {'cresta': 25.0, 'a': 100.0, 'b': 1.5, 'v_cresta': 150.0}
    emb = P.Embalse(curva_lineal(), v_min=0.0, v_max=200.0, vertedero=vert, tabla_gasto=None)
    Q = np.full((6, 1), 200.0)
    V, N, EN, DE, VE = emb.propagar(Q, 160.0, np.zeros(6), np.zeros(6), np.full(6, np.nan))
    assert VE.sum() > 0
    assert np.all(V >= 150.0 - 1e-6)                 # el aliviadero no baja el nivel de la cresta


def test_mapeo_de_cuantiles_identidad():
    s = np.sort(np.random.default_rng(1).gamma(2.0, 10.0, 400))
    x = np.array([s[10], s[200], s[390]])
    assert np.allclose(P.corregir(x, s, s), x, rtol=1e-6)


def test_nombres_en_la_url():
    for c in D.embalses():
        assert D.de_url(D.a_url(c)) == c


def test_configuracion_completa():
    _, cfgs = D.leer_config()
    assert len(cfgs) == 13
    for c, cfg in cfgs.items():
        for k in ('ide', 'lat', 'lon', 'ficha', 'tramos'):
            assert k in cfg, (c, k)
        assert cfg['tramos'], c

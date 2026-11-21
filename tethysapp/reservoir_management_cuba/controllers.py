"""Paginas de la app. Todo sale del cache que escribe actualizar_embalses.py:
las paginas nunca descargan GEOGLOWS ni recalculan en el servidor. El balance
interactivo corre en el navegador (public/pronostico/nucleo.js), el mismo
codigo que la pagina de pronosticar.py."""

from django.http import Http404
from django.shortcuts import render, reverse

from tethys_sdk.routing import controller

from . import datos as D
from .app import ReservoirManagementCuba as App


def _menu(actual=None):
    _, cfgs = D.leer_config()
    return [{'clave': c, 'nombre': cfg.get('nombre', c), 'activo': c == actual,
             'url': reverse('reservoir_management_cuba:embalse', kwargs={'embalse': D.a_url(c)})}
            for c, cfg in cfgs.items()]


def _ajuste(nombre, defecto):
    try:
        v = App.get_custom_setting(nombre)
        return v if v else defecto
    except Exception:
        return defecto


def _nivel_de_vol(curva, v):
    import numpy as np
    return float(np.interp(v, curva['volumen'], curva['nivel']))


def _referencias(cotas, junto=0.05):
    """Lineas de referencia del historico. Las que caen a la misma cota (p. ej. el
    NAN y el volumen autorizado en Protesta) van en una sola, con las dos etiquetas."""
    out = []
    for nombre, nivel in sorted(cotas, key=lambda c: c[1]):
        if out and abs(nivel - out[-1]['nivel']) <= junto:
            out[-1]['nombre'] += ' y ' + nombre
        else:
            out.append({'nombre': nombre, 'nivel': float(nivel)})
    return out


@controller(app_workspace=True)
def home(request, app_workspace):
    """Mapa y lista de los embalses con su ultimo dato y el pronostico por defecto."""
    _, cfgs = D.leer_config()
    filas = []
    for item in _menu():
        c = item['clave']
        cfg = cfgs[c]
        cache = D.leer_cache(c, app_workspace.path)
        fila = {'clave': c, 'nombre': cfg.get('nombre', c), 'provincia': cfg.get('provincia', ''),
                'lat': cfg['lat'], 'lon': cfg['lon'], 'url': item['url'], 'hay': bool(cache)}
        if cache:
            d = cache['datos']
            f = cache['ficha']
            r = cache['resumen']['Bias_Correction+Assimilation']
            fila.update({
                'f0': d['f0'], 'nivel0': round(d['nivel0'], 2), 'v0': round(d['v0'], 1),
                'emision': d['emision'],
                'pct': round(100.0 * d['v0'] / f['volumenautorizado'], 0) if f.get('volumenautorizado') else None,
                'nivel_fin': round(r['nivel_p50'], 2), 'fecha_fin': r['fecha_fin'],
                'cambio': round(r['nivel_p50'] - d['nivel0'], 2),
                'vierten': r['miembros_que_vierten'],
                'nan': f.get('nan'), 'nm': f.get('nm'),
                'atraso': len(d['info'].get('avisos', [])) > 0,
                'generado': cache['generado'],
            })
        filas.append(fila)
    contexto = {
        'menu': _menu(),
        'filas': filas,
        'mapa': {'teselas': _ajuste('url_teselas', 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'),
                 'atribucion': _ajuste('atribucion_teselas', '&copy; colaboradores de OpenStreetMap'),
                 'embalses': filas},
    }
    return render(request, 'reservoir_management_cuba/home.html', contexto)


@controller(url='embalse/{embalse}', app_workspace=True)
def embalse(request, embalse, app_workspace):
    """Pagina de un embalse: historico (serie limpia) y pronostico interactivo."""
    clave = D.de_url(embalse)
    if clave is None:
        raise Http404('Embalse desconocido')
    cfg = D.ficha(clave)
    cache = D.leer_cache(clave, app_workspace.path)
    contexto = {
        'menu': _menu(clave),
        'clave': clave,
        'nombre': cfg.get('nombre', clave),
        'provincia': cfg.get('provincia', ''),
        'hay': bool(cache),
    }
    if cache:
        f = cache['ficha']
        curva = cache['datos']['curva']
        contexto.update({
            'generado': cache['generado'],
            'datos': cache['datos'],
            'historia': {
                **cache['historia'],
                'referencias': _referencias([
                    ('Nivel muerto', f['nm']),
                    ('NAN', f['nan']),
                    ('NAM', f['nam']),
                    ('volumen autorizado', round(_nivel_de_vol(curva, f['volumenautorizado']), 2)),
                ]),
                'nombre': cfg.get('nombre', clave),
            },
        })
    return render(request, 'reservoir_management_cuba/embalse.html', contexto)

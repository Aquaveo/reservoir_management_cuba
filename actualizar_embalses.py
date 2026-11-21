"""Actualizacion diaria de la app. Hace los tres pasos, con los datos que
viven dentro de la app (workspaces/app_workspace/Operative_Forecast):

  1. Descarga del portal SGIA las lecturas nuevas      -> Raw_Data
  2. Limpia la serie (con las correcciones manuales)   -> Clean_Data
  3. Pronostica con GEOGLOWS y deja el resultado en el cache de la app

    python actualizar_embalses.py                 # los 13 embalses
    python actualizar_embalses.py Zaza Mayari     # solo algunos
    python actualizar_embalses.py --sin-portal    # solo el paso 3 (datos del portal ya al dia)

Usa el Python del entorno de Tethys (necesita geoglows, statsmodels, pandas,
requests). No depende de Django, asi que se puede programar con el Programador
de tareas de Windows o con cron. El calculo es el mismo de pronosticar.py y de
los Colabs.
"""

import argparse
import os
import subprocess
import sys
import time

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(AQUI, 'tethysapp', 'reservoir_management_cuba')
sys.path.insert(0, APP)

import datos as D          # noqa: E402
import pronosticar as P    # noqa: E402

WORKSPACE = os.path.join(APP, 'workspaces', 'app_workspace')
DATOS = os.path.join(WORKSPACE, 'Operative_Forecast')
SCRIPTS = os.path.join(APP, 'scripts')


# ------------------------------------------------------------ pasos 1 y 2

def correr(script, *argumentos):
    """Corre uno de los scripts del portal con el mismo Python. Devuelve True si termino bien."""
    cmd = [sys.executable, os.path.join(SCRIPTS, script)] + list(argumentos)
    r = subprocess.run(cmd)
    return r.returncode == 0


def descargar_portal(claves, cfgs):
    crudo = os.path.join(DATOS, 'Raw_Data')
    ok = True
    for c in claves:
        print('\n--- portal SGIA: %s' % c, flush=True)
        ok &= correr('descargar_portal_crudo.py', '--ide', str(cfgs[c]['ide']), '--carpeta', crudo)
    return ok


def limpiar(claves):
    crudos = [os.path.join(DATOS, 'Raw_Data', c, c + '_portal_crudo.csv') for c in claves]
    crudos = [r for r in crudos if os.path.exists(r)]
    if not crudos:
        print('! no hay datos crudos que limpiar')
        return False
    print('\n--- limpieza', flush=True)
    return correr('limpiar_portal.py', *crudos, '--carpeta', os.path.join(DATOS, 'Clean_Data'))


# ------------------------------------------------------------ paso 3

def serie(col, nd=4):
    return [None if not np.isfinite(x) else round(float(x), nd) for x in col.to_numpy(float)]


def resumen(datos, ctx):
    """Resultado con las salidas por defecto, por version (mapa y lista de la portada)."""
    out = {}
    fechas = ctx['fechas_bal'].dt.strftime('%Y-%m-%d').values
    for clave, _ in P.VERSIONES:
        V, N, EN, DE, VE = ctx['emb'].propagar(ctx['Q'][clave], datos['v0'], ctx['ent'], ctx['des'],
                                                ctx['ver'], ctx['evap'])
        t = P.tabla_balance(fechas, ctx['Q'][clave], V, N, EN, DE, VE, ctx['ent'], ctx['des'])
        fin = t.iloc[-1]
        out[clave] = {
            'fecha_fin': str(fin.fecha),
            'nivel_p10': round(float(fin.nivel_p10), 3), 'nivel_p50': round(float(fin.nivel_p50), 3),
            'nivel_p90': round(float(fin.nivel_p90), 3), 'volumen_p50': round(float(fin.volumen_p50), 3),
            'miembros_que_vierten': int((np.nansum(VE, axis=0) > 0).sum()),
        }
    return out


def historia(limpio):
    h = limpio.dropna(subset=['nivel'])
    return {
        'fecha': h.fecha.dt.strftime('%Y-%m-%d').tolist(),
        'nivel': serie(h.nivel), 'volumen': serie(h.volumen), 'Qin_cms': serie(h.Qin_cms, 3),
        'entrega': serie(h.entrega), 'desembalse': serie(h.desembalse), 'vertimiento': serie(h.vertimiento, 3),
        'calidad': h.calidad.fillna('').astype(str).tolist(),
    }


def pronosticar(clave, gen, cfg, carpeta, sin_descarga=False, emision=None, hasta=None):
    t0 = time.time()
    print('\n=== %s ===' % clave, flush=True)
    datos, ctx = P.calcular_datos(clave, cfg, gen, carpeta, emision=emision, hasta=hasta,
                                  sin_descarga=sin_descarga)
    if datos is None:
        return 'sin pronostico'
    datos['generado'] = time.strftime('%Y-%m-%d %H:%M')
    contenido = {
        'clave': clave,
        'nombre': cfg.get('nombre', clave),
        'provincia': cfg.get('provincia', ''),
        'ficha': cfg['ficha'],
        'generado': datos['generado'],
        'datos': datos,
        'historia': historia(ctx['limpio']),
        'resumen': resumen(datos, ctx),
    }
    ruta = D.escribir_cache(clave, WORKSPACE, contenido)
    print('cache: %s  (%.0f s)' % (ruta, time.time() - t0), flush=True)
    return 'ok'


# ------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description='Actualizacion diaria de la app: portal SGIA, limpieza y pronostico.')
    p.add_argument('embalses', nargs='*', help='claves de embalse (por defecto los 13)')
    p.add_argument('--sin-portal', action='store_true',
                   help='no descargar ni limpiar: pronosticar con la serie limpia que ya hay')
    p.add_argument('--sin-descarga', action='store_true',
                   help='no descargar GEOGLOWS: usar los pronosticos ya guardados')
    p.add_argument('--carpeta', default=DATOS,
                   help='carpeta Operative_Forecast (por defecto la de la app)')
    p.add_argument('--emision', default=None, help='AAAA-MM-DD: un pronostico pasado (pruebas)')
    p.add_argument('--hasta', default=None, help='AAAA-MM-DD: serie limpia solo hasta esa fecha (pruebas)')
    args = p.parse_args()

    gen, cfgs = D.leer_config()
    claves = args.embalses or list(cfgs)
    for c in claves:
        if c not in cfgs:
            raise SystemExit("'%s' no esta en la configuracion. Embalses: %s" % (c, ', '.join(cfgs)))

    print('Datos: %s' % os.path.abspath(args.carpeta), flush=True)
    if not args.sin_portal:
        if os.path.abspath(args.carpeta) != os.path.abspath(DATOS):
            raise SystemExit('Con --carpeta distinta a la de la app hay que usar --sin-portal.')
        if not descargar_portal(claves, cfgs):
            print('\n! la descarga del portal fallo en algun embalse; se sigue con lo que haya', flush=True)
        if not limpiar(claves):
            print('\n! la limpieza fallo; se pronostica con la serie limpia anterior', flush=True)

    estado = {}
    for c in claves:
        try:
            estado[c] = pronosticar(c, gen, cfgs[c], args.carpeta, args.sin_descarga, args.emision, args.hasta)
        except Exception as e:      # un embalse con problemas no detiene a los demas
            estado[c] = 'ERROR %s: %s' % (type(e).__name__, e)
    print()
    for c, st in estado.items():
        print(('  [OK]    ' if st == 'ok' else '  [FALLO] ') + c + ('' if st == 'ok' else '  ' + st))
    ok = sum(1 for s in estado.values() if s == 'ok')
    print('Listo: %d de %d embalses. Cache en %s' % (ok, len(estado), D.carpeta_cache(WORKSPACE)))
    sys.exit(0 if ok == len(estado) else 1)


if __name__ == '__main__':
    main()

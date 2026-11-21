"""Configuracion de los embalses y lectura/escritura del cache diario.

No depende de Django: lo usan los controladores de la app y el script diario
actualizar_embalses.py. El cache tiene un JSON por embalse en
<app_workspace>/cache/<Embalse>.json, con:

    datos     lo mismo que usa la pagina de pronosticar.py (caudales de los 52
              miembros en las tres versiones, curva, aliviadero, salidas base...)
    historia  la serie limpia diaria completa (para la pestana Historico)
    resumen   el resultado con las salidas por defecto (para el mapa y la lista)
"""

import json
import os

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(AQUI, 'embalses_pronostico.json')


def leer_config():
    with open(RUTA_CONFIG, encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg['general'], cfg['embalses']


def embalses():
    """Claves de los embalses en el orden del archivo de configuracion."""
    return list(leer_config()[1])


def ficha(clave):
    return leer_config()[1].get(clave)


def a_url(clave):
    """'La Yaya _GTM_' -> 'La_Yaya__GTM_' (los espacios no viajan bien en la URL)."""
    return clave.replace(' ', '_')


def de_url(texto):
    """Inverso de a_url, comprobado contra la configuracion."""
    for clave in embalses():
        if a_url(clave) == texto:
            return clave
    return None


def carpeta_cache(workspace):
    return os.path.join(workspace, 'cache')


def leer_cache(clave, workspace):
    ruta = os.path.join(carpeta_cache(workspace), clave + '.json')
    if not os.path.exists(ruta):
        return None
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def escribir_cache(clave, workspace, contenido):
    os.makedirs(carpeta_cache(workspace), exist_ok=True)
    ruta = os.path.join(carpeta_cache(workspace), clave + '.json')
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(contenido, f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    os.replace(tmp, ruta)      # la app nunca lee un archivo a medio escribir
    return ruta

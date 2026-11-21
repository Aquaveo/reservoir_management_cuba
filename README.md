# Pronóstico de Embalses de Cuba

App de [Tethys Platform](https://www.tethysplatform.org/) 4 que muestra el nivel y el volumen
pronosticados a 14 días para los 13 embalses priorizados, con el pronóstico por ensamble de
[GEOGLOWS](https://geoglows.ecmwf.int/) corregido con los datos del portal SGIA.

Parte de la app de República Dominicana
([tethysapp-reservoir_management](https://github.com/Aquaveo/tethysapp-reservoir_management)),
pero el cálculo es el de los Colabs de Cuba y de `pronosticar.py`, no el de la app dominicana.

## Qué hace

- **Mapa y resumen**: los 13 embalses con su último dato del portal, el porcentaje del volumen
  autorizado y el nivel esperado en 14 días (versión corregida de sesgo y de condiciones
  iniciales, con salidas por persistencia).
- **Página de cada embalse**:
  - *Pronóstico*: la misma página de `pronosticar.py`. Se elige la versión (GEOGLOWS, corregido
    de sesgo, o corregido + condiciones iniciales), se editan entrega, desembalse y vertimiento
    día por día, el balance se recalcula en el navegador y se descarga el `.zip` con los cuatro
    CSV de cada versión, igual que el del Colab.
  - *Histórico*: la serie limpia completa (nivel, volumen y caudal de entrada) con las cotas de la
    ficha, y su descarga en CSV.

## Cómo funciona

Todos los datos viven dentro de la app, en `workspaces/app_workspace/Operative_Forecast/`, con la
misma estructura de carpetas del proyecto:

```
Operative_Forecast/
├── Raw_Data/<Embalse>/<Embalse>_portal_crudo.csv     lo que devuelve el portal SGIA
├── Clean_Data/<Embalse>/                             serie limpia, curva, correcciones, alertas
├── Clean_Data/<Embalse>_correcciones_manuales.csv    las correcciones revisadas a mano
└── Forecast_Data/GEOGLOWS/<Embalse>/                 pronósticos de GEOGLOWS ya descargados
```

La app trae los datos al 17-11-2026, ya limpios y con la revisión manual de noviembre de 2026.
`actualizar_embalses.py` los pone al día cada día, en tres pasos:

```
1. portal SGIA ─► scripts/descargar_portal_crudo.py ─► Raw_Data
2.               scripts/limpiar_portal.py           ─► Clean_Data
3. GEOGLOWS    ─► pronóstico                          ─► workspaces/app_workspace/cache/<Embalse>.json
```

El paso 3 hace todo el cálculo: curva nivel–volumen y ley del
aliviadero a partir de la serie limpia, descarga de GEOGLOWS, corrección de sesgo por mapeo de
cuantiles miembro por miembro, ajuste AR(1) de las condiciones iniciales y balance. Deja un JSON
por embalse en el cache. Las páginas solo leen ese cache: no descargan nada ni recalculan en el
servidor. El balance interactivo corre en el navegador (`public/pronostico/nucleo.js`) y da
exactamente los mismos números que Python y que los Colabs.

Las fichas de los embalses (tramos de GEOGLOWS, nivel muerto, aliviadero, coordenadas) están en
`tethysapp/reservoir_management_cuba/embalses_pronostico.json`.

### Sin dependencias externas en el navegador

Plotly y Leaflet van dentro de la app (`public/vendor/`), no se cargan de internet. El mapa base es
OpenStreetMap, sin componentes de Esri. Si ese servidor de teselas no está disponible, se cambia
en la configuración de la app (ajuste `url_teselas`) por otro, por ejemplo uno propio.

## Instalación

```bash
git clone <este repositorio>
cd tethysapp-reservoir_management_cuba
tethys install -d
```

## Actualización diaria

Con el Python del entorno de Tethys, desde la carpeta del repositorio:

```bash
python actualizar_embalses.py                  # los 13 embalses: portal, limpieza y pronóstico
python actualizar_embalses.py Zaza Mayari      # solo algunos
python actualizar_embalses.py --sin-portal     # solo el pronóstico, con la serie limpia que ya hay
```

La primera vez tarda varios minutos por embalse (descarga ~30 pronósticos pasados de GEOGLOWS para
el AR(1)); esos quedan guardados en `Forecast_Data/GEOGLOWS/` y los días siguientes solo se baja el
del día. Con `--sin-descarga` usa solo lo guardado.

Se puede programar con el Programador de tareas de Windows o con cron.

### Correcciones manuales

Van en `workspaces/app_workspace/Operative_Forecast/Clean_Data/<Embalse>_correcciones_manuales.csv`,
con el mismo formato de siempre (`fecha,campo,valor,motivo`). Después de cambiarlas hay que rehacer
la limpieza completa de ese embalse:

```bash
python tethysapp/reservoir_management_cuba/scripts/limpiar_portal.py ^
    tethysapp/reservoir_management_cuba/workspaces/app_workspace/Operative_Forecast/Raw_Data/Zaza/Zaza_portal_crudo.csv ^
    --carpeta tethysapp/reservoir_management_cuba/workspaces/app_workspace/Operative_Forecast/Clean_Data --rehacer
python actualizar_embalses.py Zaza --sin-portal
```

## Estructura

```
actualizar_embalses.py          actualización diaria: portal, limpieza y pronóstico (sin Django)
tethysapp/reservoir_management_cuba/
├── scripts/                    descargar_portal_crudo.py y limpiar_portal.py (copias de Scripts/)
├── app.py                      clase de la app y ajustes del mapa base
├── controllers.py              portada (mapa y lista) y página de cada embalse
├── datos.py                    configuración y cache
├── pronosticar.py              el cálculo (copia del script de Scripts/)
├── embalses_pronostico.json    fichas de los 13 embalses
├── public/
│   ├── pronostico/             nucleo.js, pagina.js, pagina.css (los mismos de pronosticar.py)
│   ├── js/                     mapa.js, embalse.js
│   ├── css/app.css
│   └── vendor/                 plotly.min.js, leaflet/
├── templates/reservoir_management_cuba/
├── tests/test_balance.py       python -m pytest tethysapp/reservoir_management_cuba/tests
└── workspaces/app_workspace/
    ├── Operative_Forecast/     datos del portal: crudos, limpios y correcciones manuales
    └── cache/                  generado cada día; no va al repositorio
```

## Pendiente

- `pronosticar.py`, `nucleo.js`, `pagina.js`, `pagina.css`, `descargar_portal_crudo.py` y
  `limpiar_portal.py` son copias de los de `Scripts/`. Si se cambia uno, hay que copiarlo al otro lado.
- No hay registro manual de niveles (la página *Reportar* de la app dominicana): los datos vienen
  del portal SGIA.

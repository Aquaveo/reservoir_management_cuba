from tethys_sdk.base import TethysAppBase
from tethys_sdk.app_settings import CustomSetting


class ReservoirManagementCuba(TethysAppBase):
    """Pronostico de nivel y volumen de los embalses de Cuba con GEOGLOWS."""

    name = 'Pronóstico de Embalses de Cuba'
    description = ('Nivel y volumen de los embalses a 14 días con el pronóstico por ensamble '
                   'de GEOGLOWS, corregido con los datos del portal SGIA.')
    package = 'reservoir_management_cuba'
    index = 'home'
    icon = f'{package}/images/icono.png'
    root_url = 'reservoir-management-cuba'
    color = '#0b6e8f'
    tags = 'GEOGLOWS, embalses, Cuba'
    enable_feedback = False
    feedback_emails = []

    def custom_settings(self):
        return (
            CustomSetting(
                name='url_teselas',
                type=CustomSetting.TYPE_STRING,
                description=('Plantilla de las teselas del mapa base (Leaflet). Por defecto OpenStreetMap. '
                             'Cambiar si ese servidor no esta disponible, p. ej. por un servidor propio.'),
                required=False,
                default='https://tile.openstreetmap.org/{z}/{x}/{y}.png',
            ),
            CustomSetting(
                name='atribucion_teselas',
                type=CustomSetting.TYPE_STRING,
                description='Texto de atribucion del mapa base.',
                required=False,
                default='&copy; colaboradores de OpenStreetMap',
            ),
        )

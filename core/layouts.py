"""Registre des layouts visuels de l'admin (thème client).

Le layout actif est choisi par ``settings.ALFAWAY_LAYOUT`` (variable d'env
``ALFAWAY_LAYOUT``, défaut ``generali``). Un nom inconnu retombe sur
``DEFAULT_LAYOUT`` avec un warning : jamais d'erreur au rendu.

Ajouter un layout client : une entrée dans ``LAYOUTS`` + ses CSS dans
``core/static/layouts/<nom>/`` (``admin.css``, ``login.css``), puis collectstatic.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_LAYOUT = 'generali'

LAYOUTS = {
    'generali': {
        'label': 'Generali',
        'admin_css': 'layouts/generali/admin.css',
        'login_css': 'layouts/generali/login.css',
        # TODO: logo officiel Generali à fournir (static/layouts/generali/logo.svg,
        # chemin relatif à STATIC_URL). Absent → marque textuelle « AlfaWay ».
        'logo': None,
    },
}


def get_layout(name=None):
    """Layout ``name`` (défaut : ``settings.ALFAWAY_LAYOUT``), avec sa clé ``name``."""
    if name is None:
        name = getattr(settings, 'ALFAWAY_LAYOUT', DEFAULT_LAYOUT)
    if name not in LAYOUTS:
        logger.warning('ALFAWAY_LAYOUT=%r inconnu, repli sur %r', name, DEFAULT_LAYOUT)
        name = DEFAULT_LAYOUT
    return {'name': name, **LAYOUTS[name]}

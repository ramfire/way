"""Étape **routing** du cycle de vie d'un fichier (§1.4, post-qualification).

Troisième producteur de l'axe contrôles, après l'admission et la qualification.
Une fois un fichier **qualifié** (une ``Nomenclature`` reconnaît son nom), le routing
**pose la clé de dispatch** : il lit la ``route`` portée par cette Nomenclature et
l'inscrit dans ``ReceivedFile.route``. C'est de l'**observation/classification
pure**, comme les stages amont :

  * **entrée** = la Nomenclature matchée, fournie **in-process** par la qualification
    (le routing ne relit PAS la base, ne re-parse PAS le filename) ;
  * **pas de matcher, pas de tokens, pas d'ambiguïté** : une Nomenclature pointe au
    plus une Route (``nomenclature.route``) ;
  * **AUCUNE** logique de load/parsing (déféré §1.5) : on ne fait que poser
    ``route_id``. Le gate ``route IS NOT NULL`` est pour le futur stage de load ;
  * **jamais sticky** : à chaque rejeu, ``route_id`` est recalculé (set) ou effacé
    (clear), et un Event **frais** est émis ;
  * mêmes garanties que l'amont : append-only (``Event``), **ne touche jamais**
    ``ReceivedFile.state``, **ne lève jamais** vers l'appelant (garde englobante).

Le moteur ne *reject* jamais ici non plus : route manquante ou inactive → ``recycle``
(retraitable — on configure la route puis on rejoue).
"""
import logging

from .models import Event, ReceivedFile

logger = logging.getLogger(__name__)

STAGE = 'routing'

# Nom de contrôle (stable : utilisé en lecture/board).
CTRL_ROUTE_RESOLUTION = 'route_resolution'

# Codes de cause normalisés (Event.cause_code).
CAUSE_ROUTE_NOT_CONFIGURED = 'route_not_configured'   # nomenclature sans route
CAUSE_ROUTE_INACTIVE = 'route_inactive'               # route présente mais désactivée


def _emit(rf, result, monitoring_class, detail, cause_code=None):
    """Append un ``Event`` de routing (audit). Hérite du ``sub_tenant`` du fichier."""
    return Event.objects.create(
        file=rf, stage=STAGE, control=CTRL_ROUTE_RESOLUTION, result=result,
        monitoring_class=monitoring_class, detail=detail,
        cause_code=cause_code, sub_tenant_id=rf.sub_tenant_id,
    )


def _set_route(rf, route_id):
    """Pose ou efface ``rf.route`` (jamais sticky). N'écrit que si la valeur change."""
    if rf.route_id != route_id:
        rf.route_id = route_id
        rf.save(update_fields=['route'])


def _resolve_route(rf, nomenclature):
    """Cœur du routing (peut lever ; encapsulé par ``resolve_route``).

    La Route vient de ``nomenclature.route``. Émet un Event frais à chaque appel ;
    (re)pose ou efface ``rf.route_id`` selon le résultat.
    """
    nom_id = nomenclature.pk
    route = nomenclature.route  # FK ; None si la Nomenclature ne porte pas de route

    if route is None:
        _set_route(rf, None)
        _emit(rf, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
              detail={'reason': CAUSE_ROUTE_NOT_CONFIGURED, 'nomenclature_id': nom_id},
              cause_code=CAUSE_ROUTE_NOT_CONFIGURED)
        logger.info('Routing NOT_CONFIGURED file=%s nomenclature=%s', rf.pk, nom_id)
        return None
    if not route.active:
        _set_route(rf, None)
        _emit(rf, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
              detail={'reason': CAUSE_ROUTE_INACTIVE, 'nomenclature_id': nom_id,
                      'route_id': route.pk, 'route_code': route.code},
              cause_code=CAUSE_ROUTE_INACTIVE)
        logger.info('Routing INACTIVE file=%s route=%s', rf.pk, route.code)
        return None

    _set_route(rf, route.pk)
    _emit(rf, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          detail={'route_code': route.code, 'route_id': route.pk, 'nomenclature_id': nom_id})
    logger.info('Routing RESOLVED file=%s route=%s nomenclature=%s',
                rf.pk, route.code, nom_id)
    return route.pk


def resolve_route(file_id, nomenclature):
    """Route un fichier (par id) via la ``nomenclature`` fournie par la qualification.

    **Sans** ``refresh_control_class`` : réservé au **chaînage** depuis l'admission,
    qui fait un unique refresh couvrant les trois stages. Peut lever (la garde
    non-bloquante est dans l'admission). Relit la ligne pour une écriture fraîche.
    """
    rf = ReceivedFile.objects.get(pk=file_id)
    return _resolve_route(rf, nomenclature)


def latest_routing_event(rf_or_id):
    """Dernier événement de stage ``routing`` d'un fichier (ou ``None``)."""
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE)
            .order_by('-created_at', '-id').first())

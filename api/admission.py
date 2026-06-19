"""Étape **admission** du cycle de vie d'un fichier (post-stockage S3).

Une fois un fichier écrit dans S3, AlfaWay décide si le flux est **reconnu** —
*pas* s'il faut accepter le transfert. SFTPGo a déjà authentifié la session et
reste **l'unique barrière** ; l'admission ne refuse JAMAIS un upload et ne bloque
RIEN. C'est de l'**observation/classification pure** : elle tourne *après* le
stockage, consigne son verdict sous forme d'**événements** (cf. ``Event``) et fait
remonter les problèmes au board de monitoring.

Garanties (cf. CLAUDE.md, invariants) :
  * ``file_admission(file_id)`` ne prend QUE l'id et relit tout depuis la ligne ;
  * elle est **rejouable** sans court-circuit (re-jouer = le mécanisme « recycle ») ;
  * elle **ne touche jamais** ``ReceivedFile.state`` (qui ne décrit que le stockage
    S3) : un rejet d'admission laisse ``state = stored`` ;
  * elle **ne lève jamais** vers l'appelant (garde try/except englobante).
"""
import logging

from django.conf import settings

from .models import Channel, Event, Partner, ReceivedFile, refresh_control_class

logger = logging.getLogger(__name__)

STAGE = 'admission'

# Noms de contrôles (stables : utilisés en lecture/board).
CTRL_PARTNER_RECOGNISED = 'partner_recognised'
CTRL_PARTNER_STATUS = 'partner_status'
CTRL_CHANNEL_AUTHORISED = 'channel_authorised'
CTRL_VERDICT = 'verdict'

# Verdicts (posés dans detail['verdict'] de l'événement final).
# NB: `quarantine` = flux non reconnu, gardé pour audit, NON retraité. Distinct de
# l'état de stockage `ReceivedFile.State.ARCHIVED` (axe stockage, bouton manuel) —
# voir docs/admission-monitoring-design.md §8 (collision « archive » levée).
VERDICT_ADMIS = 'admis'
VERDICT_RECYCLE = 'recycle'
VERDICT_QUARANTINE = 'quarantine'

# Version du référentiel/règles au moment de la décision (traçabilité).
REFERENTIAL_VERSION = 1


def _emit(rf, control, result, monitoring_class, detail=None):
    """Append un ``Event`` (audit). Aucun update/suppression : append-only.

    L'``Event`` hérite du ``sub_tenant`` du fichier (NOT NULL) : pour un fichier
    résolu c'est le tenant du canal, sinon le tenant d'ingest par défaut.
    """
    return Event.objects.create(
        file=rf, stage=STAGE, control=control, result=result,
        monitoring_class=monitoring_class, detail=detail or {},
        sub_tenant_id=rf.sub_tenant_id,
    )


def _channel_authorised(rf):
    """Canal/chemin autorisé pour ce flux (minimal, piloté par la config).

    ``settings.ADMISSION_PATH_RULES`` = ``{identifier: [prefixes autorisés]}``
    (``identifier`` = ``username`` SFTPGo = identifiant du canal). **Absence de
    règle ⇒ autorisé** (admission = observation, on ne bloque pas par défaut). PAS
    de routage ici : simple préfixe de chemin.
    """
    rules = getattr(settings, 'ADMISSION_PATH_RULES', {}) or {}
    allowed = rules.get(rf.username)
    if not allowed:
        return True, None
    key = (rf.s3_key or rf.path or '').lstrip('/')
    ok = any(key.startswith(pfx.lstrip('/')) for pfx in allowed)
    return ok, {'path': key, 'allowed_prefixes': list(allowed)}


def _is_first_admis(username):
    """True s'il n'existe encore AUCUN événement ``admis`` pour ce partenaire.

    Dérivé des événements (pas d'état) : la milestone d'initialisation est posée
    sur le premier ``admis`` jamais émis pour les fichiers de ce ``username``.
    Vérifié AVANT d'émettre le verdict courant.
    """
    return not Event.objects.filter(
        stage=STAGE, control=CTRL_VERDICT,
        file__username=username, detail__verdict=VERDICT_ADMIS,
    ).exists()


def _ref(extra=None):
    """Snapshot référentiel à consigner dans ``detail`` (version des règles)."""
    base = {'referential_version': REFERENTIAL_VERSION}
    if extra:
        base.update(extra)
    return base


def _admit(rf, partner):
    """Verdict **admis** : partenaire mappé, actif, canal autorisé."""
    first = _is_first_admis(rf.username)
    _emit(rf, CTRL_VERDICT, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          detail=_ref({'verdict': VERDICT_ADMIS, 'first': first,
                       'username': rf.username}))
    logger.info('Admission ADMIS file=%s user=%s first=%s',
                rf.pk, rf.username, first)
    return VERDICT_ADMIS


def _recycle(rf, reason, extra=None):
    """Verdict **recycle** : corrigeable en interne, sera **retraité**.

    Résolution : un humain enrôle/déclare au référentiel, puis re-appelle
    ``file_admission(file_id)`` — qui admet alors. ``state`` inchangé.
    """
    detail = _ref({'verdict': VERDICT_RECYCLE, 'reason': reason,
                   'username': rf.username})
    if extra:
        detail.update(extra)
    _emit(rf, CTRL_VERDICT, Event.Result.FAILED,
          Event.MonitoringClass.RECYCLE, detail=detail)
    logger.info('Admission RECYCLE file=%s user=%s reason=%s',
                rf.pk, rf.username, reason)
    return VERDICT_RECYCLE


def _quarantine(rf, reason, extra=None):
    """Verdict **quarantine** : conservé pour audit, **non retraité**.

    On ne supprime JAMAIS l'objet S3 ; ``state`` reste inchangé (stored).
    """
    detail = _ref({'verdict': VERDICT_QUARANTINE, 'reason': reason,
                   'username': rf.username})
    if extra:
        detail.update(extra)
    _emit(rf, CTRL_VERDICT, Event.Result.FAILED,
          Event.MonitoringClass.REJECT, detail=detail)
    logger.info('Admission QUARANTINE file=%s user=%s reason=%s',
                rf.pk, rf.username, reason)
    return VERDICT_QUARANTINE


def _run(file_id):
    """Cœur de l'admission (peut lever ; encapsulé par ``file_admission``)."""
    rf = ReceivedFile.objects.get(pk=file_id)
    username = rf.username

    # Contrôle 1 — flux reconnu : un canal SFTP porte-t-il cet ``identifier`` ?
    # La résolution part du seul identifier (unicité globale (kind, identifier)) ;
    # le partenaire et le locataire en découlent. On pose les caches sur la ligne.
    channel = (Channel.objects
               .filter(kind=Channel.Kind.SFTP, identifier=username)
               .select_related('partner').first())
    partner = channel.partner if channel else None
    if channel is None or partner is None:
        # Modèle discovery : compte non mappé → recycle / en attente d'un humain.
        # On ne crée JAMAIS Channel ni Partner automatiquement. Caches laissés NULL.
        _emit(rf, CTRL_PARTNER_RECOGNISED, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': 'partner_not_mapped', 'username': username}))
        return _recycle(rf, 'partner_not_mapped')
    # Caches de résolution + re-pointage du locataire vers celui du canal résolu
    # (re-câblés à chaque rejeu : le mapping a pu changer). Le fichier passe du
    # tenant d'ingest par défaut au tenant réel du partenaire.
    if (rf.channel_id != channel.id or rf.partner_id != partner.id
            or rf.sub_tenant_id != channel.sub_tenant_id):
        rf.channel_id = channel.id
        rf.partner_id = partner.id
        rf.sub_tenant_id = channel.sub_tenant_id
        rf.save(update_fields=['channel', 'partner', 'sub_tenant'])
    _emit(rf, CTRL_PARTNER_RECOGNISED, Event.Result.PASSED,
          Event.MonitoringClass.PUSH, detail=_ref({'username': username}))

    # Contrôle 2 — statut du partenaire (active / revoked).
    if partner.status == Partner.Status.REVOKED:
        # Partenaire révoqué qui émet encore → la suspension SFTP n'est pas
        # effective (ou les creds n'ont jamais été coupés). AlfaWay ne suspend
        # rien lui-même : on **alerte** (action ops requise) puis on met en quarantaine.
        _emit(rf, CTRL_PARTNER_STATUS, Event.Result.FAILED,
              Event.MonitoringClass.WARNING_ACTION,
              detail=_ref({'reason': 'revoked_partner_still_emitting',
                           'partner_status': partner.status,
                           'username': username}))
        return _quarantine(rf, 'partner_revoked',
                           extra={'partner_status': partner.status})
    _emit(rf, CTRL_PARTNER_STATUS, Event.Result.PASSED,
          Event.MonitoringClass.PUSH,
          detail=_ref({'partner_status': partner.status}))

    # Contrôle 3 — canal/chemin autorisé pour ce flux (minimal, no routing).
    ok, chan_detail = _channel_authorised(rf)
    if not ok:
        _emit(rf, CTRL_CHANNEL_AUTHORISED, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': 'channel_not_authorised', **(chan_detail or {})}))
        return _recycle(rf, 'channel_not_authorised', extra=chan_detail)
    _emit(rf, CTRL_CHANNEL_AUTHORISED, Event.Result.PASSED,
          Event.MonitoringClass.PUSH, detail=_ref(chan_detail))

    # Tout est passé → admis.
    return _admit(rf, partner)


def file_admission(file_id):
    """Lance l'admission d'un fichier (par son id) et renvoie le verdict.

    **Indépendante** (id seul), **rejouable** (aucun court-circuit sur l'existence
    d'événements : re-jouer est précisément le mécanisme « recycle »), et **ne lève
    jamais** vers l'appelant (garde englobante : on log et on avale). Renvoie le
    verdict (``admis`` / ``recycle`` / ``quarantine``) ou ``None`` en cas d'erreur.
    """
    try:
        verdict = _run(file_id)
        # Chaînage qualification : seulement si admis (canal/partenaire résolus).
        # Garde dédiée → un échec de qualification n'affecte JAMAIS le verdict
        # d'admission qu'on renvoie. Le refresh ci-dessous couvre les deux stages.
        if verdict == VERDICT_ADMIS:
            _run_qualification(file_id)
        # Rematérialise le rollup worst-wins de l'axe contrôles pour ce fichier
        # (read-model du board), tous stages confondus. Source de vérité = les Event.
        refresh_control_class([file_id])
        return verdict
    except Exception:
        logger.exception('Admission: erreur inattendue pour file %s', file_id)
        return None


def _run_qualification(file_id):
    """Chaîne la qualification, garantie non bloquante (log + avale toute erreur).

    Le ``refresh_control_class`` est laissé à l'appelant (admission) pour n'en faire
    qu'un seul, couvrant admission + qualification.
    """
    try:
        from .qualification import qualify_no_refresh
        qualify_no_refresh(file_id)
    except Exception:
        logger.exception('Qualification (chaînée): échec non bloquant file %s', file_id)


def latest_admission_event(rf_or_id):
    """Dernier événement de stage ``admission`` d'un fichier (ou ``None``).

    L'« état d'admission » courant d'un fichier EST son dernier événement
    d'admission (verdict). Utilitaire de lecture (board, tests).
    """
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE)
            .order_by('-created_at', '-id').first())

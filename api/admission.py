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
import csv
import io
import logging
from datetime import date, datetime

from django.conf import settings

from .models import (
    Channel, Event, Feed, IdentificationRule, Partner, ReceivedFile,
    Referential, ReferentialEntry, SubFund, SubFundAlias,
    recompute_identification_partitions, refresh_control_class, run_scope,
)
from .s3 import get_s3_client

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
        # Une seule passe (``run_id``) couvre les quatre stages chaînés : tous leurs
        # Event partagent le run, et le rollup ne retiendra que cette passe par stage.
        with run_scope():
            verdict = _run(file_id)
            # Chaînage qualification : seulement si admis (canal/partenaire résolus).
            # Garde dédiée → un échec de qualification n'affecte JAMAIS le verdict
            # d'admission qu'on renvoie. Puis, court-circuit : routing PUIS parsing
            # (§1.5) ne tournent QUE si la qualification a passé (qualified) — ils
            # consomment la Feed matchée (in-process). Le parsing est chaîné **en
            # direct après le verdict routing** (il décode selon la Feed,
            # indépendamment de la route). Le refresh unique couvre les quatre stages.
            if verdict == VERDICT_ADMIS:
                qual_verdict, feed = _run_qualification(file_id)
                from .qualification import VERDICT_QUALIFIED
                if qual_verdict == VERDICT_QUALIFIED:
                    _run_routing(file_id, feed)
                    _run_parsing(file_id, feed)
            # Rematérialise le rollup worst-wins de l'axe contrôles pour ce fichier
            # (read-model du board), tous stages confondus. Vérité = les Event.
            refresh_control_class([file_id])
        return verdict
    except Exception:
        logger.exception('Admission: erreur inattendue pour file %s', file_id)
        return None


def _run_qualification(file_id):
    """Chaîne la qualification, garantie non bloquante (log + avale toute erreur).

    Renvoie ``(verdict, feed)`` — ``(None, None)`` en cas d'échec non
    bloquant. Le ``refresh_control_class`` est laissé à l'appelant (admission) pour
    n'en faire qu'un seul, couvrant les trois stages.
    """
    try:
        from .qualification import qualify_no_refresh
        return qualify_no_refresh(file_id)
    except Exception:
        logger.exception('Qualification (chaînée): échec non bloquant file %s', file_id)
        return None, None


def _run_routing(file_id, feed):
    """Chaîne le routing, garantie non bloquante (log + avale toute erreur).

    Comme la qualification, le ``refresh_control_class`` est laissé à l'appelant.
    """
    try:
        from .routing import resolve_route
        resolve_route(file_id, feed)
    except Exception:
        logger.exception('Routing (chaîné): échec non bloquant file %s', file_id)


def _run_parsing(file_id, feed):
    """Chaîne le parsing (§1.5) après le routing, garantie non bloquante.

    Décode le fichier selon la Feed matchée (in-process). Comme l'amont, le
    ``refresh_control_class`` est laissé à l'appelant (admission). C'est le premier
    stage chaîné qui **lit le contenu** S3 ; sa garde englobante isole tout échec.
    """
    try:
        from .parsing import parse_file_no_refresh
        parse_file_no_refresh(file_id, feed)
    except Exception:
        logger.exception('Parsing (chaîné): échec non bloquant file %s', file_id)


def latest_admission_event(rf_or_id):
    """Dernier événement de stage ``admission`` d'un fichier (ou ``None``).

    L'« état d'admission » courant d'un fichier EST son dernier événement
    d'admission (verdict). Utilitaire de lecture (board, tests).
    """
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE)
            .order_by('-created_at', '-id').first())


# ===========================================================================
# §1.6-b — Moteur d'**identification** (file_identification)
# ===========================================================================
# Fonction NOUVELLE et SÉPARÉE, co-localisée avec ``file_admission`` (décision
# §1.6-b). Elle **ne modifie pas** ``file_admission`` ni ``ReceivedFile.state``.
# Mêmes invariants que l'amont : rejouable, append-only (``Event``), **ne lève
# JAMAIS** vers l'appelant, ``refresh_control_class`` en ``finally``.
#
# Le moteur consomme le descripteur ``IdentificationProfile``/``IdentificationRule``
# (§1.6-a-bis) porté par la Feed et résout, par champ, l'EXISTENCE de l'entité
# contre son ``Referential`` (policy ``candidate``/``anomaly``). Il n'introduit ni
# ``blocking`` ni ``reject`` : tout écart est un signal Steward (``warning_action``).
# **Calendar-free** : ``valuation_date`` (axis) n'est jamais confrontée à un
# calendrier ni à une deadline — seule sa cohérence intra-record (parsabilité) est
# vérifiée. Source des records = stub ``_extract_records`` (parsing §1.5 différé).

STAGE_IDENTIFICATION = 'identification'

# Noms de contrôles « globaux fichier » (non scopés partition ; board §1.6-c).
CTRL_PROFILE_RESOLUTION = 'profile_resolution'
CTRL_EXTRACTION = 'extraction'


def _emit_identification(rf, control, result, mclass, detail):
    """Append un ``Event`` de stage ``identification`` (audit, append-only).

    Wrapper distinct du ``_emit`` d'admission (même module) ; réutilise les noms
    RÉELS du modèle ``Event`` et hérite du ``sub_tenant`` du fichier (NOT NULL).
    """
    return Event.objects.create(
        file=rf, stage=STAGE_IDENTIFICATION, control=(control or '')[:64],
        result=result, monitoring_class=mclass, detail=detail or {},
        sub_tenant_id=rf.sub_tenant_id,
    )


def _resolve_feed(rf):
    """Feed résolue du fichier, ou ``None`` s'il n'est pas actuellement ``qualified``.

    La ``Route`` posée sur ``rf.route_id`` est transverse (partagée par N Feeds) :
    elle ne ramène pas une Feed unique. La Feed faisant autorité est celle que la
    qualification a sélectionnée — on la relit via ``detail['feed_id']`` du dernier
    Event de qualification ``qualified`` (même mécanisme que le parsing §1.5)."""
    from .qualification import VERDICT_QUALIFIED, latest_qualification_event
    ev = latest_qualification_event(rf)
    if ev is None or ev.detail.get('verdict') != VERDICT_QUALIFIED:
        return None
    feed_id = ev.detail.get('feed_id')
    return Feed.objects.filter(pk=feed_id).first() if feed_id else None


def _read_s3_object(rf):
    """Bytes de l'objet S3 via la clé **physique** ``rf.path`` (repli ``s3_key``).

    Piège récurrent : la clé S3 réelle est le chemin **physique** (``path``), pas
    ``s3_key`` (virtual_path). Réutilise le client de ``api/s3.py``. Peut lever :
    l'appelant (``_extract_records``) avale et renvoie ``[]``."""
    bucket = rf.bucket or settings.SCW_BUCKET_PREFIX
    key = rf.path or (rf.s3_key or '').lstrip('/')
    obj = get_s3_client().get_object(Bucket=bucket, Key=key)
    return obj['Body'].read()


def _extract_records(rf):
    """Décode le fichier S3 en ``list[dict]`` aliasés selon ``feed.layout['columns']``.

    Convention de structure portée par le JSONB ``layout`` (AUCUN nouveau modèle) :
    chaque colonne ``{by, name|index, as}`` mappe une position/un en-tête physique
    vers un alias **logique** (``as``). Le moteur d'identification ne lit ensuite
    que les alias (``record['sub_fund']``), jamais le nom physique.

    Cas supporté : **tabulaire délimité** (CSV). XML/fixed-width/multi-record-types
    restent hors scope (parsing complet §1.5). Multi-lignes correct dès maintenant ;
    le group-by partition est §1.6-b2b (la boucle moteur verra plus de records, sans
    grouper). **Ne lève jamais** : layout absent/mal formé/colonne introuvable →
    ``[]`` + log, jamais d'exception propagée."""
    try:
        feed = _resolve_feed(rf)
        if feed is None or not feed.layout:
            return []
        layout = feed.layout
        cols = layout.get('columns') or []
        if not cols:
            return []
        delimiter = layout.get('delimiter', ',')
        has_header = layout.get('has_header', True)
        encoding = layout.get('encoding', 'utf-8')

        raw_bytes = _read_s3_object(rf)
        text = raw_bytes.decode(encoding, errors='replace')

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            return []

        header = rows[0] if has_header else None
        data_rows = rows[1:] if has_header else rows

        records = []
        for row in data_rows:
            rec = {}
            for c in cols:
                alias = c.get('as')
                if not alias:
                    continue
                if c.get('by') == 'header' and header is not None:
                    name = c.get('name')
                    if name in header:
                        idx = header.index(name)
                        rec[alias] = row[idx] if idx < len(row) else None
                    else:
                        rec[alias] = None
                elif c.get('by') == 'position':
                    idx = c.get('index')
                    rec[alias] = (row[idx] if (idx is not None and idx < len(row))
                                  else None)
            records.append(rec)
        return records
    except Exception:
        logger.exception('_extract_records %s', getattr(rf, 'pk', '?'))
        return []


def _is_parsable_date(value):
    """Cohérence intra-record d'un axis (ex. valuation_date) : date plausible ?

    **Calendar-free** : on ne confronte la valeur à AUCUN calendrier/jour ouvré —
    on vérifie seulement qu'elle est parsable comme une date (forme/plausibilité)."""
    if value is None:
        return False
    if isinstance(value, (date, datetime)):
        return True
    s = str(value).strip()
    if not s:
        return False
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def _entity_exists(referential, value, rf):
    """EXISTENCE de la valeur dans le référentiel visé (active uniquement).

    Le pivot ``subfund`` a sa table dédiée (``SubFund``, scopée locataire) ; les
    référentiels subordonnés vivent dans ``ReferentialEntry`` (scopés par le
    ``referential`` lui-même, déjà rattaché à un locataire)."""
    key = str(value)
    if referential.code == 'subfund':
        return SubFund.objects.filter(
            sub_tenant_id=rf.sub_tenant_id, key=key,
            status=SubFund.Status.ACTIVE).exists()
    return ReferentialEntry.objects.filter(
        referential=referential, key=key,
        status=ReferentialEntry.Status.ACTIVE).exists()


def _ident_control(field, value, pkey):
    """Bucket de rollup **scopé** ``(champ, partition canonique, valeur)``.

    ``current_control_rollup`` ne retient qu'UN Event par ``(file, stage, control)``
    (le plus récent). Un control = ``field`` seul ferait que des verdicts de
    partitions/valeurs différentes se **masquent** (seul le dernier émis compterait
    au worst-wins) — la classe fichier ne refléterait plus « au moins une partition
    douteuse ». En scopant le control par la partition ET la valeur, chaque check
    distinct est un bucket indépendant : worst-wins agrège toutes les partitions, et
    un rejeu (même donnée → mêmes controls) supersède proprement, partition par
    partition. Tronqué à 64 par ``_emit_identification`` (codes/ISIN/dates courts).

    §1.6-b-bis : la partition est ancrée sur le **SubFund canonique** (``sub_fund_id``)
    quand il est résolu ; sinon sur la valeur brute (``raw:<code>``) — deux inconnues
    distinctes ne fusionnent pas, et deux alias d'un même canonique partagent bien
    le même bucket."""
    part = pkey.get('sub_fund_id')
    if part is None:
        part = f"raw:{pkey.get('sub_fund_value')}"
    return f"{field}@{part}|{pkey.get('valuation_date')}#{value}"


def _resolve_existence(rf, rule, value, pkey):
    """Résout l'EXISTENCE d'une ``value`` (partition sub_fund ou subordonné) dans son
    référentiel, et émet un Event portant ``partition_key=pkey``.

    Sémantique INCHANGÉE vs §1.6-b (existence + ``absence_policy``) ; seul change le
    fait qu'on opère sur une valeur unique scopée à une partition."""
    control = _ident_control(rule.field, value, pkey)
    base = {'field': rule.field, 'role': rule.role, 'partition_key': pkey}

    if value in (None, ''):
        if rule.required:
            _emit_identification(
                rf, control, Event.Result.FAILED,
                Event.MonitoringClass.WARNING_ACTION,
                {**base, 'reason': 'missing_required_field'})
        else:
            _emit_identification(
                rf, control, Event.Result.PASSED,
                Event.MonitoringClass.WARNING_NOACTION,
                {**base, 'reason': 'optional_absent'})
        return

    referential = rule.referential
    if referential is None:
        # partition/subordinate sans référentiel = trou de config (Steward).
        _emit_identification(
            rf, control, Event.Result.FAILED,
            Event.MonitoringClass.WARNING_ACTION,
            {**base, 'value': value, 'reason': 'no_referential_for_rule'})
        return

    if _entity_exists(referential, value, rf):
        _emit_identification(
            rf, control, Event.Result.PASSED, Event.MonitoringClass.PUSH,
            {**base, 'value': value, 'referential': referential.code})
        return

    # Absente → la policy du référentiel distingue onboarding vs anomalie.
    # Les deux sont warning_action ; la nuance vit dans ``reason`` (board §1.6-c).
    if referential.absence_policy == Referential.AbsencePolicy.CANDIDATE:
        reason = 'new_entity_candidate'
    else:
        reason = 'unknown_entity'
    _emit_identification(
        rf, control, Event.Result.FAILED, Event.MonitoringClass.WARNING_ACTION,
        {**base, 'value': value, 'referential': referential.code, 'reason': reason})


def _declared_date_format(feed, field):
    """Format de date déclaré pour l'alias ``field`` dans le ``column_contracts`` du
    layout (§1.5+ : ``{as, type:'date', format}``), ou ``None``.

    Permet à l'axe §1.6 de valider la date avec **le même format que le parsing**
    (déclaré une seule fois), au lieu d'une liste fixe de formats numériques/ISO."""
    layout = feed.layout if isinstance(getattr(feed, 'layout', None), dict) else {}
    for c in (layout.get('column_contracts') or []):
        if c.get('as') == field and c.get('type') == 'date':
            return c.get('format')
    return None


def _resolve_axis(rf, rule, value, pkey, feed):
    """Cohérence intra-record d'un axis (ex. ``valuation_date``) — **aucun**
    référentiel, **calendar-free**. Émet un Event portant ``partition_key=pkey``.

    §1.6-c+ : si le ``column_contracts`` du feed déclare un ``format`` de date pour ce
    champ (§1.5+), on valide la valeur **avec ce format** (réutilise la logique de
    parsing) ; sinon repli sur la liste fixe ``_is_parsable_date`` (rétro-compat).
    Absence d'un axis requis traitée comme un champ manquant (sémantique §1.6-b)."""
    control = _ident_control(rule.field, value, pkey)
    base = {'field': rule.field, 'role': rule.role, 'partition_key': pkey}

    if value in (None, ''):
        if rule.required:
            _emit_identification(
                rf, control, Event.Result.FAILED,
                Event.MonitoringClass.WARNING_ACTION,
                {**base, 'reason': 'missing_required_field'})
        else:
            _emit_identification(
                rf, control, Event.Result.PASSED,
                Event.MonitoringClass.WARNING_NOACTION,
                {**base, 'reason': 'optional_absent'})
        return

    fmt = _declared_date_format(feed, rule.field)
    if fmt:
        from .parsing import _value_matches_type   # même validation que le parsing §1.5+
        ok = _value_matches_type(str(value).strip(), 'date', fmt)
    else:
        ok = _is_parsable_date(value)

    if ok:
        _emit_identification(
            rf, control, Event.Result.PASSED, Event.MonitoringClass.PUSH,
            {**base, 'value': value})
    else:
        detail = {**base, 'value': value, 'reason': 'unparsable_axis'}
        if fmt:
            detail['expected_format'] = fmt
        _emit_identification(
            rf, control, Event.Result.FAILED, Event.MonitoringClass.WARNING_ACTION,
            detail)


def _resolve_subfund_id(raw_value, feed, sub_tenant_id):
    """Résout une valeur brute de ``sub_fund`` vers l'id du ``SubFund`` canonique
    **actif** (§1.6-b-bis). Ordre : (a) **alias** ``SubFundAlias`` scopé au ``feed``
    (le répertoire porte le système de codes), (b) **repli** sur ``SubFund.key`` (un
    feed peut livrer le code interne directement). ``None`` si ni l'un ni l'autre →
    l'appelant tranche via l'``absence_policy``. **Détecte, ne crée JAMAIS.** Une
    résolution par valeur DISTINCTE (pas par record)."""
    if raw_value in (None, ''):
        return None
    key = str(raw_value)
    alias_sf = (SubFundAlias.objects
                .filter(sub_tenant_id=sub_tenant_id, feed=feed, external_code=key,
                        sub_fund__status=SubFund.Status.ACTIVE)
                .values_list('sub_fund_id', flat=True).first())
    if alias_sf is not None:
        return alias_sf
    return (SubFund.objects
            .filter(sub_tenant_id=sub_tenant_id, key=key,
                    status=SubFund.Status.ACTIVE)
            .values_list('id', flat=True).first())


def _resolve_partition(rf, rule, raw_value, sub_fund_id, pkey, feed):
    """Verdict de la rule ``partition`` (sub_fund) sur l'identité **canonique**
    pré-résolue (alias→canonique, cf. ``_resolve_subfund_id``) — §1.6-b-bis.

    ``present`` si résolu ; sinon ``candidate``/``anomaly`` selon l'``absence_policy``
    (INCHANGÉE). **Détecte, ne crée jamais** : sur non-résolu, le ``detail`` porte
    ``external_code`` (brut) + ``feed_id`` pour que le Steward tranche (rattacher
    l'alias à un SubFund existant **ou** créer SubFund + alias), via rejeu."""
    control = _ident_control(rule.field, raw_value, pkey)
    base = {'field': rule.field, 'role': rule.role, 'partition_key': pkey}

    if raw_value in (None, ''):
        if rule.required:
            _emit_identification(
                rf, control, Event.Result.FAILED,
                Event.MonitoringClass.WARNING_ACTION,
                {**base, 'reason': 'missing_required_field'})
        else:
            _emit_identification(
                rf, control, Event.Result.PASSED,
                Event.MonitoringClass.WARNING_NOACTION,
                {**base, 'reason': 'optional_absent'})
        return

    if sub_fund_id is not None:
        _emit_identification(
            rf, control, Event.Result.PASSED, Event.MonitoringClass.PUSH,
            {**base, 'value': raw_value, 'sub_fund_id': sub_fund_id,
             'referential': rule.referential.code if rule.referential else 'subfund'})
        return

    referential = rule.referential
    if referential is None:
        # partition sans référentiel = trou de config (Steward) — parité avec
        # ``_resolve_existence`` (on ne peut pas trancher candidate/anomaly).
        _emit_identification(
            rf, control, Event.Result.FAILED, Event.MonitoringClass.WARNING_ACTION,
            {**base, 'value': raw_value, 'reason': 'no_referential_for_rule'})
        return
    # Non résolu (ni alias ni key) → onboarding vs anomalie selon la policy du pivot.
    if referential.absence_policy == Referential.AbsencePolicy.CANDIDATE:
        reason = 'new_entity_candidate'
    else:
        reason = 'unknown_entity'
    _emit_identification(
        rf, control, Event.Result.FAILED, Event.MonitoringClass.WARNING_ACTION,
        {**base, 'value': raw_value, 'external_code': raw_value, 'feed_id': feed.id,
         'referential': referential.code, 'reason': reason})


def file_identification(file_id):
    """Identifie un fichier (§1.6-b/b2b) : regroupe ses records par partition
    ``(sub_fund, valuation_date)`` et résout chaque groupe.

    Par groupe : existence du ``sub_fund`` (partition), cohérence des axis
    (calendar-free), existence des subordonnés par **valeur distincte** (dédoublonné).
    Chaque Event porte le ``partition_key`` de son groupe (projection §1.6-c). Un
    fichier de N compartiments × M dates → jusqu'à N×M partitions.

    **Autonome** (id seul, relit tout depuis la ligne), **rejouable**, append-only,
    et **ne lève JAMAIS** vers l'appelant (garde englobante) ; rematérialise
    ``control_class`` en ``finally``. Ne modifie ni ``state`` ni ``file_admission``.
    Chaînée après l'admission par l'**orchestrateur** webhook
    (``SFTPWebhookView._run_admission``, §1.6-b2a) lorsque le verdict est ``push`` ;
    aussi déclenchable à la demande via ``manage.py run_identification``.

    Passe propre (``run_scope``), distincte de celle de l'admission : ses Event
    partagent un ``run_id`` et le rollup ne retient que la dernière identification."""
    with run_scope():
        _run_identification(file_id)


def _run_identification(file_id):
    """Cœur de l'identification (cf. ``file_identification`` pour les invariants)."""
    try:
        rf = ReceivedFile.objects.get(pk=file_id)
    except ReceivedFile.DoesNotExist:
        logger.exception('file_identification: fichier %s introuvable', file_id)
        return
    try:
        feed = _resolve_feed(rf)
        if feed is None or feed.identification_profile is None:
            # Pas de profil applicable → rien à identifier ; on trace l'attente Steward.
            _emit_identification(
                rf, CTRL_PROFILE_RESOLUTION, Event.Result.FAILED,
                Event.MonitoringClass.WARNING_ACTION,
                {'reason': 'no_identification_profile',
                 'feed_resolved': feed is not None,
                 'partition_key': {'sub_fund_id': None, 'sub_fund_value': None,
                                   'valuation_date': None,
                                   'feed_id': feed.id if feed else None}})
            return
        profile = feed.identification_profile
        rules = list(profile.rules.all())
        records = _extract_records(rf)

        if not records:
            # Aucune ligne décodée (layout absent, fichier vide, lecture S3 KO) :
            # rien à identifier — informationnel, non actionnable, mais tracé.
            _emit_identification(
                rf, CTRL_EXTRACTION, Event.Result.PASSED,
                Event.MonitoringClass.WARNING_NOACTION,
                {'reason': 'no_records',
                 'partition_key': {'sub_fund_id': None, 'sub_fund_value': None,
                                   'valuation_date': None, 'feed_id': feed.id}})
            return

        # Champs porteurs de la clé de partition (déduits des rules ; repli sur les
        # alias conventionnels si le profil ne déclare pas explicitement le rôle).
        part_rule = next(
            (r for r in rules if r.role == IdentificationRule.Role.PARTITION), None)
        axis_rules = [r for r in rules if r.role == IdentificationRule.Role.AXIS]
        sub_rules = [r for r in rules if r.role == IdentificationRule.Role.SUBORDINATE]
        part_field = part_rule.field if part_rule else 'sub_fund'
        # Axe principal de partition = le 1er axis (valuation_date). Les axis
        # supplémentaires restent de la cohérence intra-record, PAS des dimensions.
        axis_field = axis_rules[0].field if axis_rules else 'valuation_date'

        # --- §1.6-b-bis : pré-résolution alias→canonique ------------------------
        # Une résolution par valeur brute DISTINCTE de sub_fund (pas par record),
        # AVANT le grouping. Deux codes externes pointant le même SubFund → même id
        # canonique → une seule partition (collapse cross-provider, l'objectif).
        resolved = {raw: _resolve_subfund_id(raw, feed, rf.sub_tenant_id)
                    for raw in {rec.get(part_field) for rec in records}}

        # --- GROUP BY (SubFund CANONIQUE, valuation_date) -----------------------
        # Clé de groupe sur l'identité canonique ; une valeur non résolue forme sa
        # propre partition candidate (sentinelle ``('raw', brut)`` pour que deux
        # inconnues distinctes ne fusionnent pas).
        groups = {}   # gkey -> {'records', 'raw', 'sub_fund_id'}
        for rec in records:
            raw = rec.get(part_field)
            vd = rec.get(axis_field)
            sfid = resolved.get(raw)
            canon = ('id', sfid) if sfid is not None else ('raw', raw)
            g = groups.setdefault((canon, vd),
                                  {'records': [], 'raw': raw, 'sub_fund_id': sfid})
            g['records'].append(rec)

        for (_canon, vd), g in groups.items():
            grp = g['records']
            # partition_key ancré sur le CANONIQUE, IDENTIQUE sur tous les Events du
            # groupe (contrat consommé par §1.6-c). ``sub_fund_value`` (brut) toujours
            # présent (affichage/trace) ; ``sub_fund_id`` null si non résolu.
            pkey = {'sub_fund_id': g['sub_fund_id'], 'sub_fund_value': g['raw'],
                    'valuation_date': vd, 'feed_id': feed.id}
            # 1) partition : résolution alias→canonique (UNE fois par groupe).
            if part_rule:
                _resolve_partition(rf, part_rule, g['raw'], g['sub_fund_id'],
                                   pkey, feed)
            # 2) axis : cohérence intra-record de la/les date(s) (UNE fois par groupe).
            for ar in axis_rules:
                _resolve_axis(rf, ar, grp[0].get(ar.field), pkey, feed)
            # 3) subordonnés : par VALEUR DISTINCTE dans le groupe (dédoublonnage).
            #    INCHANGÉ — pas d'alias (réservé au pivot sub_fund, cf. exclusions).
            for sr in sub_rules:
                distinct_vals = {rec.get(sr.field) for rec in grp}
                for val in distinct_vals:
                    _resolve_existence(rf, sr, val, pkey)
    except Exception:
        logger.exception('file_identification: erreur inattendue pour file %s', file_id)
    finally:
        # Rollup worst-wins (read-model board IT), tous stages confondus. Toujours joué.
        refresh_control_class([rf.pk])
        # Projection par partition (read-model board métier §1.6-c), juste après le
        # rollup. Lit le même journal Event ; ne lève jamais (englobée).
        recompute_identification_partitions(rf)


def latest_identification_event(rf_or_id):
    """Dernier événement de stage ``identification`` d'un fichier (ou ``None``)."""
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE_IDENTIFICATION)
            .order_by('-created_at', '-id').first())

"""Étape **qualification** du cycle de vie d'un fichier (post-admission).

Deuxième producteur de l'axe contrôles après l'admission. Une fois un fichier
**admis** (flux reconnu, partenaire actif, canal autorisé), la qualification
**sélectionne la Nomenclature** dont la grammaire reconnaît le nom du fichier. La
Nomenclature est le contrat de nommage **fin** (ex. ``POS*.csv``) et porte sa
``route`` (consommée au stage routing §1.4). C'est de l'**observation/
classification pure** :

  * **par fichier** : sélecteur ``subfolder = dirname(s3_key)`` ; parmi les N
    Nomenclatures du (canal, sous-dossier), on retient **celle dont la grammaire
    matche** ``basename(s3_key)`` ;
  * **chaînée** après un verdict admission ``admis`` (donc ``rf.channel`` résolu),
    et **rejouable** ; expose la Nomenclature matchée **in-process** au routing ;
  * mêmes garanties que l'admission : append-only (``Event``), **ne touche jamais**
    ``ReceivedFile.state``, **ne lève jamais** vers l'appelant.

**Le moteur ne *reject* jamais** (décision 2026-06-22) : tout ce qui n'est pas
``qualified`` est ``recycle`` (retraitable). Le ``reject`` est une **décision
humaine** (triage opérateur). Verdicts : sous-dossier non enrôlé → ``recycle``
(``nomenclature_not_found``) ; nom ne matche aucune Nomenclature → ``recycle``
(``nomenclature_no_match``) ; ≥2 Nomenclatures ex æquo → ``recycle``
(``ambiguous_nomenclature_config``) ; regex de config invalide → ``recycle``
(``grammar_invalid``) ; sinon → ``qualified`` (``push``).
"""
import logging
import posixpath
import re

from .models import Event, Nomenclature, ReceivedFile, refresh_control_class

logger = logging.getLogger(__name__)

STAGE = 'qualification'

# Noms de contrôles (stables : utilisés en lecture/board).
CTRL_NOMENCLATURE_RECOGNISED = 'nomenclature_recognised'
CTRL_FILENAME_GRAMMAR = 'filename_grammar'
CTRL_VERDICT = 'verdict'

# Verdicts (posés dans detail['verdict'] de l'événement final). Pas de quarantine :
# le moteur ne reject jamais (cf. docstring) — seul le triage opérateur le fait.
VERDICT_QUALIFIED = 'qualified'
VERDICT_RECYCLE = 'recycle'

# Codes de cause normalisés (Event.cause_code).
CAUSE_NOMENCLATURE_NOT_FOUND = 'nomenclature_not_found'   # 0 nomenclature pour le subfolder
CAUSE_NOMENCLATURE_NO_MATCH = 'nomenclature_no_match'     # nom ne matche aucune nomenclature
CAUSE_AMBIGUOUS_NOMENCLATURE = 'ambiguous_nomenclature_config'  # ≥2 ex æquo
CAUSE_GRAMMAR_INVALID = 'grammar_invalid'

# Version du référentiel/règles au moment de la décision (traçabilité).
REFERENTIAL_VERSION = 1


def _emit(rf, control, result, monitoring_class, detail=None, cause_code=None):
    """Append un ``Event`` (audit). Append-only ; hérite du ``sub_tenant`` du fichier."""
    return Event.objects.create(
        file=rf, stage=STAGE, control=control, result=result,
        monitoring_class=monitoring_class, detail=detail or {},
        cause_code=cause_code, sub_tenant_id=rf.sub_tenant_id,
    )


def _ref(extra=None):
    """Snapshot référentiel à consigner dans ``detail`` (version des règles)."""
    base = {'referential_version': REFERENTIAL_VERSION}
    if extra:
        base.update(extra)
    return base


def _subfolder(rf):
    """Sous-dossier SFTP du fichier = dossier de ``s3_key`` (sans ``/`` de bord)."""
    key = rf.s3_key or rf.path or ''
    return posixpath.dirname(key).strip('/')


def _filename(rf):
    return posixpath.basename(rf.s3_key or rf.path or '')


def _qualified(rf, nom):
    """Verdict **qualified** : une Nomenclature reconnaît le fichier.

    ``nom`` (la Nomenclature matchée) est consignée dans le ``detail`` (audit, via
    ``nomenclature_id``) **et** retournée à la chaîne : elle est le contrat d'entrée
    du routing (§1.4), qui lira ``nom.route`` in-process (pas de relecture base)."""
    _emit(rf, CTRL_VERDICT, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          detail=_ref({'verdict': VERDICT_QUALIFIED, 'nomenclature_id': nom.pk,
                       'subfolder': nom.subfolder}))
    logger.info('Qualification QUALIFIED file=%s subfolder=%s nomenclature=%s',
                rf.pk, nom.subfolder, nom.pk)
    return VERDICT_QUALIFIED


def _recycle(rf, reason, extra=None):
    """Verdict **recycle** : corrigeable en interne (enrôlement), sera retraité.

    Seul verdict d'échec de la qualification : le moteur ne reject jamais."""
    detail = _ref({'verdict': VERDICT_RECYCLE, 'reason': reason})
    if extra:
        detail.update(extra)
    _emit(rf, CTRL_VERDICT, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
          detail=detail, cause_code=reason)
    logger.info('Qualification RECYCLE file=%s reason=%s', rf.pk, reason)
    return VERDICT_RECYCLE


def _grammar_regex(nom):
    """Regex de nom de fichier de la nomenclature, ou ``None`` (pas de contrainte).

    ``grammar = {"filename": "<regex>"}`` ; toute autre forme / clé absente ⇒ aucune
    contrainte de nom (admission = observation, on ne bloque pas par défaut).
    """
    grammar = nom.grammar if isinstance(nom.grammar, dict) else {}
    pattern = grammar.get('filename')
    return pattern or None


def _matching_nomenclatures(candidates, filename):
    """Sous-ensemble des ``candidates`` dont la grammaire reconnaît ``filename``.

    Grammaire vide ⇒ attrape-tout (matche). Lève ``re.error`` si une regex de config
    est invalide (l'appelant la traite en recycle ``grammar_invalid``)."""
    matches = []
    for nom in candidates:
        pattern = _grammar_regex(nom)
        if pattern is None:
            matches.append(nom)
        elif re.fullmatch(pattern, filename) is not None:
            matches.append(nom)
    return matches


def _run(rf):
    """Cœur de la qualification (peut lever ; encapsulé par ``file_qualification``).

    Renvoie ``(verdict, nomenclature)`` : la Nomenclature n'est peuplée que sur
    ``qualified`` (sinon ``None``). Aucun verdict reject : le moteur ne reject jamais.
    """
    subfolder = _subfolder(rf)
    filename = _filename(rf)

    # Contrôle 1 — sous-dossier enrôlé : ≥1 Nomenclature pour (canal, sous-dossier) ?
    candidates = list(
        Nomenclature.objects
        .filter(channel_id=rf.channel_id, subfolder=subfolder, active=True)
    ) if rf.channel_id else []
    if not candidates:
        # Discovery : sous-dossier inconnu → enrôle une Nomenclature puis rejoue.
        _emit(rf, CTRL_NOMENCLATURE_RECOGNISED, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': CAUSE_NOMENCLATURE_NOT_FOUND, 'subfolder': subfolder}),
              cause_code=CAUSE_NOMENCLATURE_NOT_FOUND)
        return _recycle(rf, CAUSE_NOMENCLATURE_NOT_FOUND, {'subfolder': subfolder}), None
    _emit(rf, CTRL_NOMENCLATURE_RECOGNISED, Event.Result.PASSED,
          Event.MonitoringClass.PUSH,
          detail=_ref({'subfolder': subfolder, 'candidates': [n.pk for n in candidates]}))

    # Contrôle 2 — sélection : la grammaire de QUELLE Nomenclature reconnaît le nom ?
    try:
        matches = _matching_nomenclatures(candidates, filename)
    except re.error as e:
        # Regex de config invalide → recycle (corriger la nomenclature, rejouer).
        _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': CAUSE_GRAMMAR_INVALID, 'filename': filename,
                           'error': str(e)}),
              cause_code=CAUSE_GRAMMAR_INVALID)
        return _recycle(rf, CAUSE_GRAMMAR_INVALID, {'filename': filename}), None
    if not matches:
        # Nom ne matche aucune Nomenclature enrôlée → recycle (PAS reject : un humain
        # tranche — ajouter une Nomenclature, ou rejeter via le triage opérateur).
        _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': CAUSE_NOMENCLATURE_NO_MATCH, 'filename': filename,
                           'subfolder': subfolder}),
              cause_code=CAUSE_NOMENCLATURE_NO_MATCH)
        return _recycle(rf, CAUSE_NOMENCLATURE_NO_MATCH,
                        {'filename': filename, 'subfolder': subfolder}), None
    if len(matches) > 1:
        top = max(n.priority for n in matches)
        tops = [n for n in matches if n.priority == top]
        if len(tops) > 1:
            # Plusieurs Nomenclatures ex æquo au sommet → anomalie de config (recycle).
            _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.FAILED,
                  Event.MonitoringClass.RECYCLE,
                  detail=_ref({'reason': CAUSE_AMBIGUOUS_NOMENCLATURE, 'filename': filename,
                               'tied_nomenclatures': sorted(n.pk for n in tops)}),
                  cause_code=CAUSE_AMBIGUOUS_NOMENCLATURE)
            return _recycle(rf, CAUSE_AMBIGUOUS_NOMENCLATURE,
                            {'filename': filename,
                             'tied_nomenclatures': sorted(n.pk for n in tops)}), None
        nom = tops[0]
    else:
        nom = matches[0]
    _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          detail=_ref({'filename': filename, 'nomenclature_id': nom.pk}))

    return _qualified(rf, nom), nom


def qualify_no_refresh(file_id):
    """Qualifie un fichier **sans** rematérialiser le board (peut lever).

    Réservé au **chaînage** depuis l'admission, qui fait un unique
    ``refresh_control_class`` couvrant les deux stages. Renvoie
    ``(verdict, nomenclature)``, ou ``(None, None)`` si le fichier n'a pas de canal
    résolu (non admis → rien à qualifier). La Nomenclature alimente le routing
    chaîné (in-process).
    """
    rf = ReceivedFile.objects.get(pk=file_id)
    if rf.channel_id is None:
        return None, None
    return _run(rf)


def file_qualification(file_id):
    """Lance la qualification d'un fichier (par son id) et renvoie le verdict.

    Entrée **autonome** (rematérialise le board), **ne lève jamais** (garde
    englobante). Renvoie le verdict (``qualified`` / ``recycle``), ``None`` si non
    qualifiable (pas de canal) ou en cas d'erreur inattendue.
    """
    try:
        verdict, _nom = qualify_no_refresh(file_id)
        refresh_control_class([file_id])
        return verdict
    except Exception:
        logger.exception('Qualification: erreur inattendue pour file %s', file_id)
        return None


def latest_qualification_event(rf_or_id):
    """Dernier événement de stage ``qualification`` d'un fichier (ou ``None``)."""
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE)
            .order_by('-created_at', '-id').first())

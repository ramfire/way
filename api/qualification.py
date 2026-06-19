"""Étape **qualification** du cycle de vie d'un fichier (post-admission).

Deuxième producteur de l'axe contrôles après l'admission. Une fois un fichier
**admis** (flux reconnu, partenaire actif, canal autorisé), la qualification décide
s'il est **conforme** à la grammaire attendue de son sous-dossier. C'est de
l'**observation/classification pure** (cf. docs/admission-monitoring-design.md §12) :

  * **par fichier** : sélecteur ``subfolder = dirname(s3_key)``, contrôle de nom
    ``basename(s3_key)`` contre la regex de la ``Nomenclature`` du canal ;
  * **chaînée** après un verdict admission ``admis`` (donc ``rf.channel`` résolu),
    et **rejouable** (re-jouer l'admission ré-exécute la qualification) ;
  * mêmes garanties que l'admission : append-only (``Event``), **ne touche jamais**
    ``ReceivedFile.state``, **ne lève jamais** vers l'appelant.

Verdicts (cf. §12) : pas de nomenclature → ``recycle`` (trou d'enrôlement,
retraitable) ; nom non conforme → ``quarantine`` (fichier mauvais, non retraité) ;
tout passe → ``qualified`` (``push``).
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

# Verdicts (posés dans detail['verdict'] de l'événement final).
VERDICT_QUALIFIED = 'qualified'
VERDICT_RECYCLE = 'recycle'
VERDICT_QUARANTINE = 'quarantine'

# Codes de cause normalisés (Event.cause_code).
CAUSE_NOMENCLATURE_NOT_FOUND = 'nomenclature_not_found'
CAUSE_GRAMMAR_MISMATCH = 'filename_grammar_mismatch'
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
    """Verdict **qualified** : nomenclature trouvée, nom conforme."""
    _emit(rf, CTRL_VERDICT, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          detail=_ref({'verdict': VERDICT_QUALIFIED, 'nomenclature_id': nom.pk,
                       'subfolder': nom.subfolder}))
    logger.info('Qualification QUALIFIED file=%s subfolder=%s', rf.pk, nom.subfolder)
    return VERDICT_QUALIFIED


def _recycle(rf, reason, extra=None):
    """Verdict **recycle** : corrigeable en interne (enrôlement), sera retraité."""
    detail = _ref({'verdict': VERDICT_RECYCLE, 'reason': reason})
    if extra:
        detail.update(extra)
    _emit(rf, CTRL_VERDICT, Event.Result.FAILED, Event.MonitoringClass.RECYCLE,
          detail=detail, cause_code=reason)
    logger.info('Qualification RECYCLE file=%s reason=%s', rf.pk, reason)
    return VERDICT_RECYCLE


def _quarantine(rf, reason, extra=None):
    """Verdict **quarantine** : fichier non conforme, conservé pour audit, non retraité."""
    detail = _ref({'verdict': VERDICT_QUARANTINE, 'reason': reason})
    if extra:
        detail.update(extra)
    _emit(rf, CTRL_VERDICT, Event.Result.FAILED, Event.MonitoringClass.REJECT,
          detail=detail, cause_code=reason)
    logger.info('Qualification QUARANTINE file=%s reason=%s', rf.pk, reason)
    return VERDICT_QUARANTINE


def _grammar_regex(nom):
    """Regex de nom de fichier de la nomenclature, ou ``None`` (pas de contrainte).

    ``grammar = {"filename": "<regex>"}`` ; toute autre forme / clé absente ⇒ aucune
    contrainte de nom (admission = observation, on ne bloque pas par défaut).
    """
    grammar = nom.grammar if isinstance(nom.grammar, dict) else {}
    pattern = grammar.get('filename')
    return pattern or None


def _run(rf):
    """Cœur de la qualification (peut lever ; encapsulé par ``file_qualification``)."""
    subfolder = _subfolder(rf)

    # Contrôle 1 — nomenclature reconnue pour (canal, sous-dossier) ?
    nom = (Nomenclature.objects
           .filter(channel_id=rf.channel_id, subfolder=subfolder, active=True)
           .first()) if rf.channel_id else None
    if nom is None:
        # Discovery : aucune nomenclature → en attente d'un humain (enrôle puis rejoue).
        _emit(rf, CTRL_NOMENCLATURE_RECOGNISED, Event.Result.FAILED,
              Event.MonitoringClass.RECYCLE,
              detail=_ref({'reason': CAUSE_NOMENCLATURE_NOT_FOUND, 'subfolder': subfolder}),
              cause_code=CAUSE_NOMENCLATURE_NOT_FOUND)
        return _recycle(rf, CAUSE_NOMENCLATURE_NOT_FOUND, {'subfolder': subfolder})
    _emit(rf, CTRL_NOMENCLATURE_RECOGNISED, Event.Result.PASSED,
          Event.MonitoringClass.PUSH, detail=_ref({'subfolder': subfolder}))

    # Contrôle 2 — grammaire (regex) du nom de fichier.
    filename = _filename(rf)
    pattern = _grammar_regex(nom)
    if pattern:
        try:
            matched = re.fullmatch(pattern, filename) is not None
        except re.error as e:
            # Regex de config invalide → recycle (corriger la nomenclature, rejouer).
            _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.FAILED,
                  Event.MonitoringClass.RECYCLE,
                  detail=_ref({'reason': CAUSE_GRAMMAR_INVALID, 'pattern': pattern,
                               'error': str(e)}),
                  cause_code=CAUSE_GRAMMAR_INVALID)
            return _recycle(rf, CAUSE_GRAMMAR_INVALID,
                            {'pattern': pattern, 'filename': filename})
    else:
        matched = True  # pas de contrainte de nom dans la grammaire
    if not matched:
        # Nom non conforme : le fichier lui-même est mauvais → quarantine (non retraité).
        _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.FAILED,
              Event.MonitoringClass.REJECT,
              detail=_ref({'reason': CAUSE_GRAMMAR_MISMATCH, 'filename': filename,
                           'pattern': pattern}),
              cause_code=CAUSE_GRAMMAR_MISMATCH)
        return _quarantine(rf, CAUSE_GRAMMAR_MISMATCH,
                           {'filename': filename, 'pattern': pattern})
    _emit(rf, CTRL_FILENAME_GRAMMAR, Event.Result.PASSED,
          Event.MonitoringClass.PUSH, detail=_ref({'filename': filename}))

    # Tout est passé → qualifié.
    return _qualified(rf, nom)


def qualify_no_refresh(file_id):
    """Qualifie un fichier **sans** rematérialiser le board (peut lever).

    Réservé au **chaînage** depuis l'admission, qui fait un unique
    ``refresh_control_class`` couvrant les deux stages. Renvoie le verdict, ou
    ``None`` si le fichier n'a pas de canal résolu (non admis → rien à qualifier).
    """
    rf = ReceivedFile.objects.get(pk=file_id)
    if rf.channel_id is None:
        return None
    return _run(rf)


def file_qualification(file_id):
    """Lance la qualification d'un fichier (par son id) et renvoie le verdict.

    Entrée **autonome** (rematérialise le board), **ne lève jamais** (garde
    englobante). Renvoie le verdict (``qualified`` / ``recycle`` / ``quarantine``),
    ``None`` si non qualifiable (pas de canal) ou en cas d'erreur inattendue.
    """
    try:
        verdict = qualify_no_refresh(file_id)
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

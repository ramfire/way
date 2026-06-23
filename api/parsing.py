"""Étape **parsing** (§1.5) : décodage structurel d'un fichier qualifié.

Quatrième producteur de l'axe contrôles. Une fois un fichier **qualifié** (une
Feed reconnaît son nom et porte sa spec de décodage ``layout``), le parsing
**décode** l'objet S3 selon ce ``layout`` et valide sa **structure** — forme
uniquement, jamais les valeurs métier :

  * **asynchrone** : exécuté hors du chemin webhook, par la commande ``parse_files``
    (worker/timer), car c'est le premier stage qui **lit le contenu** S3 ;
  * **entrée** = la Feed qui a qualifié le fichier (retrouvée via le dernier
    Event de qualification ``qualified``) ; son ``layout`` pilote tout le décodage ;
  * **frontières nettes** : ni complétude (``can_be_empty`` / nb attendu → §1.7), ni
    persistance des lignes décodées (→ load §1.6). Le parse se contente de rapporter
    des compteurs (``record_count``, ``column_count``) dans ``Event.detail`` ;
  * mêmes garanties que l'amont : append-only (``Event``), **ne touche jamais**
    ``ReceivedFile.state``, **ne lève jamais** vers l'appelant (garde englobante),
    **rejouable**.

Le moteur ne *reject* jamais : tout problème (config, lecture, décodage, forme) →
``recycle`` (retraitable — on corrige le ``layout`` puis on rejoue).
"""
import logging

from django.conf import settings

from .models import Event, Feed, ReceivedFile, refresh_control_class
from .qualification import VERDICT_QUALIFIED, latest_qualification_event
from .s3 import get_s3_client

logger = logging.getLogger(__name__)

STAGE = 'parsing'

# Nom de contrôle (stable : utilisé en lecture/board).
CTRL_FILE_DECODED = 'file_decoded'

# Verdicts (posés dans detail['verdict']).
VERDICT_PARSED = 'parsed'
VERDICT_RECYCLE = 'recycle'

# Causes normalisées (Event.cause_code).
CAUSE_UNSUPPORTED_FORMAT = 'unsupported_format'
CAUSE_TOO_LARGE = 'file_too_large'
CAUSE_UNREADABLE = 'unreadable'                     # objet S3 illisible (transitoire)
CAUSE_DECODE_ERROR = 'decode_error'
CAUSE_HEADER_MISMATCH = 'header_mismatch'
CAUSE_MALFORMED_RECORD = 'malformed_record'

# Formats délimités supportés par ce premier moteur.
SUPPORTED_FORMATS = ('csv',)

# Plafond de taille au-delà duquel on ne charge pas le fichier en mémoire.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024


def _max_bytes():
    return getattr(settings, 'PARSE_MAX_BYTES', DEFAULT_MAX_BYTES)


def _emit(rf, result, monitoring_class, detail, cause_code=None):
    """Append un ``Event`` de parsing (audit). Hérite du ``sub_tenant`` du fichier."""
    return Event.objects.create(
        file=rf, stage=STAGE, control=CTRL_FILE_DECODED, result=result,
        monitoring_class=monitoring_class, detail=detail,
        cause_code=cause_code, sub_tenant_id=rf.sub_tenant_id,
    )


def _recycle(rf, cause, extra=None):
    """Seul verdict d'échec : le moteur ne reject jamais (retraitable)."""
    detail = {'verdict': VERDICT_RECYCLE, 'reason': cause}
    if extra:
        detail.update(extra)
    _emit(rf, Event.Result.FAILED, Event.MonitoringClass.RECYCLE, detail, cause_code=cause)
    logger.info('Parsing RECYCLE file=%s cause=%s', rf.pk, cause)
    return VERDICT_RECYCLE


def _parsed(rf, detail):
    """Verdict **parsed** : le fichier décode et sa structure est conforme."""
    _emit(rf, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          {'verdict': VERDICT_PARSED, **detail})
    logger.info('Parsing PARSED file=%s records=%s cols=%s',
                rf.pk, detail.get('record_count'), detail.get('column_count'))
    return VERDICT_PARSED


def _passthrough(rf, feed):
    """``layout={}`` = « accepte tout » : push **sans** décodage (aucune lecture S3).

    Une famille sans spec déclarée laisse passer ses fichiers (décision produit).
    On émet un ``parsed`` marqué ``passthrough`` pour la traçabilité."""
    _emit(rf, Event.Result.PASSED, Event.MonitoringClass.PUSH,
          {'verdict': VERDICT_PARSED, 'passthrough': True,
           'feed_id': feed.pk})
    logger.info('Parsing PASSTHROUGH file=%s (layout non déclaré)', rf.pk)
    return VERDICT_PARSED


def _physical_key(rf):
    """Clé S3 réelle (idem presign/reconcile) : chemin physique, repli ``s3_key``."""
    return rf.path or (rf.s3_key or '').lstrip('/')


def _fetch_bytes(rf):
    """Lit l'objet S3 (peut lever : géré en ``unreadable`` par l'appelant)."""
    bucket = rf.bucket or settings.SCW_BUCKET_PREFIX
    obj = get_s3_client().get_object(Bucket=bucket, Key=_physical_key(rf))
    return obj['Body'].read()


def _parse(rf, feed):
    """Cœur du parsing (peut lever ; encapsulé par ``file_parsing``).

    Décode l'objet selon ``feed.layout`` et valide la **forme** : en-tête
    (présence + colonnes) puis nombre de champs par ligne de données. Émet un Event
    frais et renvoie le verdict. Aucune valeur métier inspectée."""
    layout = feed.layout if isinstance(feed.layout, dict) else {}
    if not layout:
        # `{}` = « accepte tout » : aucune spec déclarée → passthrough (push), sans
        # décodage ni lecture S3 (décision produit : layout non déclaré ⇒ on laisse passer).
        return _passthrough(rf, feed)

    fmt = layout.get('format')
    if fmt not in SUPPORTED_FORMATS:
        return _recycle(rf, CAUSE_UNSUPPORTED_FORMAT, {'format': fmt})

    delimiter = layout.get('delimiter') or ','
    encoding = layout.get('encoding') or 'utf-8'
    header_spec = layout.get('header') if isinstance(layout.get('header'), dict) else {}
    expected_columns = header_spec.get('columns')
    header_present = header_spec.get('present', bool(expected_columns))

    # Garde taille : ``file_size`` est fiable (renseigné à l'ingestion + backfill).
    if rf.file_size is not None and rf.file_size > _max_bytes():
        return _recycle(rf, CAUSE_TOO_LARGE,
                        {'file_size': rf.file_size, 'max_bytes': _max_bytes()})

    try:
        raw = _fetch_bytes(rf)
    except Exception as e:
        # Transitoire (S3 down / objet absent) → recycle (rejouer plus tard).
        return _recycle(rf, CAUSE_UNREADABLE, {'error': str(e)})

    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as e:
        return _recycle(rf, CAUSE_DECODE_ERROR, {'encoding': encoding, 'error': str(e)})

    # Lignes non vides (les lignes vides — fin de fichier incluse — sont ignorées).
    lines = [ln for ln in text.splitlines() if ln.strip() != '']

    if header_present:
        if not lines:
            return _recycle(rf, CAUSE_HEADER_MISMATCH, {'reason': 'no_header'})
        header_fields = lines[0].split(delimiter)
        if expected_columns is not None and header_fields != list(expected_columns):
            return _recycle(rf, CAUSE_HEADER_MISMATCH, {
                'expected_columns': len(expected_columns),
                'found_columns': len(header_fields)})
        column_count = len(header_fields)
        data_lines = lines[1:]
    else:
        if expected_columns:
            column_count = len(expected_columns)
        elif lines:
            column_count = len(lines[0].split(delimiter))
        else:
            column_count = 0
        data_lines = lines

    for i, line in enumerate(data_lines):
        fields = line.split(delimiter)
        if len(fields) != column_count:
            return _recycle(rf, CAUSE_MALFORMED_RECORD, {
                'record_index': i + 1,  # 1-based parmi les lignes de données
                'expected_fields': column_count,
                'found_fields': len(fields)})

    return _parsed(rf, {
        'format': fmt, 'column_count': column_count,
        'record_count': len(data_lines), 'feed_id': feed.pk,
    })


def _resolve_feed(rf):
    """Feed courante du fichier, ou ``None`` si pas actuellement ``qualified``.

    Source = dernier Event de qualification : on ne parse que si le verdict courant
    est ``qualified`` (sinon aucun contrat de décodage n'est applicable)."""
    ev = latest_qualification_event(rf)
    if ev is None or ev.detail.get('verdict') != VERDICT_QUALIFIED:
        return None
    nom_id = ev.detail.get('feed_id')
    return Feed.objects.filter(pk=nom_id).first() if nom_id else None


def parse_file_no_refresh(file_id, feed):
    """Parse via la ``feed`` fournie in-process **sans** refresh.

    Réservé au **chaînage** depuis l'admission (juste après le routing), qui fait un
    unique ``refresh_control_class`` couvrant tous les stages. Relit la ligne pour une
    lecture fraîche. Peut lever (la garde non bloquante est dans l'admission)."""
    rf = ReceivedFile.objects.get(pk=file_id)
    return _parse(rf, feed)


def parse_no_refresh(file_id):
    """Parse un fichier **sans** rematérialiser le board (peut lever).

    Variante **autonome** (worker / ``--file``) : retrouve elle-même la Feed
    via le dernier Event de qualification. Renvoie le verdict (``parsed`` /
    ``recycle``), ou ``None`` si le fichier n'est pas actuellement qualifié."""
    rf = ReceivedFile.objects.get(pk=file_id)
    nom = _resolve_feed(rf)
    if nom is None:
        return None
    return _parse(rf, nom)


def file_parsing(file_id):
    """Lance le parsing d'un fichier (par id) et renvoie le verdict.

    Entrée **autonome** (rematérialise le board), **rejouable**, **ne lève jamais**
    (garde englobante). Renvoie ``parsed`` / ``recycle``, ``None`` si non parsable
    (pas qualifié) ou en cas d'erreur inattendue."""
    try:
        verdict = parse_no_refresh(file_id)
        refresh_control_class([file_id])
        return verdict
    except Exception:
        logger.exception('Parsing: erreur inattendue pour file %s', file_id)
        return None


def latest_parsing_event(rf_or_id):
    """Dernier événement de stage ``parsing`` d'un fichier (ou ``None``)."""
    file_id = rf_or_id.pk if isinstance(rf_or_id, ReceivedFile) else rf_or_id
    return (Event.objects.filter(file_id=file_id, stage=STAGE)
            .order_by('-created_at', '-id').first())

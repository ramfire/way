import logging
import posixpath
import re
from collections import Counter

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import (
    Case, CharField, Count, F, FloatField, Func, IntegerField, Value, When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Lower
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from rest_framework.views import APIView
from rest_framework.response import Response

from .qualification import (
    CAUSE_FEED_NOT_FOUND, latest_qualification_event,
)
from .models import (
    MONITORING_SEVERITY, OPERATOR_REJECTED, Event, Handled, ReceivedFile,
    default_sub_tenant_id, operator_rejected_ids, refresh_control_class,
)
from .s3 import PRESIGN_DEFAULT_EXPIRY, object_size, presigned_get_url

logger = logging.getLogger(__name__)

# Classes de contrôle « en échec actionnable » sur lesquelles l'opérateur peut
# remédier (Recycle/Reject). On exclut `push`/NULL (OK ou aucun contrôle) et
# `warning_noaction` (avertissement explicitement sans action).
TRIAGE_FAILURE_CLASSES = {
    Event.MonitoringClass.BLOCKING, Event.MonitoringClass.WARNING_ACTION,
    Event.MonitoringClass.RECYCLE, Event.MonitoringClass.REJECT,
}


def _triage_eligible(rf):
    """Un fichier est remédiable (Recycle/Reject) ssi son contrôle est en échec
    actionnable ET qu'il n'est pas déjà tranché : ni « traité » (``Handled``), ni
    déjà rejeté par un opérateur (``operator_rejected``). Garde serveur des deux
    actions — l'UI n'affiche les boutons que sur ce même critère (``can_remediate``).
    """
    if rf.control_class not in TRIAGE_FAILURE_CLASSES:
        return False
    if Handled.objects.filter(file=rf).exists():
        return False
    return rf.pk not in operator_rejected_ids([rf.pk])


class FileNotReady(Exception):
    """Fichier non téléchargeable ; ``status`` + ``detail`` décrivent le refus."""

    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def presign_received_file(rf, expires_in=PRESIGN_DEFAULT_EXPIRY):
    """URL pré-signée (GET) pour un ``ReceivedFile``, ou lève ``FileNotReady``.

    L'objet réel dans le bucket est le chemin *physique* (``path`` = key_prefix
    + chemin virtuel SFTPGo), PAS ``s3_key`` (chemin virtuel) — sinon 404 S3.
    """
    if rf.state != ReceivedFile.State.STORED:
        raise FileNotReady(409, f'fichier non disponible (état: {rf.state})')
    bucket = rf.bucket or settings.SCW_BUCKET_PREFIX
    key = rf.path or rf.s3_key.lstrip('/')
    if not bucket or not key:
        raise FileNotReady(422, 'objet S3 introuvable (bucket/clé manquant)')
    filename = posixpath.basename(key)
    try:
        url = presigned_get_url(bucket, key, expires_in=expires_in, filename=filename)
    except Exception:
        logger.exception('Presign échoué pour ReceivedFile %s (%s/%s)', rf.pk, bucket, key)
        raise FileNotReady(502, 'erreur génération URL')
    return {'url': url, 'bucket': bucket, 'key': key,
            'filename': filename, 'expires_in': expires_in}


def _key(p):
    """Clé S3 logique : chemin virtuel SFTPGo (fallback chemin physique)."""
    return p.get('virtual_path') or p.get('path') or ''


class SFTPWebhookView(APIView):
    """Receives SFTPGo action hooks (e.g. file uploads).

    SFTPGo's HTTP action hook POSTs a JSON body and does NOT send custom
    headers, so the shared secret is accepted either as the ``token`` query
    param (how SFTPGo is configured) or an ``X-Webhook-Token`` header (handy
    for manual testing).
    """

    authentication_classes = []  # internal endpoint, guarded by shared token
    permission_classes = []

    def post(self, request):
        token = request.query_params.get('token') or request.headers.get('X-Webhook-Token')
        if not settings.SFTPGO_WEBHOOK_TOKEN or token != settings.SFTPGO_WEBHOOK_TOKEN:
            return Response({'detail': 'forbidden'}, status=403)

        payload = request.data if isinstance(request.data, dict) else {}
        action = payload.get('action')

        # `pre-upload` est SYNCHRONE et BLOQUANT côté SFTPGo : on enregistre au
        # mieux mais on renvoie TOUJOURS 200 sur token valide, pour qu'un souci
        # d'enregistrement ne bloque jamais un upload (seul un gunicorn down le
        # ferait). D'où le try/except englobant.
        handler = {
            'pre-upload': self._on_pre_upload,
            'upload': self._on_upload,
            'delete': self._on_delete,
            'rename': self._on_rename,
        }.get(action)
        try:
            if handler:
                handler(payload)
        except Exception:
            logger.exception('Webhook %s: échec enregistrement métadonnées', action)

        return Response({'ok': True})

    def _on_pre_upload(self, p):
        """Avant écriture S3 : créer la ligne en état ``receiving``.

        Idempotent : si SFTPGo rejoue le hook (même session + même clé), on
        réutilise la ligne ``receiving`` existante au lieu d'en créer un doublon.
        """
        s3_key = _key(p)
        ReceivedFile.objects.get_or_create(
            state=ReceivedFile.State.RECEIVING,
            s3_key=s3_key,
            session_id=p.get('session_id') or '',
            defaults=dict(
                path=p.get('path') or '',
                username=p.get('username') or '',
                protocol=p.get('protocol') or '',
                ip=p.get('ip') or None,
                bucket=p.get('bucket') or '',
                action='pre-upload',
                sftpgo_timestamp=p.get('timestamp'),
                # Tenant d'ingest par défaut (l'admission le re-pointe ensuite).
                sub_tenant_id=default_sub_tenant_id(),
                raw=p,
            ),
        )
        logger.info('SFTP pre-upload: %s by %s', s3_key, p.get('username'))

    def _on_upload(self, p):
        """Après écriture S3 : confirmer ``stored`` (ou ``failed``)."""
        s3_key = _key(p)
        username = p.get('username') or ''
        session_id = p.get('session_id') or ''
        sftp_status = p.get('status')
        # SFTPGo: status == 1 => succès. Absent => on suppose succès (post-hook).
        success = sftp_status in (None, 1)
        state = ReceivedFile.State.STORED if success else ReceivedFile.State.FAILED

        # Taille du fichier : le hook `upload` de SFTPGo ne transmet PAS `file_size`.
        # Quand elle manque (upload réussi), on la lit une fois depuis S3 (clé
        # physique = `path`, repli `virtual_path`) pour la stocker à la source — ainsi
        # tous les consommateurs (complétude §1.7, board…) la trouvent en base, sans
        # toucher S3 plus tard. Best-effort : `object_size` renvoie None si S3 KO,
        # auquel cas la ligne reste sans taille (le filet `reconcile_files` rattrape).
        file_size = p.get('file_size')
        if file_size is None and success:
            bucket = p.get('bucket') or settings.SCW_BUCKET_PREFIX
            physical_key = p.get('path') or _key(p).lstrip('/')
            file_size = object_size(bucket, physical_key)

        fields = dict(
            path=p.get('path') or '',
            username=username,
            protocol=p.get('protocol') or '',
            ip=p.get('ip') or None,
            session_id=session_id,
            file_size=file_size,
            status=sftp_status,
            bucket=p.get('bucket') or '',
            action='upload',
            sftpgo_timestamp=p.get('timestamp'),
            state=state,
            raw=p,
        )

        # Corréler avec la ligne pre-upload (même session + même clé), sinon créer.
        rf = (ReceivedFile.objects
              .filter(state=ReceivedFile.State.RECEIVING, s3_key=s3_key, session_id=session_id)
              .order_by('-received_at')
              .first())
        if rf:
            for k, v in fields.items():
                setattr(rf, k, v)
            rf.stored_at = timezone.now() if success else None
            rf.save()
        else:
            # Pas de ligne pre-upload corrélée : on crée directement (tenant d'ingest
            # par défaut, re-pointé par l'admission). La branche update conserve le
            # sub_tenant déjà posé au pre-upload.
            rf = ReceivedFile.objects.create(
                s3_key=s3_key,
                stored_at=timezone.now() if success else None,
                sub_tenant_id=default_sub_tenant_id(),
                **fields,
            )
        rf_id = rf.pk
        logger.info('SFTP upload %s: %s by %s',
                    'stored' if success else 'FAILED', s3_key, username)
        # TODO: déclencher DORA checks (futur worker via flag `processed`)

        # Admission (post-stockage) : observation/classification, JAMAIS bloquante.
        # Lancée seulement après que la ligne est `stored` et la 200 est acquise.
        # Garde dédiée en plus du try/except de post() : l'admission ne peut EN
        # AUCUN CAS affecter la réponse webhook ni l'upload (invariant 1).
        if success and rf_id is not None:
            self._run_admission(rf_id)

    @staticmethod
    def _run_admission(file_id):
        """Admission + chaînage identification, garantis non bloquants (webhook 200).

        Séparation des stages : le chaînage admission→identification (§1.6-b) vit
        **ici, dans l'orchestrateur**, PAS dans ``file_admission``. L'identification
        n'est lancée que si l'admission aboutit à un verdict **push**
        (``control_class``) ; un recycle/reject n'identifie rien. Garde englobante :
        ni l'admission ni l'identification ne peuvent affecter la réponse webhook."""
        try:
            from .admission import file_admission, file_identification
            file_admission(file_id)
            rf = ReceivedFile.objects.get(pk=file_id)
            if rf.control_class == Event.MonitoringClass.PUSH:
                file_identification(file_id)
        except Exception:
            logger.exception(
                'Admission/identification: échec non bloquant pour file %s', file_id)

    def _on_delete(self, p):
        """Fichier supprimé via SFTP : marquer les lignes ``stored`` en ``deleted``.

        Idempotent : on ne (re)marque que ce qui est encore ``stored``. La ligne
        et son ``raw`` d'origine sont conservés (audit) ; on n'efface jamais.
        """
        s3_key = _key(p)
        n = (ReceivedFile.objects
             .filter(s3_key=s3_key, state=ReceivedFile.State.STORED)
             .update(state=ReceivedFile.State.DELETED,
                     deleted_at=timezone.now(), action='delete'))
        logger.info('SFTP delete: %s by %s (%d ligne(s) marquée(s) deleted)',
                    s3_key, p.get('username'), n)

    def _on_rename(self, p):
        """Fichier renommé/déplacé via SFTP : repointer la clé virtuelle + physique.

        SFTPGo fournit l'ancien chemin (``virtual_path``/``path``) et le nouveau
        (``virtual_target_path``/``target_path``). Idempotent : si déjà repointé
        (plus rien sur l'ancienne clé), ``update`` renvoie simplement 0.
        """
        old_key = _key(p)
        new_key = p.get('virtual_target_path') or p.get('target_path') or old_key
        new_path = p.get('target_path') or p.get('path') or ''
        n = (ReceivedFile.objects
             .filter(s3_key=old_key, state=ReceivedFile.State.STORED)
             .update(s3_key=new_key, path=new_path, action='rename'))
        logger.info('SFTP rename: %s -> %s by %s (%d ligne(s) repointée(s))',
                    old_key, new_key, p.get('username'), n)


class PresignedDownloadView(APIView):
    """Renvoie une URL pré-signée (GET, 15 min) vers un fichier reçu.

    Endpoint interne : protégé par ``INTERNAL_TOKEN`` accepté en query param
    ``?token=`` ou header ``X-Internal-Token``. Pas de secret S3 côté client :
    le navigateur tape Scaleway directement avec l'URL signée.

    L'objet réel dans le bucket est le chemin *physique* (``path`` = key_prefix
    + chemin virtuel SFTPGo), PAS ``s3_key`` (chemin virtuel) — sinon 404.
    """

    authentication_classes = []  # endpoint interne, garde par token partagé
    permission_classes = []

    def get(self, request, pk):
        token = request.query_params.get('token') or request.headers.get('X-Internal-Token')
        if not settings.INTERNAL_TOKEN or token != settings.INTERNAL_TOKEN:
            return Response({'detail': 'forbidden'}, status=403)

        try:
            rf = ReceivedFile.objects.get(pk=pk)
        except ReceivedFile.DoesNotExist:
            return Response({'detail': 'not found'}, status=404)

        try:
            data = presign_received_file(rf)
        except FileNotReady as e:
            return Response({'detail': e.detail}, status=e.status)

        expires_at = timezone.now() + timezone.timedelta(seconds=data['expires_in'])
        return Response({**data, 'expires_at': expires_at.isoformat()})


@staff_member_required
def download_received_file(request, pk):
    """Page web (proxy authentifié) : redirige vers l'URL pré-signée Scaleway.

    Gardée par la session admin Django (``staff_member_required``) : aucun
    token partagé ne transite côté client. L'admin clique → 302 vers S3.
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    try:
        data = presign_received_file(rf)
    except FileNotReady as e:
        return HttpResponse(e.detail, status=e.status, content_type='text/plain; charset=utf-8')
    logger.info('Download presign par %s pour ReceivedFile %s (%s)',
                request.user, pk, data['key'])
    return HttpResponseRedirect(data['url'])


# --- Monitoring temps réel (page auto-rafraîchie côté navigateur) -------------

MONITORING_FEED_LIMIT = 50   # taille de page par défaut renvoyée par le feed
MONITORING_FEED_MAX = 500    # borne dure (?limit=) : protège le payload / la DB

# Tri serveur : clés de colonnes acceptées (?sort=). Les champs *dérivés*
# (filename, elapsed_ms, et stored_at en History) ne sont pas des colonnes brutes ;
# ils sont calculés en base via annotations (voir `monitoring_feed`) pour que le
# tri porte sur TOUTE la table et pas seulement sur la page renvoyée.
SORT_KEYS = frozenset({
    'state', 'filename', 'username', 'protocol', 'ip',
    'file_size', 'elapsed_ms', 'received_at', 'stored_at',
    'control',   # colonne « Contrôles » : tri par SÉVÉRITÉ (pas alphabétique)
})

# Vue « Live » : flux opérationnel courant (à surveiller / à traiter). Les `failed`
# y restent visibles tant qu'ils ne sont pas traités (futures actions de remédiation).
LIVE_STATES = (
    ReceivedFile.State.RECEIVING,
    ReceivedFile.State.STORED,
    ReceivedFile.State.FAILED,
)
# Vue « History » : états terminaux/archivés (deleted/missing automatiques +
# `archived` posé manuellement sur un échec traité).
HISTORY_STATES = (
    ReceivedFile.State.DELETED,
    ReceivedFile.State.MISSING,
    ReceivedFile.State.ARCHIVED,
)


@ensure_csrf_cookie  # pose le cookie csrftoken : le JS le renvoie en header sur les actions POST
@staff_member_required
def monitoring_page(request):
    """Page de supervision live des fichiers reçus (polling JS, voir template)."""
    return render(request, 'monitoring.html', {'feed_limit': MONITORING_FEED_LIMIT})


@require_POST
@staff_member_required
def archive_received_file(request, pk):
    """Action UI : sortir un échec traité du board Live → état ``archived``.

    Restreint aux lignes ``failed`` (cf. portée décidée). Idempotent côté effet :
    si la ligne n'est plus ``failed`` (déjà archivée / changée), on renvoie 409.
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    if rf.state != ReceivedFile.State.FAILED:
        return JsonResponse(
            {'detail': f'non archivable (état: {rf.state})'}, status=409)
    rf.state = ReceivedFile.State.ARCHIVED
    rf.archived_at = timezone.now()
    rf.action = 'archive'
    rf.save(update_fields=['state', 'archived_at', 'action'])
    logger.info('Archive ReceivedFile %s (%s) par %s', pk, rf.s3_key, request.user)
    return JsonResponse({'ok': True, 'id': pk, 'state': rf.state})


@require_POST
@staff_member_required
def restore_received_file(request, pk):
    """Action UI : renvoyer un fichier archivé dans Live → état ``failed``.

    Réversibilité de l'archivage : la portée n'autorisant que les ``failed`` à
    être archivés, on les restaure vers ``failed``. Refuse (409) ce qui n'est
    pas ``archived`` (un ``deleted``/``missing`` reflète l'état réel S3/SFTP).
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    if rf.state != ReceivedFile.State.ARCHIVED:
        return JsonResponse(
            {'detail': f'non restaurable (état: {rf.state})'}, status=409)
    rf.state = ReceivedFile.State.FAILED
    rf.archived_at = None
    rf.action = 'restore'
    rf.save(update_fields=['state', 'archived_at', 'action'])
    logger.info('Restore ReceivedFile %s (%s) par %s', pk, rf.s3_key, request.user)
    return JsonResponse({'ok': True, 'id': pk, 'state': rf.state})


@staff_member_required
def monitoring_feed(request):
    """JSON consommé par la page monitoring : une page de fichiers + compteurs.

    Deux vues via ``?view=`` : ``live`` (défaut — flux courant receiving/stored/
    failed, échecs en tête) et ``history`` (archives deleted/missing). Le **tri**
    (``?sort=&dir=``) et le **filtre par état** (``?state=``) sont appliqués côté
    serveur sur TOUTE la table (et pas seulement sur la page) ; on renvoie ensuite
    une **page** (``?limit=`` défaut 50 borné, ``?offset=`` pour paginer) plus
    ``matched_total`` (nb total de lignes correspondant au filtre) pour que l'UI
    pagine « a–b / total ». Lecture seule, gardée par la session staff (cookie du
    fetch same-origin) ; pas de CSRF (GET non mutant).
    """
    view = 'history' if request.GET.get('view') == 'history' else 'live'
    states = HISTORY_STATES if view == 'history' else LIVE_STATES
    base = ReceivedFile.objects.filter(state__in=states)

    # Filtre par état (chip cliquable). On ne retient que les états valides pour la
    # vue courante ; toute autre valeur est ignorée (= pas de filtre).
    valid_states = {s.value for s in states}
    state_filter = request.GET.get('state')
    if state_filter not in valid_states:
        state_filter = None
    if state_filter:
        base = base.filter(state=state_filter)

    # Triage : par défaut on MASQUE les fichiers TRANCHÉS → le board ne montre que ce
    # qui reste à arbitrer. « Tranché » = soit « traité » (ligne ``Handled``, set-once,
    # forcément un OK ``push``), soit **rejeté définitivement** par un opérateur
    # (Event de triage ``operator_rejected``, forcément un ``reject`` via le
    # court-circuit). `?show_handled=1` réaffiche les deux (toggle UI). Le masquage
    # s'applique AUSSI aux compteurs ci-dessous (chip ↔ liste cohérents).
    show_handled = request.GET.get('show_handled') == '1'
    _rejected_q = {'events__stage': Event.Stage.TRIAGE,
                   'events__cause_code': OPERATOR_REJECTED}

    def _hide_resolved(qs):
        if show_handled:
            return qs
        return qs.filter(handled__isnull=True).exclude(**_rejected_q)

    def _undecided(qs):
        # Aucune décision opérateur posée (ni « Recycled »/Handled, ni « Rejected »).
        return qs.filter(handled__isnull=True).exclude(**_rejected_q)

    # Compteurs des 4 ÉTATS affichés — vocabulaire sans ambiguïté verdict↔décision —
    # plus `none` (aucun contrôle encore passé). Mutuellement exclusifs, indépendants
    # du toggle :
    #   ok       = verdict OK (push), aucune décision        → vert
    #   failed   = verdict en ÉCHEC, aucune décision         → orange + boutons Recycle/Reject
    #   recycled = l'opérateur a recyclé (ligne Handled)     → terminal, Download seul
    #   rejected = l'opérateur a rejeté (operator_rejected)  → terminal, Download seul
    # NB : un `reject` MOTEUR (quarantine, sans décision) tombe dans `failed` — plus
    # jamais « Rejected » tant qu'aucune décision opérateur n'est prise.
    _vis = ReceivedFile.objects.filter(state__in=states)
    _und = _undecided(_vis)
    per_display_state = {}
    for key, n in (
        ('ok', _und.filter(control_class=Event.MonitoringClass.PUSH).count()),
        ('failed', _und.filter(control_class__in=TRIAGE_FAILURE_CLASSES).count()),
        ('recycled', _vis.filter(handled__isnull=False).count()),
        ('rejected', _vis.filter(**_rejected_q).distinct().count()),
        ('none', _und.filter(control_class__isnull=True).count()),
    ):
        if n:
            per_display_state[key] = n

    # Filtre par état affiché (chip cliquable). `recycled`/`rejected` outrepassent le
    # masquage par défaut (sinon rien à voir) ; `ok`/`failed`/`none` restent sur les
    # fichiers SANS décision. Autre valeur → pas de filtre (vue par défaut masquée).
    control_filter = request.GET.get('control')
    if control_filter == 'recycled':
        base = base.filter(handled__isnull=False)
    elif control_filter == 'rejected':
        base = base.filter(**_rejected_q).distinct()
    elif control_filter == 'ok':
        base = _undecided(base).filter(control_class=Event.MonitoringClass.PUSH)
    elif control_filter == 'failed':
        base = _undecided(base).filter(control_class__in=TRIAGE_FAILURE_CLASSES)
    elif control_filter == 'none':
        base = _undecided(base).filter(control_class__isnull=True)
    else:
        control_filter = None
        base = _hide_resolved(base)

    # Total des lignes correspondant au(x) filtre(s), AVANT pagination (top-N).
    matched_total = base.count()

    # Taille de page (?limit=), bornée pour protéger le payload et la DB.
    try:
        limit = int(request.GET.get('limit', MONITORING_FEED_LIMIT))
    except (TypeError, ValueError):
        limit = MONITORING_FEED_LIMIT
    limit = max(1, min(limit, MONITORING_FEED_MAX))

    # Décalage de pagination (?offset=). Borné à >= 0 ; si l'offset dépasse le
    # total filtré (vue/tri/filtre changé, lignes passées en History entre deux
    # polls…), on recale sur la dernière page non vide plutôt que de renvoyer une
    # page vide. L'UI resynchronise son offset sur celui renvoyé.
    try:
        offset = int(request.GET.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    if matched_total and offset >= matched_total:
        offset = ((matched_total - 1) // limit) * limit

    # Annotations pour le tri serveur des champs dérivés :
    #  - _filename : basename de s3_key (tout après le dernier '/'), insensible à la casse ;
    #  - _elapsed  : durée de transfert (ms) extraite du JSON `raw` et castée en nombre ;
    #  - _stored_eff : horodatage « dernière colonne » en History (archive/suppression/stockage) ;
    #  - _ctrl_sev : sévérité de la classe de monitoring matérialisée (control_class),
    #    pour trier la colonne « Contrôles » par GRAVITÉ et non alphabétiquement
    #    (NULL/inconnu → -1, le moins sévère). Worst-wins déjà matérialisé en amont.
    ctrl_sev = Case(
        *[When(control_class=cls, then=Value(sev))
          for cls, sev in MONITORING_SEVERITY.items()],
        default=Value(-1), output_field=IntegerField(),
    )
    base = base.annotate(
        _filename=Lower(Func(
            F('s3_key'), Value(r'^.*/'), Value(''),
            function='regexp_replace', output_field=CharField())),
        _elapsed=Cast(KeyTextTransform('elapsed', 'raw'), FloatField()),
        _stored_eff=Coalesce('archived_at', 'deleted_at', 'stored_at'),
        _ctrl_sev=ctrl_sev,
    )

    sort = request.GET.get('sort')
    direction = 'desc' if request.GET.get('dir') == 'desc' else 'asc'
    if sort in SORT_KEYS:
        source = {
            'state': F('state'),
            'filename': F('_filename'),
            'username': Lower('username'),
            'protocol': Lower('protocol'),
            'ip': F('ip'),                # GenericIPAddressField → inet : tri numérique natif
            'file_size': F('file_size'),
            'elapsed_ms': F('_elapsed'),
            'received_at': F('received_at'),
            # stored_at : effectif (archive/suppr/stockage) en History, brut en Live.
            'stored_at': F('_stored_eff') if view == 'history' else F('stored_at'),
            # control : tri par sévérité de la classe (desc = le plus grave en tête).
            'control': F('_ctrl_sev'),
        }[sort]
        primary = (source.desc(nulls_last=True) if direction == 'desc'
                   else source.asc(nulls_last=True))
        # Tri explicite : on l'honore tel quel (pas de « failed en tête »), avec un
        # départage déterministe par récence puis id.
        qs = list(base.order_by(primary, '-received_at', '-id')[offset:offset + limit])
    elif view == 'live':
        # Ordre par défaut Live : erreurs (failed) en tête, puis les plus récents.
        sort = None
        err_first = Case(
            When(state=ReceivedFile.State.FAILED, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
        qs = list(base.annotate(_err_first=err_first)
                  .order_by('_err_first', '-received_at', '-id')[offset:offset + limit])
    else:
        sort = None
        qs = list(base.order_by('-received_at', '-id')[offset:offset + limit])

    # Fichiers « traités » (sparse) + rejetés opérateur parmi les lignes de la page
    # (batché pour éviter le N+1 ; pilotent `handled` et `can_remediate`).
    page_ids = [rf.pk for rf in qs]
    handled_ids = set(
        Handled.objects.filter(file_id__in=page_ids).values_list('file_id', flat=True))
    rejected_ids = operator_rejected_ids(page_ids)

    def _display_state(rf):
        # Les 4 états sans ambiguïté (cf. compteurs ci-dessus). La décision opérateur
        # (Recycled/Rejected) prime sur le verdict ; un `reject` moteur sans décision
        # reste « failed » (jamais « rejected »).
        if rf.pk in handled_ids:
            return 'recycled'
        if rf.pk in rejected_ids:
            return 'rejected'
        if rf.control_class == Event.MonitoringClass.PUSH:
            return 'ok'
        if rf.control_class in TRIAGE_FAILURE_CLASSES:
            return 'failed'
        return 'none'

    def to_row(rf):
        name = posixpath.basename(rf.s3_key or rf.path or '') or '(sans nom)'
        # Durée de transfert : SFTPGo la fournit dans le payload (`elapsed`, ms).
        elapsed = rf.raw.get('elapsed') if isinstance(rf.raw, dict) else None
        ds = _display_state(rf)
        return {
            'id': rf.pk,
            'state': rf.state,
            'filename': name,
            'path': rf.s3_key,
            'username': rf.username,
            'protocol': rf.protocol,
            'ip': rf.ip,
            'file_size': rf.file_size,
            'elapsed_ms': elapsed,
            'received_at': rf.received_at.isoformat() if rf.received_at else None,
            'stored_at': rf.stored_at.isoformat() if rf.stored_at else None,
            'deleted_at': rf.deleted_at.isoformat() if rf.deleted_at else None,
            'archived_at': rf.archived_at.isoformat() if rf.archived_at else None,
            'downloadable': rf.state == ReceivedFile.State.STORED,
            # Axe contrôles : classe « worst-wins » matérialisée (read-model). Badge
            # générique du board ; le détail par contrôle est dans la modale (dbl-clic).
            'control_class': rf.control_class,
            # Triage (étape 5) : booléen « traité » AU NIVEAU FICHIER. Couplage
            # strict ⟹ vrai uniquement sur un OK (``push``) retraité ; le front
            # n'affiche le badge que si `control_class === 'push' && handled`.
            'handled': rf.pk in handled_ids,
            # Actions disponibles côté UI (gardées aussi côté serveur).
            'can_archive': rf.state == ReceivedFile.State.FAILED,
            'can_restore': rf.state == ReceivedFile.State.ARCHIVED,
            # État affiché (4 états sans ambiguïté) — pilote badge + boutons. `failed`
            # = échec SANS décision opérateur ⟹ seul état qui propose Recycle/Reject.
            'display_state': ds,
            'can_remediate': ds == 'failed',
        }

    # Compteurs globaux par état (indexés) : pilotent les chips + le badge History.
    # Respectent le masquage des traités (cohérence avec les lignes/compteurs visibles).
    per_state = {s.value: 0 for s in ReceivedFile.State}
    for row in _hide_resolved(ReceivedFile.objects.all()).values('state').annotate(n=Count('id')):
        per_state[row['state']] = row['n']
    live_total = sum(per_state[s.value] for s in LIVE_STATES)
    history_total = sum(per_state[s.value] for s in HISTORY_STATES)

    return JsonResponse({
        'view': view,
        'rows': [to_row(rf) for rf in qs],
        'per_state': per_state,
        'per_display_state': per_display_state,
        'live_total': live_total,
        'history_total': history_total,
        # Écho de la requête de tri/filtre + cardinalités (pilote « affichés / total »).
        'state_filter': state_filter,
        'control_filter': control_filter,
        'show_handled': show_handled,
        'sort': sort,
        'dir': direction if sort else None,
        'limit': limit,
        'offset': offset,
        'matched_total': matched_total,
        'returned': len(qs),
        'server_time': timezone.now().isoformat(),
    })


@staff_member_required
def monitoring_causes(request):
    """Agrégation « par cause » des contrôles en échec courants (complément).

    Vue de **second niveau** (unité de travail = le fichier, cf. design §7) : regroupe
    l'état **courant** des contrôles en échec (dernier ``Event`` par
    ``(file, stage, control)``, ``result=failed``) par **cause** =
    ``(stage, control, monitoring_class, reason)``. Pour chaque cause : nombre de
    fichiers, partenaires dominants, quelques exemples. Sert à voir que « N lignes
    partagent une cause » et à **surfacer les signaux** (un même ``warning_action``
    sur 2092 fichiers = une action). Lecture seule, gardée staff ; ne modifie rien.
    """
    view = 'history' if request.GET.get('view') == 'history' else 'live'
    states = HISTORY_STATES if view == 'history' else LIVE_STATES
    file_ids = list(ReceivedFile.objects.filter(state__in=states)
                    .values_list('id', flat=True))

    # État courant de chaque contrôle (dernier event par (file, stage, control)).
    events = (Event.objects.filter(file_id__in=file_ids)
              .order_by('file_id', 'stage', 'control', '-created_at', '-id')
              .values('file_id', 'stage', 'control', 'monitoring_class', 'result', 'detail'))
    current = {}
    for e in events:
        key = (e['file_id'], e['stage'], e['control'])
        if key not in current:
            current[key] = e

    # Regrouper les contrôles COURANTS en échec par cause.
    causes = {}
    affected = set()
    for e in current.values():
        if e['result'] != Event.Result.FAILED:
            continue
        affected.add(e['file_id'])
        detail = e['detail'] if isinstance(e['detail'], dict) else {}
        reason = detail.get('reason') or '—'
        key = (e['stage'], e['control'], e['monitoring_class'], reason)
        agg = causes.get(key)
        if agg is None:
            agg = causes[key] = {
                'stage': e['stage'], 'control': e['control'],
                'monitoring_class': e['monitoring_class'], 'reason': reason,
                'count': 0, '_users': Counter(), '_examples': [], '_files': [],
            }
        agg['count'] += 1
        agg['_files'].append(e['file_id'])
        # On compte aussi l'username vide ('' = partenaire « vide » bien réel ici) ;
        # l'UI l'affiche « ∅ ». most_common surface le(s) partenaire(s) dominant(s).
        agg['_users'][detail.get('username') or ''] += 1
        if len(agg['_examples']) < 5:
            agg['_examples'].append(e['file_id'])

    # Résoudre les noms d'exemples (une seule requête).
    ex_ids = {fid for c in causes.values() for fid in c['_examples']}
    names = {}
    if ex_ids:
        for rf in (ReceivedFile.objects.filter(id__in=ex_ids)
                   .values('id', 's3_key', 'path')):
            names[rf['id']] = posixpath.basename(
                rf['s3_key'] or rf['path'] or '') or '(sans nom)'

    # Triage (étape 5) — réconciliation AU NIVEAU FICHIER uniquement : le triage par
    # cause (``TriageAck``) a été retiré. Un fichier « traité » = ligne Handled (sparse).
    handled_files = set(
        Handled.objects.filter(file_id__in=affected)
        .values_list('file_id', flat=True))

    open_files = set()   # fichiers « à traiter » (non traités), distincts
    rows = []
    for key, c in causes.items():
        files = c['_files']
        n_file_resolved = sum(1 for fid in files if fid in handled_files)
        # « À traiter » dans cette cause = les fichiers non traités (sans Handled).
        n_open = c['count'] - n_file_resolved
        open_files.update(fid for fid in files if fid not in handled_files)
        rows.append({
            'stage': c['stage'], 'control': c['control'],
            'monitoring_class': c['monitoring_class'], 'reason': c['reason'],
            'count': c['count'],
            'open_count': n_open,
            'file_resolved_count': n_file_resolved,
            'top_users': [{'username': u, 'count': n} for u, n in c['_users'].most_common(3)],
            'examples': [names.get(fid, str(fid)) for fid in c['_examples']],
        })
    # Tri : le plus sévère d'abord, puis le plus gros volume.
    rows.sort(key=lambda r: (-MONITORING_SEVERITY.get(r['monitoring_class'], -1), -r['count']))

    return JsonResponse({
        'view': view,
        'causes': rows,
        'cause_total': len(rows),
        'files_affected': len(affected),
        'files_open': len(open_files),   # après réconciliation triage (cause × fichier)
        'server_time': timezone.now().isoformat(),
    })


def _admission_payload(rf):
    """Trace de contrôle d'un fichier : tous les événements de l'axe contrôles
    (stages ``admission``, ``qualification``, ``routing`` **et** ``parsing`` — chaque
    contrôle + le verdict de chaque stage), du plus ancien au plus récent. Forme
    partagée par ``admission_detail`` (lecture), ``replay_admission`` et
    ``enrol_feed`` (rejeu). Le stage ``triage`` (décision opérateur) est
    volontairement exclu de la trace de contrôle.

    Expose aussi de quoi proposer l'enrôlement d'une feed côté UI :
    ``subfolder`` (le dossier du fichier) et ``needs_feed`` (vrai si le
    fichier est admis mais que sa qualification est en ``recycle`` faute de
    feed — miroir de l'enrôlement partenaire pour l'admission)."""
    events = (Event.objects
              .filter(file=rf, stage__in=(
                  Event.Stage.ADMISSION, Event.Stage.QUALIFICATION,
                  Event.Stage.ROUTING, Event.Stage.PARSING))
              .order_by('created_at', 'id'))
    last_qual = latest_qualification_event(rf)
    needs_feed = bool(
        rf.channel_id and last_qual is not None
        and last_qual.cause_code == CAUSE_FEED_NOT_FOUND)
    return {
        'id': rf.pk,
        'filename': posixpath.basename(rf.s3_key or rf.path or '') or '(sans nom)',
        'username': rf.username,
        'state': rf.state,
        'subfolder': posixpath.dirname(rf.s3_key or rf.path or '').strip('/'),
        'needs_feed': needs_feed,
        'events': [{
            'stage': e.stage,
            'control': e.control,
            'result': e.result,
            'monitoring_class': e.monitoring_class,
            'cause_code': e.cause_code,
            'detail': e.detail if isinstance(e.detail, dict) else {},
            'created_at': e.created_at.isoformat() if e.created_at else None,
        } for e in events],
    }


@staff_member_required
def admission_detail(request, pk):
    """Détail des contrôles d'admission d'un fichier (pour le support).

    Lecture seule, gardée par la session staff. Renvoie TOUS les événements de
    stage ``admission`` du fichier (chaque contrôle + le verdict), du plus ancien
    au plus récent — c'est exactement la trace que le support déroule au
    double-clic sur une ligne du board. N'altère rien (events append-only).
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    return JsonResponse(_admission_payload(rf))


@require_POST
@staff_member_required
def replay_admission(request, pk):
    """Action UI : **rejouer** l'admission d'un fichier (le mécanisme « recycle »).

    Après enrôlement d'un partenaire / autorisation d'un canal au référentiel,
    l'opérateur rejoue l'admission depuis la modale ; ``file_admission`` réémet un
    verdict (append-only) et rematérialise le rollup du board. Idempotent et sans
    effet sur ``ReceivedFile.state`` (cf. api/admission.py). Renvoie le détail
    d'admission rafraîchi (forme de ``admission_detail``) + le verdict obtenu, pour
    ré-afficher la modale et laisser le board repoller.
    """
    from .admission import file_admission

    rf = get_object_or_404(ReceivedFile, pk=pk)
    verdict = file_admission(rf.pk)
    if verdict is None:
        # file_admission avale ses exceptions et renvoie None ; voir django.log.
        return JsonResponse({'detail': 'rejeu impossible (voir les logs)'}, status=502)
    logger.info('Replay admission ReceivedFile %s (%s) -> %s par %s',
                pk, rf.s3_key, verdict, request.user)
    rf.refresh_from_db()
    payload = _admission_payload(rf)
    payload['ok'] = True
    payload['verdict'] = verdict
    return JsonResponse(payload)


@require_POST
@staff_member_required
def enrol_feed(request, pk):
    """Action UI : **enrôler la feed** d'un sous-dossier (le « recycle » de la
    qualification, miroir de l'enrôlement partenaire côté admission).

    Déclenchée depuis la modale sur un fichier **admis** dont la qualification est en
    ``recycle`` (``feed_not_found``). Crée (ou réactive/aligne) la
    ``Feed`` du couple ``(canal, sous-dossier)`` du fichier — avec une
    grammaire de nom **optionnelle** (regex ; vide = aucune contrainte) — puis
    **rejoue l'admission**, qui ré-enchaîne la qualification et rematérialise le
    rollup du board. Renvoie la trace rafraîchie (forme de ``admission_detail``).
    """
    from .models import Feed
    from .admission import file_admission

    rf = get_object_or_404(ReceivedFile, pk=pk)
    if rf.channel_id is None:
        # Pas de canal résolu ⇒ rien à qualifier ; on enrôle d'abord le partenaire.
        return JsonResponse(
            {'detail': 'fichier non admis (canal non résolu) — enrôler le partenaire d’abord'},
            status=409)

    subfolder = posixpath.dirname(rf.s3_key or rf.path or '').strip('/')
    filename_regex = (request.POST.get('filename_regex') or '').strip()
    if filename_regex:
        try:
            re.compile(filename_regex)
        except re.error as exc:
            return JsonResponse({'detail': f'regex invalide : {exc}'}, status=400)
    grammar = {'filename': filename_regex} if filename_regex else {}

    # N Feeds par (canal, sous-dossier) depuis §1.4 : la grammaire fait partie
    # de la clé d'idempotence (une ligne par motif). La Route s'assigne séparément
    # (admin) ; un enrôlement sans route → qualifié puis recycle (route_not_configured).
    nom, created = Feed.objects.get_or_create(
        channel_id=rf.channel_id, subfolder=subfolder, grammar=grammar,
        defaults={'sub_tenant_id': rf.sub_tenant_id, 'active': True})
    if not created and not nom.active:
        nom.active = True
        nom.save(update_fields=['active'])
    logger.info('Enrôlement feed #%s (canal=%s subfolder=%r created=%s) par %s',
                nom.pk, rf.channel_id, subfolder, created, request.user)

    verdict = file_admission(rf.pk)   # ré-enchaîne admission + qualification
    if verdict is None:
        return JsonResponse({'detail': 'rejeu impossible (voir les logs)'}, status=502)
    rf.refresh_from_db()
    payload = _admission_payload(rf)
    payload['ok'] = True
    payload['verdict'] = verdict
    payload['feed_created'] = created
    return JsonResponse(payload)


@require_POST
@staff_member_required
def recycle_file(request, pk):
    """Action UI : **Recycle** — **mécanisme unique** de retraitement d'un échec.

    Rejoue ``file_admission`` (ré-émet admission + qualification, append-only,
    re-dérive ``control_class``) **et** — couplage strict, hérité de l'ancien
    « Handle » qu'il remplace — pose le flag set-once ``Handled`` **ssi** le
    re-contrôle aboutit à un OK (``push``). Sinon le fichier reste en échec, sans
    flag. Garde serveur partagée avec Reject (``_triage_eligible``) : 409 sur un
    fichier OK ou déjà tranché. Le flag n'existe QUE pour un OK obtenu ici (un OK
    natif via reconcile/modale n'a pas de badge).
    """
    from .admission import file_admission

    rf = get_object_or_404(ReceivedFile, pk=pk)
    if not _triage_eligible(rf):
        return JsonResponse(
            {'detail': 'fichier non remédiable (OK ou déjà tranché)'}, status=409)
    verdict = file_admission(rf.pk)
    if verdict is None:
        return JsonResponse({'detail': 'rejeu impossible (voir les logs)'}, status=502)
    rf.refresh_from_db()
    handled = (rf.control_class == Event.MonitoringClass.PUSH)
    if handled:
        # Set-once : on ne réécrit pas un tampon existant (pas de owner volatile).
        Handled.objects.get_or_create(
            file=rf, defaults={'owner': request.user.get_username(),
                               'sub_tenant_id': rf.sub_tenant_id})
    logger.info('Recycle ReceivedFile %s (%s) -> %s handled=%s par %s',
                pk, rf.s3_key, verdict, handled, request.user)
    return JsonResponse({'ok': True, 'id': rf.pk, 'control_class': rf.control_class,
                         'verdict': verdict, 'handled': handled})


@require_POST
@staff_member_required
def reject_file(request, pk):
    """Action UI : **Reject** — décision opérateur TERMINALE sur un fichier en échec.

    Appende un Event de triage ``operator_rejected`` (append-only, audit) puis
    rematérialise la classe : le court-circuit de ``refresh_control_class`` force
    ``reject`` (le terminal humain prime sur l'actionnable ``recycle``). Garde
    serveur partagée avec Recycle (``_triage_eligible``) : refus 409 sur un fichier
    OK ou déjà tranché. Définitif (mutuellement exclusif avec Recycle ensuite).
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    if not _triage_eligible(rf):
        return JsonResponse(
            {'detail': 'fichier non remédiable (OK ou déjà tranché)'}, status=409)
    Event.objects.create(
        file=rf, sub_tenant_id=rf.sub_tenant_id, stage=Event.Stage.TRIAGE,
        control='operator_decision', result=Event.Result.FAILED,
        monitoring_class=Event.MonitoringClass.REJECT, cause_code=OPERATOR_REJECTED,
        detail={'by': request.user.get_username()})
    refresh_control_class([rf.pk])
    rf.refresh_from_db()
    logger.info('Reject (triage) ReceivedFile %s (%s) par %s', pk, rf.s3_key, request.user)
    return JsonResponse({'ok': True, 'id': rf.pk, 'control_class': rf.control_class})

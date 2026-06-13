import logging
import posixpath

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Case, Count, IntegerField, Value, When
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import ReceivedFile
from .s3 import PRESIGN_DEFAULT_EXPIRY, presigned_get_url

logger = logging.getLogger(__name__)


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

        fields = dict(
            path=p.get('path') or '',
            username=username,
            protocol=p.get('protocol') or '',
            ip=p.get('ip') or None,
            session_id=session_id,
            file_size=p.get('file_size'),
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
            ReceivedFile.objects.create(
                s3_key=s3_key,
                stored_at=timezone.now() if success else None,
                **fields,
            )
        logger.info('SFTP upload %s: %s by %s',
                    'stored' if success else 'FAILED', s3_key, username)
        # TODO: déclencher DORA checks (futur worker via flag `processed`)

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

MONITORING_FEED_LIMIT = 50  # nb de lignes renvoyées par le feed

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
    """JSON consommé par la page monitoring : N derniers fichiers + compteurs.

    Deux vues via ``?view=`` : ``live`` (défaut — flux courant receiving/stored/
    failed, échecs en tête) et ``history`` (archives deleted/missing). Lecture
    seule, gardée par la session staff (cookie du fetch same-origin) ; pas de CSRF
    (GET non mutant).
    """
    view = 'history' if request.GET.get('view') == 'history' else 'live'
    states = HISTORY_STATES if view == 'history' else LIVE_STATES
    base = ReceivedFile.objects.filter(state__in=states)

    if view == 'live':
        # Erreurs (failed) remontées en tête, puis les plus récents.
        err_first = Case(
            When(state=ReceivedFile.State.FAILED, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
        qs = list(base.annotate(_err_first=err_first)
                  .order_by('_err_first', '-received_at')[:MONITORING_FEED_LIMIT])
    else:
        qs = list(base.order_by('-received_at')[:MONITORING_FEED_LIMIT])

    def to_row(rf):
        name = posixpath.basename(rf.s3_key or rf.path or '') or '(sans nom)'
        # Durée de transfert : SFTPGo la fournit dans le payload (`elapsed`, ms).
        elapsed = rf.raw.get('elapsed') if isinstance(rf.raw, dict) else None
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
            # Actions disponibles côté UI (gardées aussi côté serveur).
            'can_archive': rf.state == ReceivedFile.State.FAILED,
            'can_restore': rf.state == ReceivedFile.State.ARCHIVED,
        }

    # Compteurs globaux par état (indexés) : pilotent les chips + le badge History.
    per_state = {s.value: 0 for s in ReceivedFile.State}
    for row in ReceivedFile.objects.values('state').annotate(n=Count('id')):
        per_state[row['state']] = row['n']
    live_total = sum(per_state[s.value] for s in LIVE_STATES)
    history_total = sum(per_state[s.value] for s in HISTORY_STATES)

    return JsonResponse({
        'view': view,
        'rows': [to_row(rf) for rf in qs],
        'per_state': per_state,
        'live_total': live_total,
        'history_total': history_total,
        'server_time': timezone.now().isoformat(),
    })

import logging
import posixpath
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

from .admission import STAGE as ADMISSION_STAGE
from .models import (
    MONITORING_SEVERITY, Event, FileTriage, ReceivedFile, TriageAck,
)
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
            rf = ReceivedFile.objects.create(
                s3_key=s3_key,
                stored_at=timezone.now() if success else None,
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
        """Appel d'admission garanti non bloquant (log + avale toute erreur)."""
        try:
            from .admission import file_admission
            file_admission(file_id)
        except Exception:
            logger.exception('Admission: échec non bloquant pour file %s', file_id)

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

    # Triage (étape 5) : par défaut on MASQUE les fichiers marqués « traités »
    # (override fichier `resolved`) → le board ne montre que ce qui reste à faire.
    # `?show_handled=1` les réaffiche (toggle UI). Le masquage s'applique AUSSI aux
    # compteurs par classe ci-dessous, pour que les chips/badge « Causes (N) » se
    # rafraîchissent quand on traite un fichier (sinon le compteur ne bouge pas).
    show_handled = request.GET.get('show_handled') == '1'

    def _hide_handled(qs):
        return qs if show_handled else qs.exclude(
            triage__status=FileTriage.Status.RESOLVED)

    # Compteurs par classe de monitoring (axe contrôles, read-model matérialisé),
    # sur la vue courante AVANT le filtre par classe → pilotent les chips. `none` =
    # aucun contrôle encore passé (control_class NULL). On compte par **vraie
    # classe** (le flag « traité » est orthogonal au verdict) : un fichier traité
    # mais encore en échec reste compté dans sa classe de problème (recycle/…) tant
    # qu'il n'a pas été re-contrôlé OK. Le masquage des traités s'applique comme à
    # la liste (chip ↔ liste cohérents). Les traités sont AUSSI comptés à part sous
    # la clé `handled` (chip vert dédié, cliquable, indépendant du toggle).
    per_control_class = {}
    for row in (_hide_handled(ReceivedFile.objects.filter(state__in=states))
                .values('control_class').annotate(n=Count('id'))):
        per_control_class[row['control_class'] or 'none'] = row['n']
    handled_count = (ReceivedFile.objects
                     .filter(state__in=states,
                             triage__status=FileTriage.Status.RESOLVED).count())
    if handled_count:
        per_control_class['handled'] = handled_count

    # Filtre par classe de monitoring (chip cliquable). Valeurs valides = les 6
    # classes + `none` (NULL) + `handled` (chip vert dédié). Autre → pas de filtre.
    valid_classes = set(Event.MonitoringClass.values)
    control_filter = request.GET.get('control')
    if control_filter == 'handled':
        # Chip « Traité » : on n'affiche QUE les traités (override du masquage par
        # défaut — sinon il n'y aurait rien à voir).
        base = base.filter(triage__status=FileTriage.Status.RESOLVED)
    else:
        base = _hide_handled(base)
        if control_filter == 'none':
            base = base.filter(control_class__isnull=True)
        elif control_filter in valid_classes:
            base = base.filter(control_class=control_filter)
        else:
            control_filter = None

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

    # Override de triage par fichier (sparse) pour les lignes de la page (étape 5).
    file_triage = dict(
        FileTriage.objects.filter(file_id__in=[rf.pk for rf in qs])
        .values_list('file_id', 'status'))

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
            # Axe contrôles : classe « worst-wins » matérialisée (read-model). Badge
            # générique du board ; le détail par contrôle est dans la modale (dbl-clic).
            'control_class': rf.control_class,
            # Triage (étape 5) : override AU NIVEAU FICHIER (None = aucun → suit la
            # cause). Pilote le badge « traité » + l'action ligne.
            'triage': file_triage.get(rf.pk),
            # Actions disponibles côté UI (gardées aussi côté serveur).
            'can_archive': rf.state == ReceivedFile.State.FAILED,
            'can_restore': rf.state == ReceivedFile.State.ARCHIVED,
        }

    # Compteurs globaux par état (indexés) : pilotent les chips + le badge History.
    # Respectent le masquage des traités (cohérence avec les lignes/compteurs visibles).
    per_state = {s.value: 0 for s in ReceivedFile.State}
    for row in _hide_handled(ReceivedFile.objects.all()).values('state').annotate(n=Count('id')):
        per_state[row['state']] = row['n']
    live_total = sum(per_state[s.value] for s in LIVE_STATES)
    history_total = sum(per_state[s.value] for s in HISTORY_STATES)

    return JsonResponse({
        'view': view,
        'rows': [to_row(rf) for rf in qs],
        'per_state': per_state,
        'per_control_class': per_control_class,
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

    # Triage (étape 5) — réconciliation cause × fichier.
    # Acks de cause existants, indexés par signature.
    acks = {(a.stage, a.control, a.monitoring_class, a.reason): a
            for a in TriageAck.objects.filter(stage=ADMISSION_STAGE)}
    # Overrides fichier `resolved` parmi les fichiers concernés (sparse).
    resolved_files = set(
        FileTriage.objects.filter(file_id__in=affected,
                                  status=FileTriage.Status.RESOLVED)
        .values_list('file_id', flat=True))

    open_files = set()   # fichiers « à traiter » après réconciliation (distincts)
    rows = []
    for key, c in causes.items():
        ack = acks.get(key)
        cause_resolved = bool(ack and ack.status == TriageAck.Status.RESOLVED)
        files = c['_files']
        n_file_resolved = sum(1 for fid in files if fid in resolved_files)
        # « À traiter » dans cette cause : 0 si la cause est résolue, sinon les
        # fichiers sans override `resolved`. Règle : override fichier OU ack de cause.
        n_open = 0 if cause_resolved else (c['count'] - n_file_resolved)
        if not cause_resolved:
            open_files.update(fid for fid in files if fid not in resolved_files)
        rows.append({
            'stage': c['stage'], 'control': c['control'],
            'monitoring_class': c['monitoring_class'], 'reason': c['reason'],
            'count': c['count'],
            'open_count': n_open,
            'file_resolved_count': n_file_resolved,
            'top_users': [{'username': u, 'count': n} for u, n in c['_users'].most_common(3)],
            'examples': [names.get(fid, str(fid)) for fid in c['_examples']],
            'triage': {
                'status': ack.status if ack else TriageAck.Status.OPEN,
                'owner': ack.owner if ack else '',
                'note': ack.note if ack else '',
            },
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


@require_POST
@staff_member_required
def triage_cause(request):
    """Upsert le triage d'une CAUSE : ``claim`` (→ en cours) / ``resolve`` (→ résolu)
    / ``reopen`` (→ ouvert). Propriétaire = utilisateur courant. Clé = signature de
    cause passée en POST. Mutable ; n'altère ni les ``Event`` ni le ``state``.
    """
    p = request.POST
    sig = dict(
        stage=(p.get('stage') or '')[:32],
        control=(p.get('control') or '')[:64],
        monitoring_class=(p.get('monitoring_class') or '')[:20],
        reason=(p.get('reason') or '')[:255],
    )
    status = {
        'claim': TriageAck.Status.IN_PROGRESS,
        'resolve': TriageAck.Status.RESOLVED,
        'reopen': TriageAck.Status.OPEN,
    }.get(p.get('action'))
    if status is None or not sig['stage'] or not sig['control']:
        return JsonResponse({'detail': 'requête de triage invalide'}, status=400)

    ack, _ = TriageAck.objects.get_or_create(**sig)
    ack.status = status
    # `open` = désassigné ; claim/resolve s'attribuent à l'utilisateur courant.
    ack.owner = '' if status == TriageAck.Status.OPEN else request.user.get_username()
    if 'note' in p:
        ack.note = (p.get('note') or '')[:2000]
    ack.save()
    logger.info('Triage cause %s/%s:%s -> %s par %s', sig['stage'], sig['control'],
                sig['reason'], status, request.user)
    return JsonResponse({'ok': True, 'status': ack.status,
                         'owner': ack.owner, 'note': ack.note})


@require_POST
@staff_member_required
def triage_file(request, pk):
    """Override de triage AU NIVEAU FICHIER : ``resolve`` (marque traité, prime sur
    l'ack de cause) ou ``reopen`` (retire l'override → le fichier suit sa cause).
    """
    rf = get_object_or_404(ReceivedFile, pk=pk)
    action = request.POST.get('action')
    if action == 'resolve':
        t, _ = FileTriage.objects.get_or_create(file=rf)
        t.status = FileTriage.Status.RESOLVED
        t.owner = request.user.get_username()
        if 'note' in request.POST:
            t.note = (request.POST.get('note') or '')[:2000]
        t.save()
        logger.info('Triage file %s -> resolved par %s', pk, request.user)
        return JsonResponse({'ok': True, 'status': t.status, 'owner': t.owner})
    if action == 'reopen':
        FileTriage.objects.filter(file=rf).delete()
        logger.info('Triage file %s -> reopen (override retiré) par %s', pk, request.user)
        return JsonResponse({'ok': True, 'status': None})
    return JsonResponse({'detail': 'action invalide'}, status=400)


def _admission_payload(rf):
    """Trace d'admission d'un fichier : tous les événements de stage ``admission``
    (chaque contrôle + le verdict), du plus ancien au plus récent. Forme partagée
    par ``admission_detail`` (lecture) et ``replay_admission`` (rejeu)."""
    events = (Event.objects
              .filter(file=rf, stage=ADMISSION_STAGE)
              .order_by('created_at', 'id'))
    return {
        'id': rf.pk,
        'filename': posixpath.basename(rf.s3_key or rf.path or '') or '(sans nom)',
        'username': rf.username,
        'state': rf.state,
        'events': [{
            'control': e.control,
            'result': e.result,
            'monitoring_class': e.monitoring_class,
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

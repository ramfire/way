"""Chaîne post-stockage d'un fichier reçu, exécutée HORS requête webhook.

Le webhook SFTPGo (``SFTPWebhookView._on_upload``) se contente de poser
``pipeline_state='pending'`` et répond 200 immédiatement. Le worker
``manage.py run_pipeline_worker`` réclame les lignes en attente
(``claim_next``) et déroule ``run_pipeline`` : admission (→ qualification →
routing → parsing) puis, si ``push``, identification et chargement
(``acta_nav.maybe_load``).

Pourquoi hors requête : un amont lent (Acta) faisait dépasser le timeout du hook
SFTPGo (20 s) ; SFTPGo REJOUE alors la notification (``retry_max``) et chaque
rejeu recréait une ligne et rechargeait le fichier (incident 2026-10-12).
"""
import logging

from django.db import transaction
from django.utils import timezone

from .models import Event, Handled, ReceivedFile

logger = logging.getLogger(__name__)

PS = ReceivedFile.PipelineState


ACTIVE_STATES = (PS.PENDING, PS.RUNNING)


def enqueue(file_id, requested_by=''):
    """Met un fichier en file (``pending``). Idempotent.

    ``requested_by`` : opérateur d'un « Recycle » ; ``process`` posera ``Handled``
    à son nom si la chaîne aboutit à ``push`` (couplage strict de l'ex-« Handle »)."""
    ReceivedFile.objects.filter(pk=file_id).update(
        pipeline_state=PS.PENDING, pipeline_started_at=None,
        pipeline_finished_at=None, pipeline_requested_by=requested_by)


def run_pipeline(file_id):
    """Admission + chaînage identification/chargement. Ne lève jamais ;
    renvoie True si la chaîne s'est déroulée sans exception.

    Séparation des stages : le chaînage admission→identification (§1.6-b) vit
    **ici, dans l'orchestrateur**, PAS dans ``file_admission``. L'identification
    n'est lancée que si l'admission aboutit à un verdict **push**
    (``control_class``) ; un recycle/reject n'identifie rien.

    Puis chargement (``acta_nav.maybe_load``) : seulement si la Route porte la
    stratégie ``acta_nav`` et que l'amont est ``push`` ; ne lève jamais."""
    try:
        from .acta_nav import maybe_load
        from .admission import file_admission, file_identification
        file_admission(file_id)
        rf = ReceivedFile.objects.get(pk=file_id)
        if rf.control_class == Event.MonitoringClass.PUSH:
            file_identification(file_id)
            maybe_load(file_id)
        return True
    except Exception:
        logger.exception(
            'Admission/identification: échec non bloquant pour file %s', file_id)
        return False


def claim_next():
    """Réclame le plus ancien fichier ``pending`` → ``running`` ; renvoie son id
    ou None. ``skip_locked`` : sûr même si plusieurs workers tournent."""
    with transaction.atomic():
        rf = (ReceivedFile.objects
              .select_for_update(skip_locked=True)
              .filter(pipeline_state=PS.PENDING)
              .order_by('stored_at', 'pk')
              .first())
        if rf is None:
            return None
        rf.pipeline_state = PS.RUNNING
        rf.pipeline_started_at = timezone.now()
        rf.save(update_fields=['pipeline_state', 'pipeline_started_at'])
        return rf.pk


def process(file_id):
    """Déroule la chaîne d'un fichier réclamé et clôt son état de file."""
    ok = run_pipeline(file_id)
    rf = ReceivedFile.objects.get(pk=file_id)
    if rf.pipeline_requested_by and rf.control_class == Event.MonitoringClass.PUSH:
        # Recycle abouti : tampon « traité » set-once (on ne réécrit pas l'owner).
        Handled.objects.get_or_create(
            file=rf, defaults={'owner': rf.pipeline_requested_by,
                               'sub_tenant_id': rf.sub_tenant_id})
    ReceivedFile.objects.filter(pk=file_id).update(
        pipeline_state=PS.DONE if ok else PS.ERROR,
        pipeline_finished_at=timezone.now(), pipeline_requested_by='')
    return ok


def requeue_orphans():
    """Remet en ``pending`` les lignes ``running`` laissées par un worker
    interrompu (arrêt brutal, OOM). À appeler au démarrage du worker unique."""
    return (ReceivedFile.objects
            .filter(pipeline_state=PS.RUNNING)
            .update(pipeline_state=PS.PENDING, pipeline_started_at=None))

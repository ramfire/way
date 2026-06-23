"""Backfill du ``Event.run_id`` (passes de traitement) + re-matérialisation du board.

Les événements antérieurs à l'introduction du ``run_id`` (0033) n'en portent pas :
on les regroupe en **passes** *a posteriori* à partir de la seule histoire ordonnée.

Heuristique de reconstruction (par fichier, events triés ``created_at, id``) : une
passe = une suite de stages où chaque stage forme un **bloc contigu** et ne
**réapparaît pas**. On démarre une nouvelle passe quand un stage déjà vu dans la
passe courante réapparaît (ex. un 2e ``admission`` = nouveau ``file_admission`` ; un
2e ``parsing`` après que d'autres stages soient passés = nouveau parse). Les events
consécutifs d'un même stage (les N contrôles d'une admission) restent dans la même
passe. Le rollup ne comparant les ``run_id`` qu'**au sein d'un même (file, stage)**,
cette reconstruction suffit à isoler les contrôles d'une version périmée du contrat.

Puis on **re-matérialise** ``ReceivedFile.control_class`` avec le nouveau rollup
(worst-wins limité à la dernière passe de chaque stage) — ce qui purge les fantômes
(cf. incident NAV_001 : ``column_type`` d'un layout périmé maintenant ``recycle``).

Idempotent ; reverse = repasse tous les ``run_id`` à ``NULL`` (sans toucher au board).
"""
import uuid

from django.db import migrations

# Copies GELÉES (la migration ne doit pas dépendre des constantes vivantes du modèle).
MONITORING_SEVERITY = {
    'blocking': 50, 'warning_action': 40, 'recycle': 30,
    'reject': 20, 'warning_noaction': 10, 'push': 0,
}
OPERATOR_REJECTED = 'operator_rejected'


def _assign_runs(events):
    """``[event...]`` (triés) → ``{event.id: run_id}`` (uuid hex par passe)."""
    runs = {}
    seen = set()
    prev_stage = None
    run_id = None
    for e in events:
        st = e.stage
        if prev_stage is None or (st != prev_stage and st in seen):
            run_id = uuid.uuid4().hex      # nouvelle passe
            seen = {st}
        elif st != prev_stage:
            seen.add(st)
        runs[e.id] = run_id
        prev_stage = st
    return runs


def _worst_class(events, runs, rejected):
    """Rollup worst-wins limité à la dernière passe de chaque stage (+ court-circuit
    Reject opérateur). ``events`` = events d'UN fichier, triés ``created_at, id``."""
    if events and events[0].file_id in rejected:
        return 'reject'
    # run_id courant de chaque stage = celui du dernier event du stage.
    latest_run = {}
    for e in events:
        latest_run[e.stage] = runs[e.id]   # écrasé jusqu'au dernier (events triés ASC)
    best_sev, best_cls = None, None
    for e in events:
        if runs[e.id] != latest_run[e.stage]:
            continue
        sev = MONITORING_SEVERITY.get(e.monitoring_class, -1)
        if best_sev is None or sev > best_sev:
            best_sev, best_cls = sev, e.monitoring_class
    return best_cls


def backfill(apps, schema_editor):
    Event = apps.get_model('api', 'Event')
    ReceivedFile = apps.get_model('api', 'ReceivedFile')

    # Fichiers rejetés définitivement (court-circuit terminal du rollup).
    rejected = set(Event.objects
                   .filter(stage='triage', cause_code=OPERATOR_REJECTED)
                   .values_list('file_id', flat=True))

    file_ids = list(Event.objects.values_list('file_id', flat=True).distinct())
    for fid in file_ids:
        events = list(Event.objects.filter(file_id=fid).order_by('created_at', 'id'))
        if not events:
            continue
        runs = _assign_runs(events)
        # Écriture des run_id (un UPDATE par event ; volumétrie audit modeste).
        for e in events:
            if e.run_id != runs[e.id]:
                e.run_id = runs[e.id]
                e.save(update_fields=['run_id'])
        # Re-matérialisation du read-model board pour ce fichier.
        cls = _worst_class(events, runs, rejected)
        ReceivedFile.objects.filter(pk=fid).update(control_class=cls)


def clear(apps, schema_editor):
    Event = apps.get_model('api', 'Event')
    Event.objects.update(run_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0033_event_run_id'),
    ]

    operations = [
        migrations.RunPython(backfill, clear),
    ]

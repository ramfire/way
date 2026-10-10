import api.models
import django.db.models.deletion
from django.db import migrations, models


def backfill_handled(apps, schema_editor):
    """Réduit l'ex-``FileTriage`` au tampon set-once ``Handled``.

    - ne garde que les lignes ``status='resolved'`` (les seules sémantiquement
      valides : le flag « traité » n'existe que pour un OK obtenu via Handle) ;
    - ``handled_at`` reprend l'``updated_at`` (instant du Handle) ;
    - ``sub_tenant`` est copié depuis le fichier (déjà backfillé, étape 1).
    """
    Handled = apps.get_model('api', 'Handled')
    Handled.objects.exclude(status='resolved').delete()
    for h in Handled.objects.select_related('file').all():
        h.handled_at = h.updated_at
        h.sub_tenant_id = h.file.sub_tenant_id
        h.save(update_fields=['handled_at', 'sub_tenant'])


def noop(apps, schema_editor):
    """Irréversible côté données (purge) ; reverse = no-op."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0013_event_cause_code_and_stage_choices'),
    ]

    operations = [
        # 1. Renommage du modèle (table api_filetriage → api_handled).
        migrations.RenameModel(old_name='FileTriage', new_name='Handled'),
        # 2. related_name 'triage' → 'handled' (état seul, pas de SQL).
        migrations.AlterField(
            model_name='handled',
            name='file',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='handled', to='api.receivedfile'),
        ),
        # 3. Nouveaux champs, nullables le temps du backfill.
        migrations.AddField(
            model_name='handled',
            name='sub_tenant',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='handled', to='api.subtenant'),
        ),
        migrations.AddField(
            model_name='handled',
            name='handled_at',
            field=models.DateTimeField(null=True),
        ),
        # 4. Purge des lignes non-resolved + backfill handled_at / sub_tenant.
        migrations.RunPython(backfill_handled, noop),
        # 5. Suppression des champs mutables (statut/note) et des timestamps legacy.
        migrations.RemoveField(model_name='handled', name='status'),
        migrations.RemoveField(model_name='handled', name='note'),
        migrations.RemoveField(model_name='handled', name='created_at'),
        migrations.RemoveField(model_name='handled', name='updated_at'),
        # 6. Verrouillage NOT NULL des nouveaux champs (données backfillées).
        migrations.AlterField(
            model_name='handled',
            name='sub_tenant',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='handled', to='api.subtenant'),
        ),
        migrations.AlterField(
            model_name='handled',
            name='handled_at',
            field=models.DateTimeField(db_index=True, default=api.models.now_ms),
        ),
        # 7. Suppression du triage par cause (feature retirée).
        migrations.DeleteModel(name='TriageAck'),
    ]

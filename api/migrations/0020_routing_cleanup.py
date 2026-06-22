"""Nettoyage des données avant le repivot du routing (§1.4) vers Nomenclature→Route.

Le routing par tokens (Option B : RoutePattern + tokens) est abandonné au profit du
binding **Nomenclature → Route** (Option A fine). On vide les données dépendantes
AVANT le changement de schéma (0021) pour éviter les blocages PROTECT/NOT NULL :
  1. ``ReceivedFile.route = None`` partout (lève les FK PROTECT vers Route) ;
  2. suppression de tous les ``RoutePattern`` (modèle supprimé en 0021) ;
  3. suppression de toutes les ``Route`` (reshapées en 0021 : partner→sub_tenant).
Les Routes/Nomenclatures de tee sont re-seedées proprement en 0022. Reverse = no-op.
"""
from django.db import migrations


def cleanup(apps, schema_editor):
    ReceivedFile = apps.get_model('api', 'ReceivedFile')
    RoutePattern = apps.get_model('api', 'RoutePattern')
    Route = apps.get_model('api', 'Route')

    ReceivedFile.objects.exclude(route__isnull=True).update(route=None)
    RoutePattern.objects.all().delete()
    Route.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0019_remove_route_uniq_route_subtenant_code_and_more'),
    ]

    operations = [
        migrations.RunPython(cleanup, migrations.RunPython.noop),
    ]

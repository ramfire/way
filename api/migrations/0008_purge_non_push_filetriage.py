from django.db import migrations


def purge_non_push_resolved(apps, schema_editor):
    """Couplage strict (option B) : un flag « traité » n'existe QUE pour un OK
    (``control_class == push``) obtenu via Handle. La v0.2 posait le flag même sur un
    fichier encore en échec → ces lignes sont désormais invalides (elles seraient
    masquées à tort et gonfleraient le chip « traité » au-delà des OK). On les purge.
    """
    FileTriage = apps.get_model('api', 'FileTriage')
    (FileTriage.objects
     .filter(status='resolved')
     .exclude(file__control_class='push')
     .delete())


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0007_filetriage_triageack'),
    ]

    operations = [
        migrations.RunPython(purge_non_push_resolved, migrations.RunPython.noop),
    ]

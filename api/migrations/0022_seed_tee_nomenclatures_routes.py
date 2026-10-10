"""Seed du routing Nomenclature→Route (§1.4) pour le partenaire réel ``tee``.

Remplace l'ancienne grammaire lâche unique (racine) par **3 Nomenclatures fines**
(une par forme), chacune portant sa Route. ``tee`` dépose à la racine ; les 3 motifs
sont mutuellement exclusifs par extension (aucune ambiguïté) :

    .*\\.csv      → Route tee-csv
    .*\\.txt      → Route tee-txt
    TI.*\\.dat    → Route tee-dat

Les fichiers ``.log`` (et ``.dat`` non-``TI``) ne matchent aucune Nomenclature →
``recycle`` (un humain tranche), conformément au principe « le moteur ne reject pas ».

Idempotent et **défensif** : no-op si le partenaire/canal ``tee`` n'existe pas
(base de test, autres environnements). Reverse = no-op.
"""
from django.db import migrations

OLD_LOOSE_GRAMMAR = {'filename': '(?:.*\\.csv|TI.*\\.dat|.*\\.txt)'}

# (code Route, data_type, regex de grammaire, libellé).
TEE_SPECS = [
    ('tee-csv', 'csv', r'.*\.csv', 'tee — fichiers CSV (racine)'),
    ('tee-txt', 'txt', r'.*\.txt', 'tee — fichiers TXT (racine)'),
    ('tee-dat', 'dat', r'TI.*\.dat', 'tee — fichiers TI*.dat (racine)'),
]


def seed(apps, schema_editor):
    Partner = apps.get_model('api', 'Partner')
    Channel = apps.get_model('api', 'Channel')
    Route = apps.get_model('api', 'Route')
    Nomenclature = apps.get_model('api', 'Nomenclature')

    partner = Partner.objects.filter(code='tee').first()
    channel = Channel.objects.filter(kind='sftp', identifier='tee').first()
    if partner is None or channel is None:
        return
    st_id = partner.sub_tenant_id

    # Retire l'ancienne Nomenclature lâche (racine) si elle traîne encore.
    Nomenclature.objects.filter(
        channel=channel, subfolder='', grammar=OLD_LOOSE_GRAMMAR).delete()

    for code, data_type, regex, label in TEE_SPECS:
        route, _ = Route.objects.get_or_create(
            sub_tenant_id=st_id, code=code,
            defaults={'label': label, 'data_type': data_type,
                      'layout_version': 1, 'active': True},
        )
        Nomenclature.objects.get_or_create(
            channel=channel, subfolder='', grammar={'filename': regex},
            defaults={'sub_tenant_id': st_id, 'route': route,
                      'active': True, 'priority': 0},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0021_remove_routepattern_route_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]

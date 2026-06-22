"""Seed du routing (§1.4) pour le partenaire réel ``tee`` — cas 2 du livret.

``tee`` dépose tout à la **racine** sous une grammaire de conformité lâche
(``(?:.*\\.csv|TI.*\\.dat|.*\\.txt)``) qui agrège 3 formes distinctes (csv / txt /
TI.dat). Le subfolder ne discrimine pas (tout est à plat) : on route donc sur le
token ``ext`` (signal de forme produit par la qualification, Option B). Une Route
par forme — chacune portera son propre ``layout``/loader au stage de load (§1.5).

Idempotent et **défensif** : ne fait rien si le partenaire ``tee`` n'existe pas
(autres environnements, base de test). Normalement les routes se gèrent en admin ;
ce seed n'existe que pour rendre le cas 2 reproductible. Reverse = no-op (on ne
supprime pas des routes potentiellement référencées par des ``ReceivedFile``).
"""
from django.db import migrations

# (code Route, data_type/ext routé, libellé).
TEE_ROUTES = [
    ('tee-csv', 'csv', 'tee — fichiers CSV (racine)'),
    ('tee-txt', 'txt', 'tee — fichiers TXT (racine)'),
    ('tee-dat', 'dat', 'tee — fichiers TI*.dat (racine)'),
]


def seed_tee_routes(apps, schema_editor):
    Partner = apps.get_model('api', 'Partner')
    Route = apps.get_model('api', 'Route')
    RoutePattern = apps.get_model('api', 'RoutePattern')

    partner = Partner.objects.filter(code='tee').first()
    if partner is None:
        return  # rien à seeder hors de l'environnement réel
    st_id = partner.sub_tenant_id

    for code, ext, label in TEE_ROUTES:
        route, _ = Route.objects.get_or_create(
            sub_tenant_id=st_id, code=code,
            defaults={'partner_id': partner.pk, 'label': label,
                      'data_type': ext, 'layout_version': 1, 'active': True},
        )
        RoutePattern.objects.get_or_create(
            route=route, match={'ext': ext},
            defaults={'sub_tenant_id': st_id, 'priority': 10, 'active': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0017_alter_event_stage_route_receivedfile_route_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_tee_routes, migrations.RunPython.noop),
    ]

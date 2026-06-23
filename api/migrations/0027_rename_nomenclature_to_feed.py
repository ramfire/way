"""Renomme le modèle ``Nomenclature`` en ``Feed`` (rename pur, sans perte).

Aucun champ FK ne s'appelait ``nomenclature`` (la relation va dans l'autre sens :
``Nomenclature`` → Channel/SubTenant/Route), donc **pas de ``RenameField``**. La
table ``api_nomenclature`` est renommée en ``api_feed`` par ``RenameModel`` (pas de
``db_table`` explicite) ; ``sub_tenant_id`` (colonne + index) est intégralement
préservé. Les ``AlterField`` ne portent que le ``related_name`` (``nomenclatures`` →
``feeds``) — purement déclaratif, **aucun SQL**.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0026_alter_event_stage'),
    ]

    operations = [
        migrations.RenameModel(old_name='Nomenclature', new_name='Feed'),
        migrations.AlterField(
            model_name='feed',
            name='channel',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='feeds', to='api.channel'),
        ),
        migrations.AlterField(
            model_name='feed',
            name='sub_tenant',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='feeds', to='api.subtenant'),
        ),
        migrations.AlterField(
            model_name='feed',
            name='route',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='feeds', to='api.route'),
        ),
    ]

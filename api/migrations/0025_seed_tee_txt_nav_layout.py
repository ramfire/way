"""Déclare le ``layout`` de décodage (§1.5) de la famille NAV ``tee`` (fichiers .txt).

Premier onboarding d'une spec de décodage : on pose le ``layout`` sur la Nomenclature
``tee`` / racine / ``.*\\.txt`` (id local 3), d'après la structure réelle observée
(``tee/NAV_02.txt``) : **TSV** (tabulation), UTF-8, avec une **ligne d'en-tête** de
23 colonnes nommées. Aucun ``control_total`` (le fichier n'en porte pas).

Config pure : **rien ne lit encore** ce ``layout`` (le moteur de parsing §1.5 viendra
plus tard). Match **défensif** par (canal ``tee``, racine, grammaire ``.*\\.txt``) — pas
par id, fragile entre environnements ; no-op si le canal/la Nomenclature n'existe pas.
Idempotent. Reverse = remet ``layout = {}`` (« pas encore déclaré »).
"""
from django.db import migrations

# Sélecteur de la Nomenclature cible (idem seed 0022 : grammaire .txt racine de tee).
TEE_TXT_GRAMMAR = {'filename': r'.*\.txt'}

# Spec de décodage de la famille NAV (forme conforme à ``validate_layout``).
NAV_LAYOUT = {
    'format': 'csv',
    'delimiter': '\t',
    'encoding': 'utf-8',
    'header': {
        'present': True,
        'columns': [
            'PortfolioCode', 'PortfolioFrequency', 'EntityName', 'PortfolioName',
            'PortfolioCurrency', 'ShareClassCode', 'ShareClassName', 'InstrumentIsin',
            'InstrumentName', 'InstrumentCurrency', 'NavDate', 'ShareOutstanding',
            'SharePricePortfolioCurrency', 'SharePriceShareCurrency',
            'ShareTnaportfolioCurrency', 'ShareTnashareCurrency', 'AumNav',
            'SubscriptionPortfolioCurrency', 'SubscriptionShareCurrency',
            'RedemptionPortfolioCurrency', 'RedemptionShareCurrency',
            'DividendPortfolioCurrency', 'DividendShareClassCurrency',
        ],
    },
}


def _tee_txt_qs(apps):
    """Nomenclature ``.txt`` racine du canal ``tee`` (queryset, possiblement vide)."""
    Channel = apps.get_model('api', 'Channel')
    Nomenclature = apps.get_model('api', 'Nomenclature')
    channel = Channel.objects.filter(kind='sftp', identifier='tee').first()
    if channel is None:
        return Nomenclature.objects.none()
    return Nomenclature.objects.filter(
        channel=channel, subfolder='', grammar=TEE_TXT_GRAMMAR)


def set_layout(apps, schema_editor):
    _tee_txt_qs(apps).update(layout=NAV_LAYOUT)


def clear_layout(apps, schema_editor):
    _tee_txt_qs(apps).update(layout={})


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0024_nomenclature_can_be_empty_nomenclature_layout'),
    ]

    operations = [
        migrations.RunPython(set_layout, clear_layout),
    ]

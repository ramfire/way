from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    BusinessCalendar, CalendarException, CalendarHoliday,
    Channel, Event, Handled, Feed, IdentificationProfile, IdentificationRule,
    NavCalendarEntry, Partner, ReceivedFile, Referential, ReferentialEntry, Route,
    SubFund, SubFundAlias, SubTenant,
)

# Ordre d'affichage des modèles dans l'index admin (par défaut alphabétique).
# On suit la hiérarchie du référentiel — tenant → partner → channel →
# feed → route — puis le runtime/journal (fichiers, événements, traités).
# But explicite : remonter « Sub tenants » tout en haut, au-dessus de « Channels ».
_MODEL_ORDER = [
    'SubTenant', 'Partner', 'Channel', 'Feed', 'Route',
    'ReceivedFile', 'Event', 'Handled',
]
_ORDER_INDEX = {name: i for i, name in enumerate(_MODEL_ORDER)}
_default_get_app_list = admin.site.get_app_list


def _ordered_get_app_list(request, app_label=None):
    """Surcharge ``AdminSite.get_app_list`` : trie les modèles selon ``_MODEL_ORDER``."""
    app_list = _default_get_app_list(request, app_label)
    for app in app_list:
        app['models'].sort(
            key=lambda m: _ORDER_INDEX.get(m['object_name'], len(_ORDER_INDEX)))
    return app_list


admin.site.get_app_list = _ordered_get_app_list


@admin.register(ReceivedFile)
class ReceivedFileAdmin(admin.ModelAdmin):
    list_display = (
        's3_key', 'username', 'state', 'file_size', 'protocol',
        'received_at', 'stored_at', 'download',
    )
    list_filter = ('state', 'protocol', 'bucket', 'username')
    search_fields = ('s3_key', 'path', 'username', 'session_id', 'ip')
    date_hierarchy = 'received_at'
    ordering = ('-received_at',)
    change_list_template = 'admin/receivedfile_changelist.html'
    readonly_fields = [f.name for f in ReceivedFile._meta.fields] + ['download']

    @admin.display(description='Téléchargement')
    def download(self, obj):
        """Lien vers le proxy authentifié → URL pré-signée (15 min)."""
        if obj.state != ReceivedFile.State.STORED:
            return '—'
        return format_html(
            '<a href="{}">⬇ Télécharger</a>', reverse('download-file', args=[obj.pk]),
        )


@admin.register(SubTenant)
class SubTenantAdmin(admin.ModelAdmin):
    """Locataire de premier niveau (éditable : enrôlement manuel)."""
    list_display = ('code', 'name')
    search_fields = ('code', 'name')
    ordering = ('code',)


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    """Référentiel partenaires : c'est ici qu'un humain **enrôle**/déclare un
    partenaire (modèle discovery), puis re-lance l'admission du fichier en attente.
    """
    list_display = ('code', 'status', 'sub_tenant')
    list_filter = ('status', 'sub_tenant')
    search_fields = ('code',)
    ordering = ('code',)


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    """Canaux d'arrivée (éditable) : l'``identifier`` porte la résolution."""
    list_display = ('kind', 'identifier', 'partner', 'sub_tenant', 'active')
    list_filter = ('kind', 'active', 'sub_tenant')
    search_fields = ('identifier',)
    ordering = ('kind', 'identifier')


@admin.register(Feed)
class FeedAdmin(admin.ModelAdmin):
    """Contrats de nommage (éditable) : grammaire + Route portée (§1.4)."""
    list_display = (
        'channel', 'subfolder', 'route', 'identification_profile',
        'priority', 'active',
    )
    list_filter = ('active', 'sub_tenant', 'route')
    list_select_related = ('channel', 'route', 'identification_profile')
    search_fields = ('subfolder',)
    autocomplete_fields = ('route',)


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    """Descripteur de traitement réutilisable (§1.4), référencé par les Feeds.

    Configurable à la main (pas d'UI IAM pour l'instant — cf. data_owner provisoire).
    Descripteur transverse (non scopé locataire).
    """
    list_display = (
        'code', 'data_type', 'business_domain',
        'layout_version', 'active',
    )
    list_filter = ('active', 'business_domain', 'data_type')
    search_fields = ('code', 'label', 'data_owner')
    ordering = ('code',)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Journal d'audit append-only : **lecture seule** (aucun ajout/modif/suppr)."""
    list_display = (
        'created_at', 'file', 'stage', 'control', 'result', 'monitoring_class',
    )
    list_filter = ('stage', 'result', 'monitoring_class', 'control')
    search_fields = ('file__s3_key', 'file__username')
    date_hierarchy = 'created_at'
    ordering = ('-created_at', '-id')
    readonly_fields = [f.name for f in Event._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Handled)
class HandledAdmin(admin.ModelAdmin):
    """Tampon « traité » set-once au niveau fichier : **lecture seule**."""
    list_display = ('file', 'owner', 'handled_at', 'sub_tenant')
    list_filter = ('sub_tenant',)
    search_fields = ('file__s3_key', 'file__username', 'owner')
    ordering = ('-handled_at',)
    readonly_fields = [f.name for f in Handled._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BusinessCalendar)
class BusinessCalendarAdmin(admin.ModelAdmin):
    list_display = ('code', 'label', 'sub_tenant')


@admin.register(CalendarHoliday)
class CalendarHolidayAdmin(admin.ModelAdmin):
    list_display = ('date', 'label', 'business_calendar', 'is_bank_holiday')
    list_filter = ('business_calendar', 'is_bank_holiday')
    date_hierarchy = 'date'


@admin.register(CalendarException)
class CalendarExceptionAdmin(admin.ModelAdmin):
    list_display = ('date', 'business_calendar', 'is_open', 'reason')


@admin.register(Referential)
class ReferentialAdmin(admin.ModelAdmin):
    """Méta-référentiel (§1.6-a) : le Steward ajuste candidate/anomaly et ajoute
    country/currency/… comme nouvelles lignes (liste ouverte)."""
    list_display = ('code', 'label', 'absence_policy', 'sub_tenant')
    list_filter = ('absence_policy',)


class SubFundAliasInline(admin.TabularInline):
    """Alias provider → ce compartiment (§1.6-a-ter), édités sur la fiche SubFund."""
    model = SubFundAlias
    fields = ('feed', 'external_code', 'sub_tenant')
    extra = 0


class NavCalendarEntryInline(admin.TabularInline):
    """Attente VNI de ce compartiment (§3.7), éditée sur la fiche SubFund."""
    model = NavCalendarEntry
    fields = ('cadence', 'lag', 'deadline_time', 'business_calendar', 'status',
              'sub_tenant')
    extra = 0


@admin.register(SubFund)
class SubFundAdmin(admin.ModelAdmin):
    """Référentiel pivot (§1.6-a) : onboarding manuel d'un sous-fonds."""
    list_display = ('key', 'status', 'sub_tenant')
    list_filter = ('status',)
    search_fields = ('key',)
    inlines = [SubFundAliasInline, NavCalendarEntryInline]


@admin.register(NavCalendarEntry)
class NavCalendarEntryAdmin(admin.ModelAdmin):
    """Calendrier VNI déclaratif par compartiment (§3.7) : le Steward saisit
    cadence / lag (jours ouvrés) / heure limite. Aucun calcul ici (différé §3.8)."""
    list_display = ('sub_fund', 'cadence', 'lag', 'deadline_time', 'status',
                    'sub_tenant')
    list_filter = ('cadence', 'status', 'sub_tenant')
    search_fields = ('sub_fund__key',)


@admin.register(SubFundAlias)
class SubFundAliasAdmin(admin.ModelAdmin):
    """Alias code externe provider → SubFund canonique, scopé par Feed (§1.6-a-ter)."""
    list_display = ('external_code', 'sub_fund', 'feed', 'sub_tenant')
    list_filter = ('feed', 'sub_tenant')
    search_fields = ('external_code', 'sub_fund__key')


@admin.register(ReferentialEntry)
class ReferentialEntryAdmin(admin.ModelAdmin):
    """Valeurs des référentiels subordonnés (§1.6-a-bis) : saisie Steward."""
    list_display = ('key', 'referential', 'status', 'sub_tenant')
    list_filter = ('referential', 'status')
    search_fields = ('key',)


class IdentificationRuleInline(admin.TabularInline):
    model = IdentificationRule
    fields = ('field', 'referential', 'role', 'required')
    extra = 0


@admin.register(IdentificationProfile)
class IdentificationProfileAdmin(admin.ModelAdmin):
    """Descripteur d'identification (§1.6-a-bis) : règles éditables en inline."""
    list_display = ('file_type', 'label', 'sub_tenant')
    inlines = [IdentificationRuleInline]

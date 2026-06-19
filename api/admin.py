from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    Channel, Event, Handled, Nomenclature, Partner, ReceivedFile, SubTenant,
)


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


@admin.register(Nomenclature)
class NomenclatureAdmin(admin.ModelAdmin):
    """Grammaires de sous-dossiers (éditable ; sert la qualification à venir)."""
    list_display = ('channel', 'subfolder', 'active')
    list_filter = ('active', 'sub_tenant')
    search_fields = ('subfolder',)


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

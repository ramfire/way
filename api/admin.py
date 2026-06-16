from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Event, FileTriage, Partner, ReceivedFile, TriageAck


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


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    """Référentiel partenaires : c'est ici qu'un humain **enrôle**/déclare un
    partenaire (modèle discovery), puis re-lance l'admission du fichier en attente.
    """
    list_display = ('username', 'status')
    list_filter = ('status',)
    search_fields = ('username',)
    ordering = ('username',)


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


@admin.register(TriageAck)
class TriageAckAdmin(admin.ModelAdmin):
    """Triage humain d'une cause (statut + propriétaire). Mutable."""
    list_display = ('stage', 'control', 'monitoring_class', 'reason',
                    'status', 'owner', 'updated_at')
    list_filter = ('status', 'monitoring_class', 'stage', 'control')
    search_fields = ('reason', 'owner')
    ordering = ('-updated_at',)


@admin.register(FileTriage)
class FileTriageAdmin(admin.ModelAdmin):
    """Override de triage au niveau fichier (exception)."""
    list_display = ('file', 'status', 'owner', 'updated_at')
    list_filter = ('status',)
    search_fields = ('file__s3_key', 'file__username', 'owner')
    ordering = ('-updated_at',)

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import ReceivedFile


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

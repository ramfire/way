from django.db import models


class ReceivedFile(models.Model):
    """Métadonnées d'un fichier reçu via les hooks SFTPGo (direct-S3, option C).

    Cycle de vie tracé en deux temps autour de l'écriture S3 :
      - hook ``pre-upload`` → ligne créée en état ``receiving`` (avant S3) ;
      - hook ``upload`` (post) → passage en ``stored`` (succès) ou ``failed``.
    ``raw`` conserve le JSON complet pour rester robuste si SFTPGo évolue.
    """

    class State(models.TextChoices):
        RECEIVING = 'receiving', 'Réception (avant S3)'
        STORED = 'stored', 'Enregistré dans S3'
        FAILED = 'failed', 'Échec'
        DELETED = 'deleted', 'Supprimé via SFTP'
        MISSING = 'missing', 'Absent de S3 (drift réconciliation)'

    state = models.CharField(
        max_length=16, choices=State.choices, default=State.RECEIVING, db_index=True,
    )

    # Identité du fichier
    s3_key = models.CharField(max_length=1024, db_index=True)  # virtual_path
    path = models.CharField(max_length=1024, blank=True, default='')  # chemin physique

    # Auteur / transport
    username = models.CharField(max_length=255, db_index=True)
    protocol = models.CharField(max_length=16, blank=True, default='')  # SFTP/FTP/HTTP
    ip = models.GenericIPAddressField(null=True, blank=True)
    session_id = models.CharField(max_length=128, blank=True, default='')

    # Caractéristiques
    file_size = models.BigIntegerField(null=True, blank=True)
    status = models.IntegerField(null=True, blank=True)  # 1 = OK côté SFTPGo
    bucket = models.CharField(max_length=255, blank=True, default='')
    action = models.CharField(max_length=32, blank=True, default='')
    sftpgo_timestamp = models.BigIntegerField(null=True, blank=True)  # epoch ns

    # Suivi interne (futur worker sync S3 / DORA)
    processed = models.BooleanField(default=False, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)  # pre-upload
    stored_at = models.DateTimeField(null=True, blank=True)  # confirmation S3 (post)
    deleted_at = models.DateTimeField(null=True, blank=True)  # delete SFTP / drift S3
    # True quand la ligne provient d'un backfill `reconcile_files` (et non d'un hook).
    reconciled = models.BooleanField(default=False, db_index=True)

    # Filet de sécurité : payload brut complet
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['username', '-received_at']),
        ]

    def __str__(self):
        return f'{self.s3_key} ({self.username})'

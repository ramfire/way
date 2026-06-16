from django.db import models
from django.utils import timezone


def now_ms():
    """``timezone.now()`` tronqué à la milliseconde (UTC).

    Les événements d'audit (``Event.created_at``) sont horodatés en précision
    milliseconde : PostgreSQL stocke la microseconde, on tronque volontairement
    pour une granularité stable et un départage déterministe par ``-id``.
    """
    t = timezone.now()
    return t.replace(microsecond=(t.microsecond // 1000) * 1000)


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
        ARCHIVED = 'archived', 'Archivé (sorti du board Live)'

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
    archived_at = models.DateTimeField(null=True, blank=True)  # archivage manuel (failed traité)
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


class Partner(models.Model):
    """Référentiel des partenaires, indexé sur le ``username`` SFTPGo.

    Modèle *discovery* : **l'absence de ligne = « non mappé »** (à arbitrer par
    un humain). AlfaWay ne crée JAMAIS une ligne automatiquement. Il n'y a
    volontairement **pas** de statut ``initialisation`` : la milestone d'init est
    dérivée des événements d'admission (cf. ``Event``), pas d'un état ici.
    Un seul champ métier : ``status``.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Actif'
        REVOKED = 'revoked', 'Révoqué'

    username = models.CharField(max_length=255, unique=True, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )

    def __str__(self):
        return f'{self.username} ({self.status})'


class Event(models.Model):
    """Journal de traçabilité (append-only) du cycle de vie d'un fichier.

    Colonne vertébrale de **toutes** les étapes futures (admission, qualification,
    routage, parsing…) ; l'admission est juste le premier producteur. Un événement
    = le verdict d'un contrôle à un instant donné. **Append-only / audit** : on
    n'update ni ne supprime jamais. L'« état d'admission » courant d'un fichier est
    son **dernier** événement de stage ``admission``.
    """

    class Result(models.TextChoices):
        PASSED = 'passed', 'Réussi'
        FAILED = 'failed', 'Échoué'

    class MonitoringClass(models.TextChoices):
        BLOCKING = 'blocking', 'Bloquant'
        WARNING_ACTION = 'warning_action', 'Avertissement (action requise)'
        WARNING_NOACTION = 'warning_noaction', 'Avertissement (sans action)'
        REJECT = 'reject', 'Rejet (archivé, non retraité)'
        RECYCLE = 'recycle', 'Recyclage (retraitable)'
        PUSH = 'push', 'Confirmation (poussé)'

    file = models.ForeignKey(
        ReceivedFile, on_delete=models.CASCADE, related_name='events',
    )
    stage = models.CharField(max_length=32, db_index=True)  # ex: "admission"
    control = models.CharField(max_length=64)               # nom du contrôle
    result = models.CharField(max_length=16, choices=Result.choices)
    monitoring_class = models.CharField(
        max_length=20, choices=MonitoringClass.choices, db_index=True,
    )
    created_at = models.DateTimeField(default=now_ms, db_index=True)  # UTC, ms
    detail = models.JSONField(default=dict, blank=True)  # raison, version réf., etc.

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            # Récupération par fichier (le « dernier » événement d'un stage).
            models.Index(fields=['file', 'stage', '-created_at']),
            # Récupération par classe de monitoring (alimentation du board).
            models.Index(fields=['monitoring_class']),
        ]

    def __str__(self):
        return f'{self.stage}/{self.control}={self.result} (file {self.file_id})'

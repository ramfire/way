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

    # Read-model matérialisé de l'axe contrôles : classe de monitoring « worst-wins »
    # (la plus sévère parmi l'état COURANT des contrôles du fichier, tous stages
    # confondus). Dérivé des `Event` ; rafraîchi par `refresh_control_class()` après
    # chaque émission de contrôle (admission aujourd'hui, DORA demain). NULL = aucun
    # contrôle encore passé. Sert le filtre/tri/agrégation côté serveur du board
    # (cf. docs/admission-monitoring-design.md §6, étape 3). Pas de `choices` : valeur
    # contrainte par le code (clés de MONITORING_SEVERITY / Event.MonitoringClass).
    control_class = models.CharField(max_length=20, null=True, blank=True, db_index=True)

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


class TriageAck(models.Model):
    """Triage humain d'une **cause** (cf. ``monitoring_causes``) : statut +
    propriétaire + note. **Mutable**, et délibérément **distinct** du journal
    append-only ``Event`` (qui n'est QUE de l'observation) — c'est une décision
    humaine, pas une observation. Clé = **signature de cause**
    ``(stage, control, monitoring_class, reason)``. On traite la cause (corrige le
    partenaire/canal, rejoue) → les fichiers quittent l'ensemble « en échec » seuls.
    """

    class Status(models.TextChoices):
        OPEN = 'open', 'Ouvert'
        IN_PROGRESS = 'in_progress', 'En cours'
        RESOLVED = 'resolved', 'Résolu'

    stage = models.CharField(max_length=32)
    control = models.CharField(max_length=64)
    monitoring_class = models.CharField(max_length=20)
    reason = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    owner = models.CharField(max_length=255, blank=True, default='')
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['stage', 'control', 'monitoring_class', 'reason'],
                name='uniq_triage_cause'),
        ]

    def __str__(self):
        return f'{self.stage}/{self.control}:{self.reason}={self.status}'


class FileTriage(models.Model):
    """Override de triage **au niveau fichier** (exception) : prime sur l'ack de la
    cause du fichier (cf. règle de réconciliation, docs §9). **Sparse** : une ligne
    seulement pour les fichiers explicitement triés à la main.
    """

    class Status(models.TextChoices):
        OPEN = 'open', 'Ouvert'
        RESOLVED = 'resolved', 'Traité'

    file = models.OneToOneField(
        ReceivedFile, on_delete=models.CASCADE, related_name='triage')
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RESOLVED, db_index=True)
    owner = models.CharField(max_length=255, blank=True, default='')
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'file {self.file_id} triage={self.status}'


# Sévérité des classes de monitoring pour le rollup « worst-wins » du board (un
# seul signal par fichier, toutes étapes/contrôles confondus). Board orienté
# ACTION : l'actionnable (`recycle`) prime sur le terminal (`reject`).
# Décision design 2026-06-16 — docs/admission-monitoring-design.md §5.
# Plus le nombre est grand, plus c'est sévère (remonte en tête).
MONITORING_SEVERITY = {
    Event.MonitoringClass.BLOCKING: 50,
    Event.MonitoringClass.WARNING_ACTION: 40,
    Event.MonitoringClass.RECYCLE: 30,
    Event.MonitoringClass.REJECT: 20,
    Event.MonitoringClass.WARNING_NOACTION: 10,
    Event.MonitoringClass.PUSH: 0,
}


def current_control_rollup(file_ids):
    """Rollup « worst-wins » de l'axe contrôles, par fichier (générique).

    Pour chaque fichier : on prend l'état **courant** de chacun de ses contrôles
    (= dernier ``Event`` par couple ``(stage, control)`` — append-only/rejouable),
    puis on retient la **classe de monitoring la plus sévère** parmi eux
    (``MONITORING_SEVERITY``, board orienté action). Un seul signal par fichier,
    **toutes étapes/contrôles confondus** : admission aujourd'hui, contrôles DORA
    demain, sans câbler aucun nom de contrôle/stage.

    NB : le worst-wins porte sur **tous** les contrôles, pas sur le seul ``verdict``
    — c'est ce qui fait **remonter** un signal comme le ``warning_action``
    « partenaire révoqué qui émet » (plus sévère que le verdict ``reject``), au lieu
    de l'enterrer (cf. docs/admission-monitoring-design.md §5/§7).

    Renvoie ``{file_id: {'monitoring_class', 'stage', 'control', 'result'}}`` ;
    un fichier sans aucun événement est simplement absent du mapping.
    """
    file_ids = list(file_ids)
    if not file_ids:
        return {}
    # Derniers d'abord : la 1re ligne vue pour un (file, stage, control) est l'actuelle.
    events = (Event.objects
              .filter(file_id__in=file_ids)
              .order_by('file_id', 'stage', 'control', '-created_at', '-id')
              .values('file_id', 'stage', 'control', 'monitoring_class', 'result'))
    current = {}   # (file, stage, control) -> event courant
    for e in events:
        key = (e['file_id'], e['stage'], e['control'])
        if key not in current:
            current[key] = e
    worst = {}
    for (fid, _stage, _control), e in current.items():
        sev = MONITORING_SEVERITY.get(e['monitoring_class'], -1)
        best = worst.get(fid)
        if best is None or sev > best['_severity']:
            worst[fid] = {
                '_severity': sev,
                'monitoring_class': e['monitoring_class'],
                'stage': e['stage'],
                'control': e['control'],
                'result': e['result'],
            }
    return worst


def refresh_control_class(file_ids):
    """(Re)matérialise ``ReceivedFile.control_class`` depuis les événements.

    À appeler **après toute émission de contrôle** pour un fichier (l'admission le
    fait en fin de passage). Idempotent ; un fichier sans événement repasse à NULL.
    Écrit en lots par classe (≤ 6 UPDATE) — efficace même en backfill massif.
    """
    file_ids = list(file_ids)
    if not file_ids:
        return
    rollup = current_control_rollup(file_ids)
    by_class = {}
    for fid in file_ids:
        roll = rollup.get(fid)
        by_class.setdefault(roll['monitoring_class'] if roll else None, []).append(fid)
    for cls, ids in by_class.items():
        ReceivedFile.objects.filter(pk__in=ids).update(control_class=cls)

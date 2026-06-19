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


class SubTenant(models.Model):
    """Locataire de premier niveau (entité cliente d'AlfaWay).

    Racine de l'isolation multi-tenant : Partner, Channel, Nomenclature,
    ReceivedFile, Event et Handled portent tous une FK ``sub_tenant``. ``code``
    est l'identifiant stable (court, unique) utilisé en référence ; ``name`` est
    le libellé humain. Un SubTenant par défaut (``GIL``) sert de cible de backfill
    pour les données antérieures au modèle multi-tenant.
    """

    code = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return self.code


# Locataire par défaut : tampon d'**ingest**. Au moment où un fichier arrive (hook
# pre-upload/upload) ou est rattrapé par la réconciliation, on ne connaît pas encore
# son locataire — on l'estampille ``GIL`` puis l'admission le re-pointe vers le
# locataire du canal résolu. ``sub_tenant`` étant NOT NULL, tout insert DOIT en poser
# un (sinon IntegrityError silencieuse côté webhook, cf. CLAUDE.md « Déploiement »).
DEFAULT_SUB_TENANT_CODE = 'GIL'


def default_sub_tenant_id():
    """PK du SubTenant par défaut (``GIL``), pour estampiller un fichier à l'ingest."""
    return SubTenant.objects.get(code=DEFAULT_SUB_TENANT_CODE).pk


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

    # Multi-tenant : locataire propriétaire de ce fichier (FK auto-indexée).
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='files',
    )
    # Caches de résolution (posés par l'admission). Nullable par nature : un fichier
    # d'un compte non mappé (discovery) n'a ni canal ni partenaire résolu.
    channel = models.ForeignKey(
        'Channel', on_delete=models.SET_NULL, related_name='files',
        null=True, blank=True,
    )
    partner = models.ForeignKey(
        'Partner', on_delete=models.SET_NULL, related_name='files',
        null=True, blank=True,
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

    # Identifiant métier, unique PAR locataire (cf. UniqueConstraint ci-dessous).
    code = models.CharField(max_length=255, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )
    # Multi-tenant : locataire propriétaire (FK auto-indexée).
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='partners',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sub_tenant', 'code'], name='uniq_partner_subtenant_code'),
        ]

    def __str__(self):
        return f'{self.code} ({self.status})'


class Channel(models.Model):
    """Voie d'arrivée concrète d'un partenaire (un compte SFTP, une adresse mail…).

    Un partenaire peut avoir plusieurs canaux. La **résolution** d'un fichier reçu
    part du seul ``identifier`` (ex : le ``username`` SFTPGo) : la contrainte
    d'unicité ``(kind, identifier)`` est **globale** (pas de scope sub_tenant) — le
    locataire en **découle** via le canal trouvé. ``rule`` (JSON, optionnel) porte
    une autorisation grossière (préfixes de chemin…) ; ``active`` reflète l'état.
    """

    class Kind(models.TextChoices):
        SFTP = 'sftp', 'SFTP'
        EMAIL = 'email', 'E-mail'
        WEB = 'web', 'Web'
        URL = 'url', 'URL (réservé)'

    partner = models.ForeignKey(
        Partner, on_delete=models.CASCADE, related_name='channels',
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='channels',
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    identifier = models.CharField(max_length=255, db_index=True)
    rule = models.JSONField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['kind', 'identifier'], name='uniq_channel_kind_identifier'),
        ]

    def __str__(self):
        return f'{self.kind}:{self.identifier}'


class Nomenclature(models.Model):
    """Grammaire attendue d'un sous-dossier d'un canal (sélecteur → règles).

    Sert l'étape **qualification** (incrément suivant, pas encore câblée) : pour un
    canal donné, ``subfolder`` sélectionne le jeu de règles ; ``grammar`` décrit le
    format attendu et ``mandatory`` la liste des éléments obligatoires. Table créée
    **vide** ici — l'enrôlement se fait à la main (admin)."""

    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name='nomenclatures',
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='nomenclatures',
    )
    subfolder = models.CharField(max_length=255, blank=True, default='')
    grammar = models.JSONField(default=dict, blank=True)
    mandatory = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['channel', 'subfolder'], name='uniq_nomenclature_channel_subfolder'),
        ]

    def __str__(self):
        return f'{self.channel}/{self.subfolder or "(racine)"}'


class Event(models.Model):
    """Journal de traçabilité (append-only) du cycle de vie d'un fichier.

    Colonne vertébrale de **toutes** les étapes futures (admission, qualification,
    routage, parsing…) ; l'admission est juste le premier producteur. Un événement
    = le verdict d'un contrôle à un instant donné. **Append-only / audit** : on
    n'update ni ne supprime jamais. L'« état d'admission » courant d'un fichier est
    son **dernier** événement de stage ``admission``.
    """

    class Stage(models.TextChoices):
        ADMISSION = 'admission', 'Admission'
        QUALIFICATION = 'qualification', 'Qualification'
        TRIAGE = 'triage', 'Triage (décision opérateur)'

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
    # Multi-tenant : locataire propriétaire (FK auto-indexée).
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='events',
    )
    stage = models.CharField(
        max_length=32, choices=Stage.choices, db_index=True,  # ex: "admission"
    )
    control = models.CharField(max_length=64)               # nom du contrôle
    result = models.CharField(max_length=16, choices=Result.choices)
    monitoring_class = models.CharField(
        max_length=20, choices=MonitoringClass.choices, db_index=True,
    )
    # Code de cause normalisé (réutilisé par l'agrégation « par cause » et la
    # qualification à venir). Nullable : tous les événements n'en portent pas.
    cause_code = models.CharField(max_length=64, null=True, blank=True)
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


class Handled(models.Model):
    """Tampon « traité » **au niveau fichier** — *set-once*, sans statut mutable.

    Remplace ``FileTriage`` : l'**existence** d'une ligne = le fichier a été traité
    (un Handle ayant abouti à un OK ``push``, cf. ``triage_file``). Plus de statut,
    plus de note, plus de triage par cause (``TriageAck`` supprimé). **Sparse** :
    une ligne uniquement pour les fichiers explicitement traités à la main.
    """

    file = models.OneToOneField(
        ReceivedFile, on_delete=models.CASCADE, related_name='handled')
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='handled')
    owner = models.CharField(max_length=255, blank=True, default='')
    handled_at = models.DateTimeField(default=now_ms, db_index=True)

    def __str__(self):
        return f'file {self.file_id} handled by {self.owner or "?"}'


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

# Cause d'un Event de triage « Reject opérateur » : décision humaine TERMINALE.
# Elle prime sur le worst-wins (qui place l'actionnable `recycle` au-dessus du
# terminal `reject`) — cf. court-circuit dans `refresh_control_class`.
OPERATOR_REJECTED = 'operator_rejected'


def operator_rejected_ids(file_ids):
    """Sous-ensemble des ``file_ids`` rejetés définitivement par un opérateur.

    Append-only : l'**existence** d'un Event de triage ``operator_rejected`` suffit
    (le Reject est définitif ; aucune décision de triage ultérieure ne le renverse —
    le Recycle passe par l'admission, pas par le stage ``triage``)."""
    return set(Event.objects
               .filter(file_id__in=list(file_ids), stage=Event.Stage.TRIAGE,
                       cause_code=OPERATOR_REJECTED)
               .values_list('file_id', flat=True))


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
    # Court-circuit terminal : un Reject opérateur force `reject`, en dépit du
    # worst-wins (board orienté action). C'est le SEUL nom câblé dans cette
    # matérialisation, par exception assumée — le rollup générique reste intact.
    rejected = operator_rejected_ids(file_ids)
    by_class = {}
    for fid in file_ids:
        if fid in rejected:
            cls = Event.MonitoringClass.REJECT
        else:
            roll = rollup.get(fid)
            cls = roll['monitoring_class'] if roll else None
        by_class.setdefault(cls, []).append(fid)
    for cls, ids in by_class.items():
        ReceivedFile.objects.filter(pk__in=ids).update(control_class=cls)

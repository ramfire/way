import contextvars
import uuid
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# --- Passe de traitement (« run ») -----------------------------------------
# Identifiant partagé par TOUS les Event d'une même passe de pipeline (admission
# → qualification → routing → parsing), d'une identification, ou d'une décision
# de triage. Porté par un contextvar et appliqué AUTOMATIQUEMENT comme défaut du
# champ ``Event.run_id`` (callable default évalué à la création de chaque Event),
# donc AUCUN point d'émission n'a besoin de le passer explicitement.
#
# Pourquoi : le rollup « worst-wins » du board ne doit refléter que la **dernière
# passe** de chaque stage. Sans ``run_id``, un contrôle qui cesse d'être émis
# (colonne retypée, règle retirée du contrat) fige son dernier verdict À VIE dans
# le rollup (cf. incident NAV_001 : un `column_type` échoué d'une version périmée
# du layout maintenait `control_class=recycle` alors que le fichier décode bien).
_run_id_var = contextvars.ContextVar('alfaway_run_id', default=None)


def _current_run_id():
    """Défaut du champ ``Event.run_id`` : le run courant (``None`` hors d'une passe)."""
    return _run_id_var.get()


@contextmanager
def run_scope(run_id=None):
    """Ouvre une passe : tous les ``Event`` créés dedans partagent un ``run_id``.

    **Réentrant** : un scope imbriqué hérite du run englobant (la passe externe
    prime). C'est volontaire — les stages chaînés depuis l'admission (qualif /
    routing / parsing via les variantes ``*_no_refresh``) doivent rester dans la
    MÊME passe que l'admission qui les orchestre.
    """
    existing = _run_id_var.get()
    if existing is not None:
        yield existing                      # déjà dans une passe → on hérite
        return
    token = _run_id_var.set(run_id or uuid.uuid4().hex)
    try:
        yield _run_id_var.get()
    finally:
        _run_id_var.reset(token)


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

    Racine de l'isolation multi-tenant : Partner, Channel, Feed,
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
    # Clé de dispatch posée par le stage **routing** (§1.4) : la Route portée par la
    # Feed qui a qualifié le fichier (``feed.route``). Nullable par
    # nature (fichier pas encore routé, ou route non configurée/inactive). ``PROTECT``
    # : une Route référencée ne peut être supprimée. **Jamais sticky** : recalculée OU
    # effacée à chaque rejeu de la chaîne (cf. ``api/routing.py``). FK ⇒ index auto.
    route = models.ForeignKey(
        'Route', on_delete=models.PROTECT, related_name='files',
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


def validate_layout(value):
    """Validateur **de forme** du descripteur ``layout`` (§1.5), au save.

    Spec de décodage du fichier (delimiter, encoding, header/control_total…),
    consommée **plus tard** par le parsing — ici on ne vérifie que la **forme et
    les types**, jamais une valeur métier. Volontairement **permissif** : on ne
    rejette pas les clés inconnues (le schéma se resserrera à l'onboarding des
    familles). Un descripteur malformé doit échouer au save pour qu'une erreur de
    config soit attrapée tôt, hors de l'espace recycle/reject du parsing.

    Un ``{}`` (défaut) est valide : « layout pas encore déclaré ».
    """
    if not isinstance(value, dict):
        raise ValidationError('layout must be a dict (JSON object).')
    if not value:
        return

    str_keys = ('format', 'delimiter', 'encoding')
    for key in str_keys:
        if key in value and not isinstance(value[key], str):
            raise ValidationError(f'layout.{key} must be a string.')

    if 'record_types' in value and not isinstance(value['record_types'], list):
        raise ValidationError('layout.record_types must be a list.')

    if 'header' in value:
        header = value['header']
        if not isinstance(header, dict):
            raise ValidationError('layout.header must be a dict.')
        if 'control_total' in header:
            control_total = header['control_total']
            if not isinstance(control_total, dict):
                raise ValidationError(
                    'layout.header.control_total must be a dict locating the '
                    'field (e.g. {"field": ...} or {"position": ...}).'
                )


class Feed(models.Model):
    """Contrat de nommage **fin** d'un (canal, sous-dossier) → porte sa Route (§1.4).

    Sert l'étape **qualification** : ``subfolder`` borne le périmètre, ``grammar``
    (regex de nom) **reconnaît** le fichier. Contrairement au modèle initial (1
    Feed par sous-dossier), il y a désormais **N Feeds par
    (canal, sous-dossier)** : une par motif précis (ex. ``POS*.csv`` vs ``TXN*.csv``).
    La qualification retient celle dont la grammaire matche le nom, ``priority``
    départageant un éventuel recouvrement. La Feed matchée porte la ``route``
    (clé de dispatch posée par le routing). ``mandatory`` : différé.
    """

    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, related_name='feeds',
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='feeds',
    )
    subfolder = models.CharField(max_length=255, blank=True, default='')
    grammar = models.JSONField(default=dict, blank=True)
    mandatory = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    # Départage si plusieurs grammaires matchent le même nom (DESC ; le + haut gagne).
    priority = models.IntegerField(default=0)
    # Route portée par ce contrat (§1.4). Nullable = pas encore configurée (→ recycle
    # au routing). PROTECT : une Route référencée ne peut être supprimée.
    route = models.ForeignKey(
        'Route', on_delete=models.PROTECT, related_name='feeds',
        null=True, blank=True, db_index=True,
    )
    # Spec de décodage de la famille (§1.5), consommée **plus tard** par le parsing.
    # ``{}`` = layout pas encore déclaré ⇒ recycle (config gap) au parse, plus tard.
    # Validé en forme au save (cf. ``validate_layout``), jamais en valeur métier.
    layout = models.JSONField(default=dict, blank=True, validators=[validate_layout])
    # Contrat de complétude (§1.7), consommé **plus tard** : contenu attendu par
    # défaut ; la vacuité doit être explicitement autorisée par famille.
    can_be_empty = models.BooleanField(default=False)
    # Descripteur d'identification (§1.6). Nullable : un feed sans profil ne casse
    # pas ; le moteur (§1.6-b) tracera l'absence. SET_NULL : supprimer un profil
    # n'efface pas le feed (le lien retombe à null).
    identification_profile = models.ForeignKey(
        'IdentificationProfile', on_delete=models.SET_NULL, related_name='feeds',
        null=True, blank=True,
    )

    # Plus de UniqueConstraint(channel, subfolder) : N Feeds par sous-dossier,
    # départagées par grammaire (+ priority). L'unicité « par motif » n'est pas
    # exprimable en contrainte DB (la grammaire est un JSON regex) → gérée à l'usage.
    # La Route étant désormais transverse (pas de sub_tenant), plus de garde-fou
    # « même locataire » : une Feed peut référencer n'importe quelle Route.

    def save(self, *args, **kwargs):
        # Validation **au save** du seul ``layout`` (forme only) : un descripteur
        # malformé doit échouer ici, pour qu'une erreur de config soit attrapée tôt
        # — hors de l'espace recycle/reject du parsing (§1.5). Les ``validators`` de
        # champ ne s'exécutent qu'au ``full_clean`` (admin/forms) ; on les rejoue
        # explicitement ici pour couvrir aussi les ``.save()`` programmatiques, sans
        # élargir la validation aux autres champs (pas de changement de comportement).
        validate_layout(self.layout)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.channel}/{self.subfolder or "(racine)"}'


class Route(models.Model):
    """Descripteur de traitement **réutilisable**, référencé par une Feed (§1.4).

    Une Route décrit *où va* un flux reconnu et *comment le charger* — sans embarquer
    aucune logique de parsing (déféré §1.5). Le routing se contente de poser la clé
    (``ReceivedFile.route``) = ``feed.route`` (au plus une route par
    Feed ; **n Feeds → 1 Route** réutilisable). ``code`` est le slug
    stable (**unique globalement**). ``layout``/``layout_version`` décrivent la
    structure cible, ``target`` la destination déclarative, ``strategy`` le loader
    (``null`` = loader générique). ``data_type``/``business_domain``/``data_owner``
    provisoires avant l'IAM (cf. [[iam-rights-redesign]]).

    **Non scopée locataire** : pas de ``sub_tenant`` ni de ``partner``. Une Route est
    un descripteur **réutilisable** transverse ; le rattachement à un partenaire/tenant
    se fait via la Feed qui la référence (``Feed.route``).
    """

    code = models.CharField(max_length=128, db_index=True)  # slug stable
    label = models.CharField(max_length=255, blank=True, default='')

    # Provisoire avant l'IAM (libellés plats ; future redéfinition des droits).
    data_type = models.CharField(max_length=64, blank=True, default='')
    business_domain = models.CharField(max_length=64, blank=True, default='')
    data_owner = models.CharField(max_length=128, blank=True, default='')

    # Structure cible (déclaratif, consommé par le futur stage de load §1.5).
    layout = models.JSONField(default=dict, blank=True)
    layout_version = models.IntegerField(default=1)
    target = models.JSONField(default=dict, blank=True)

    # Loader : ``strategy=null`` ⇒ loader générique ; sinon stratégie nommée.
    strategy = models.CharField(max_length=64, null=True, blank=True)
    strategy_params = models.JSONField(default=dict, blank=True)

    # Attributs transverses (contains_pii, regulatory_scope…).
    attributes = models.JSONField(default=dict, blank=True)

    active = models.BooleanField(default=True)
    version = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['code'], name='uniq_route_code'),
        ]

    def __str__(self):
        return self.code


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
        ROUTING = 'routing', 'Routage'
        PARSING = 'parsing', 'Parsing'
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
    # Passe de traitement (cf. ``run_scope``). Stampé automatiquement via le défaut
    # callable ``_current_run_id`` (contextvar) : tous les Event d'une même passe le
    # partagent. ``null`` = Event créé hors d'une passe (legacy avant backfill, ou
    # création directe hors orchestrateur). Le rollup l'utilise pour ne retenir que
    # la **dernière passe** de chaque stage.
    run_id = models.CharField(
        max_length=32, null=True, blank=True, db_index=True,
        default=_current_run_id, editable=False,
    )
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
    (un Recycle ayant abouti à un OK ``push``, cf. ``recycle_file``). Plus de statut,
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

    Pour chaque fichier, on ne considère que la **dernière passe de chaque stage**
    (``run_id`` du dernier ``Event`` du stage, cf. ``run_scope``) : un contrôle qui
    a cessé d'être émis dans la passe courante (colonne retypée, règle retirée du
    contrat) est ainsi **écarté** au lieu de figer son ancien verdict. Parmi les
    contrôles de ces passes courantes, on retient la **classe de monitoring la plus
    sévère** (``MONITORING_SEVERITY``, board orienté action). Un seul signal par
    fichier, **toutes étapes confondues** : admission/qualif/routing/parsing
    aujourd'hui, contrôles DORA demain, sans câbler aucun nom de contrôle/stage.

    NB : le worst-wins porte sur **tous** les contrôles de la passe courante, pas
    sur le seul ``verdict`` — c'est ce qui fait **remonter** un signal comme le
    ``warning_action`` « partenaire révoqué qui émet » (plus sévère que le verdict
    ``reject``), au lieu de l'enterrer (cf. docs/admission-monitoring-design.md §5/§7).

    Renvoie ``{file_id: {'monitoring_class', 'stage', 'control', 'result'}}`` ;
    un fichier sans aucun événement est simplement absent du mapping.
    """
    file_ids = list(file_ids)
    if not file_ids:
        return {}
    # Derniers d'abord : pour chaque (file, stage), le 1er event vu fixe le ``run_id``
    # de la passe courante ; on ne garde ensuite QUE les events de cette passe (même
    # (file, stage, run_id)). Append-only + mono-thread par fichier ⇒ les events d'une
    # passe sont contigus dans cet ordre, jamais entrelacés avec une passe antérieure.
    events = (Event.objects
              .filter(file_id__in=file_ids)
              .order_by('file_id', 'stage', '-created_at', '-id')
              .values('file_id', 'stage', 'control', 'monitoring_class',
                      'result', 'run_id'))
    stage_run = {}   # (file, stage) -> run_id de la passe courante du stage
    worst = {}
    for e in events:
        sk = (e['file_id'], e['stage'])
        if sk not in stage_run:
            stage_run[sk] = e['run_id']          # le plus récent du stage
        if e['run_id'] != stage_run[sk]:
            continue                             # event d'une passe antérieure → ignoré
        fid = e['file_id']
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


class BusinessCalendar(models.Model):
    """Calendrier de jours ouvrés d'une place / convention (référentiel).

    Regroupe les fériés (``holidays``) et les exceptions (``exceptions``) d'une
    place donnée (ex. ``LU`` pour le Luxembourg / ABBL, ``TARGET2`` pour la place
    interbancaire euro). Référentiel pur : aucun moteur de calcul de jours ouvrés
    n'est implémenté ici. La précédence retenue (résolue **plus tard** par le futur
    moteur, hors scope) est : exception sous-fonds > exception calendrier > férié de
    place > week-end. Les week-ends ne sont **pas** stockés (règle déduite par le
    moteur).
    """

    code = models.CharField(max_length=32, unique=True, db_index=True)
    label = models.CharField(max_length=255)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='business_calendars',
    )

    def __str__(self):
        return self.code


class CalendarHoliday(models.Model):
    """Jour férié d'une place (jour entier fermé).

    ``is_bank_holiday`` distingue le férié bancaire de convention collective (B,
    True) du férié légal (P, False) — tracé pour audit, **aucune logique** dessus
    dans ce batch. Les week-ends ne sont pas stockés ici. Une demi-journée (ex.
    24/12 après-midi) se modélisera plus tard via ``CalendarException(is_open=...)``,
    pas ici : 24/12 est un jour fermé entier dans ce référentiel.
    """

    business_calendar = models.ForeignKey(
        BusinessCalendar, on_delete=models.CASCADE, related_name='holidays',
    )
    date = models.DateField(db_index=True)
    label = models.CharField(max_length=255)
    is_bank_holiday = models.BooleanField(default=False)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='calendar_holidays',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['business_calendar', 'date'], name='uniq_calendar_date'),
        ]
        ordering = ['date']

    def __str__(self):
        return f'{self.date} {self.label}'


class CalendarException(models.Model):
    """Exception bidirectionnelle au calendrier d'une place.

    ``is_open=True`` ouvre un jour normalement fermé ; ``is_open=False`` ferme un
    jour normalement ouvert. Précédence (résolue **plus tard** par le futur moteur,
    hors scope) : exception sous-fonds > exception calendrier > férié de place >
    week-end. Portée **calendrier uniquement** dans ce batch (FK
    ``business_calendar``) ; la portée sous-fonds est différée (``SubFund`` n'existe
    pas encore — §1.6).
    """

    business_calendar = models.ForeignKey(
        BusinessCalendar, on_delete=models.CASCADE, related_name='exceptions',
    )
    date = models.DateField(db_index=True)
    is_open = models.BooleanField()
    reason = models.CharField(max_length=255)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='calendar_exceptions',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['business_calendar', 'date'],
                name='uniq_calendar_exception_date'),
        ]

    def __str__(self):
        return f'{self.date} ({self.reason})'


class Referential(models.Model):
    """Méta-référentiel : déclare un référentiel et sa politique d'absence (§1.6-a).

    Liste **ouverte** de référentiels (``subfund``, ``share_class``, ``instrument``,
    ``country``, ``currency``, …) ajoutables comme simples lignes, sans migration.
    Porte uniformément l'``absence_policy`` consommée **plus tard** par le moteur
    d'identification (§1.6-b). Aucun lien physique vers les valeurs : le pivot
    ``SubFund`` ne référence PAS ce modèle ; le moteur résoudra sa policy via
    ``Referential.objects.get(code='subfund')``.
    """

    class AbsencePolicy(models.TextChoices):
        CANDIDATE = 'candidate', 'Candidate (onboarding)'
        ANOMALY = 'anomaly', 'Anomalie (erreur)'

    code = models.CharField(max_length=32, unique=True, db_index=True)
    label = models.CharField(max_length=255)
    absence_policy = models.CharField(
        max_length=16, choices=AbsencePolicy.choices,
        default=AbsencePolicy.CANDIDATE,
        help_text=(
            'entité absente du référentiel → candidate (onboarding, '
            'warning_action) ou anomaly (erreur). Appliquée uniformément par le '
            "moteur d'identification (§1.6-b)."
        ),
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='referentials',
    )

    def __str__(self):
        return self.code


class SubFund(models.Model):
    """Référentiel pivot (rôle partition de l'identification §1.6). Atome de
    monitoring métier. absence_policy portée par Referential(code='subfund'),
    résolue par le moteur.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Actif'
        INACTIVE = 'inactive', 'Inactif'

    key = models.CharField(max_length=255, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='subfunds',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sub_tenant', 'key'], name='uniq_subfund_key_per_tenant'),
        ]
        ordering = ['key']

    def __str__(self):
        return self.key


class SubFundAlias(models.Model):
    """Alias d'un code externe provider vers le ``SubFund`` canonique, scopé par
    ``Feed`` (le répertoire porte le système de codes). L'identité interne reste
    ``SubFund.key`` (§1.6-a-ter).

    Un même code externe peut désigner des compartiments différents dans deux feeds
    distincts (namespaces séparés) ; il est unique au sein d'un ``(sub_tenant, feed)``.
    a-ter n'apporte QUE cette table + son admin (saisie Steward) ; la résolution
    alias→canonique au moteur, c'est §1.6-b-bis.
    """

    sub_fund = models.ForeignKey(
        SubFund, on_delete=models.CASCADE, related_name='aliases',
    )
    # PROTECT : supprimer un Feed ne doit pas faire disparaître silencieusement les
    # mappings d'alias qui en dépendent (le namespace de codes est porté par le Feed).
    feed = models.ForeignKey(
        'Feed', on_delete=models.PROTECT, related_name='subfund_aliases',
    )
    external_code = models.CharField(max_length=255, db_index=True)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='subfund_aliases',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sub_tenant', 'feed', 'external_code'],
                name='uniq_subfund_alias'),
        ]
        ordering = ['feed', 'external_code']

    def __str__(self):
        # Feed n'a pas de ``file_type`` (il vit sur IdentificationProfile) ; le
        # namespace réel d'un Feed est son ``__str__`` (canal/sous-dossier).
        return f'{self.external_code} → {self.sub_fund.key} ({self.feed})'


class ReferentialEntry(models.Model):
    """Valeurs des référentiels subordonnés (existence seule). SubFund est un
    référentiel pivot dédié, hors de ce modèle.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Actif'
        INACTIVE = 'inactive', 'Inactif'

    referential = models.ForeignKey(
        Referential, on_delete=models.PROTECT, related_name='entries',
    )
    key = models.CharField(max_length=255, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='referential_entries',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sub_tenant', 'referential', 'key'],
                name='uniq_referential_entry'),
        ]
        ordering = ['referential', 'key']

    def __str__(self):
        return f'{self.referential}:{self.key}'


class IdentificationProfile(models.Model):
    """Descripteur d'identification d'un type de fichier (§1.6). Porte les règles
    (``IdentificationRule``) qui mappent chaque champ du record vers son rôle et
    son référentiel ; consommé **plus tard** par le moteur (§1.6-b).
    """

    file_type = models.CharField(max_length=255, db_index=True)
    label = models.CharField(max_length=255)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='identification_profiles',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sub_tenant', 'file_type'],
                name='uniq_profile_per_filetype'),
        ]

    def __str__(self):
        return self.file_type


class IdentificationRule(models.Model):
    """Règle déclarative : mappe un champ du record vers son rôle d'identification
    et, le cas échéant, le référentiel qui le résout (§1.6).
    """

    class Role(models.TextChoices):
        PARTITION = 'partition', 'Partition'
        AXIS = 'axis', 'Axe'
        SUBORDINATE = 'subordinate', 'Subordonné'

    profile = models.ForeignKey(
        IdentificationProfile, on_delete=models.CASCADE, related_name='rules',
    )
    field = models.CharField(max_length=255)
    referential = models.ForeignKey(
        Referential, on_delete=models.PROTECT, null=True, blank=True,
        help_text=(
            'référentiel résolu pour ce champ. Null pour un axis (ex. date) qui '
            'ne résout aucun référentiel.'
        ),
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    required = models.BooleanField(default=True)
    sub_tenant = models.ForeignKey(
        SubTenant, on_delete=models.PROTECT, related_name='identification_rules',
    )

    class Meta:
        ordering = ['profile', 'role', 'field']

    def __str__(self):
        return f'{self.profile}:{self.field} ({self.role})'

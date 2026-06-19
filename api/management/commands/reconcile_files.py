"""Réconciliation S3 (vérité du stockage) ↔ table ``api.ReceivedFile`` (journal).

Filet de sécurité pour les divergences que les hooks SFTPGo ne couvrent pas
(événements perdus si Gunicorn était down, suppressions hors-bande, etc.) :

  * **backfill**  — objet présent dans le bucket mais aucune ligne ``stored`` :
                    on crée une ligne ``stored`` marquée ``reconciled=True`` ;
  * **promote**   — ligne bloquée en ``receiving`` alors que l'objet existe :
                    on la passe en ``stored`` (le hook ``upload`` s'est perdu) ;
  * **missing**   — ligne ``stored`` dont l'objet n'existe plus dans S3 :
                    on la passe en ``missing`` (drift, p.ex. delete non notifié).

La clé S3 réelle d'une ligne est son chemin *physique* (``path``), avec repli
sur ``s3_key`` — exactement la résolution utilisée par le presign (voir
``presign_received_file``). ``--dry-run`` n'écrit rien et se contente de lister.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.admission import file_admission
from api.models import ReceivedFile, default_sub_tenant_id
from api.s3 import get_s3_client


def object_key(rf):
    """Clé d'objet S3 réelle d'un ``ReceivedFile`` (idem presign)."""
    return rf.path or (rf.s3_key or '').lstrip('/')


class Command(BaseCommand):
    help = 'Réconcilie le bucket S3 avec la table ReceivedFile (backfill / missing).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bucket', default=None,
            help='Bucket à scanner (défaut: bucket des lignes existantes, '
                 'sinon SCW_BUCKET_PREFIX).')
        parser.add_argument(
            '--prefix', default='',
            help='Ne réconcilier que les clés sous ce préfixe (ex: test/).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help="N'écrit rien : affiche seulement ce qui serait changé.")

    def handle(self, *args, **opts):
        bucket = opts['bucket'] or self._default_bucket()
        prefix = opts['prefix']
        dry = opts['dry_run']
        if not bucket:
            self.stderr.write('Aucun bucket (ni --bucket, ni lignes, ni '
                              'SCW_BUCKET_PREFIX).')
            return

        # 1) Vérité côté S3 : key -> taille.
        s3_objects = self._list_s3(bucket, prefix)
        self.stdout.write(f'S3 {bucket}/{prefix or ""} : {len(s3_objects)} objet(s).')

        # 2) État côté base : key réelle -> ReceivedFile « actif » le plus récent.
        #    (on indexe les lignes non terminales/utiles pour la comparaison)
        db_by_key = {}
        active_states = (
            ReceivedFile.State.RECEIVING,
            ReceivedFile.State.STORED,
            ReceivedFile.State.MISSING,
        )
        qs = ReceivedFile.objects.filter(state__in=active_states).order_by('received_at')
        for rf in qs.iterator():
            key = object_key(rf)
            if prefix and not key.startswith(prefix):
                continue
            db_by_key[key] = rf  # garde le plus récent (ordre croissant)

        backfilled = promoted = marked_missing = in_sync = 0

        # 3) S3 -> base : backfill / promote.
        for key, size in s3_objects.items():
            rf = db_by_key.get(key)
            if rf is None:
                backfilled += 1
                self.stdout.write(f'  [backfill] {key} ({size} o)')
                if not dry:
                    ReceivedFile.objects.create(
                        state=ReceivedFile.State.STORED,
                        s3_key=key, path=key, bucket=bucket,
                        file_size=size, status=1, action='reconcile',
                        reconciled=True, stored_at=timezone.now(),
                        # Tenant d'ingest par défaut (l'admission le re-pointe).
                        sub_tenant_id=default_sub_tenant_id(),
                        raw={'source': 'reconcile'},
                    )
            elif rf.state == ReceivedFile.State.RECEIVING:
                promoted += 1
                self.stdout.write(f'  [promote ] {key} (receiving -> stored)')
                if not dry:
                    rf.state = ReceivedFile.State.STORED
                    rf.file_size = rf.file_size or size
                    rf.status = 1
                    rf.reconciled = True
                    rf.stored_at = rf.stored_at or timezone.now()
                    rf.save(update_fields=['state', 'file_size', 'status',
                                           'reconciled', 'stored_at'])
            elif rf.state == ReceivedFile.State.MISSING:
                # L'objet est réapparu : on rétablit stored.
                promoted += 1
                self.stdout.write(f'  [restore ] {key} (missing -> stored)')
                if not dry:
                    rf.state = ReceivedFile.State.STORED
                    rf.deleted_at = None
                    rf.reconciled = True
                    rf.save(update_fields=['state', 'deleted_at', 'reconciled'])
            else:
                in_sync += 1

        # 4) base -> S3 : lignes stored dont l'objet a disparu.
        for key, rf in db_by_key.items():
            if rf.state == ReceivedFile.State.STORED and key not in s3_objects:
                marked_missing += 1
                self.stdout.write(f'  [missing ] {key} (stored mais absent de S3)')
                if not dry:
                    rf.state = ReceivedFile.State.MISSING
                    rf.deleted_at = timezone.now()
                    rf.save(update_fields=['state', 'deleted_at'])

        # 5) Filet admission : toute ligne `stored` SANS aucun événement
        #    d'admission n'a jamais été classée (webhook perdu, gunicorn down au
        #    moment de l'upload, backfill ci-dessus, etc.). On (re)lance
        #    file_admission — même rôle que rattraper un hook perdu. Idempotent et
        #    rejouable ; --dry-run n'écrit rien.
        admitted = 0
        need_admission = list(
            ReceivedFile.objects
            .filter(state=ReceivedFile.State.STORED)
            .exclude(events__stage='admission'))
        for rf in need_admission:
            admitted += 1
            self.stdout.write(f'  [admission] {object_key(rf)} (stored sans event admission)')
            if not dry:
                file_admission(rf.pk)

        tag = 'DRY-RUN — ' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'{tag}backfill={backfilled} promote={promoted} '
            f'missing={marked_missing} admission={admitted} déjà-ok={in_sync}'))

    def _default_bucket(self):
        """Bucket réel : l'unique bucket des lignes, sinon SCW_BUCKET_PREFIX."""
        buckets = list(
            ReceivedFile.objects.exclude(bucket='')
            .order_by('bucket')  # neutralise le Meta.ordering, sinon distinct() ne collapse pas
            .values_list('bucket', flat=True).distinct())
        if len(buckets) == 1:
            return buckets[0]
        if len(buckets) > 1:
            self.stderr.write(
                f'Plusieurs buckets en base {buckets} : précisez --bucket.')
            return None
        return settings.SCW_BUCKET_PREFIX

    def _list_s3(self, bucket, prefix):
        """{key: size} de tous les objets (pagination)."""
        client = get_s3_client()
        out = {}
        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                out[obj['Key']] = obj['Size']
        return out

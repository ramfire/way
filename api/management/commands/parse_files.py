"""Worker de parsing (§1.5) : décode les fichiers qualifiés pas encore parsés.

Asynchrone par conception (hors du chemin webhook) : c'est le premier stage qui lit
le **contenu** S3. Sélectionne les fichiers ayant un Event de qualification
``qualified`` et **aucun** Event de parsing, puis appelle ``file_parsing`` (qui
revérifie que le verdict de qualification *courant* est bien ``qualified``,
idempotent, rejouable, non bloquant).

``--force`` reparse aussi les fichiers déjà parsés (utile après un changement de
``layout``). ``--dry-run`` n'écrit rien : liste seulement les candidats.
"""
from django.core.management.base import BaseCommand

from api.models import ReceivedFile
from api.parsing import (
    STAGE as PARSING_STAGE, VERDICT_PARSED, VERDICT_RECYCLE, file_parsing,
)
from api.qualification import STAGE as QUAL_STAGE, VERDICT_QUALIFIED


class Command(BaseCommand):
    help = 'Parse les fichiers qualifiés (§1.5) : décodage structurel via leur layout.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Reparse aussi les fichiers déjà parsés (ex. après edit du layout).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help="N'écrit rien : liste seulement les candidats.")

    def handle(self, *args, **opts):
        force = opts['force']
        dry = opts['dry_run']

        # .filter() puis .exclude() sur la relation `events` ⇒ deux jointures
        # distinctes : « a un event qualif qualified » ET « n'a aucun event parsing ».
        qs = (ReceivedFile.objects
              .filter(events__stage=QUAL_STAGE,
                      events__detail__verdict=VERDICT_QUALIFIED)
              .distinct())
        if not force:
            qs = qs.exclude(events__stage=PARSING_STAGE)

        parsed = recycled = skipped = 0
        for rf in qs.iterator():
            key = rf.path or rf.s3_key
            if dry:
                self.stdout.write(f'  [candidat] {key} (id={rf.pk})')
                skipped += 1
                continue
            verdict = file_parsing(rf.pk)
            if verdict == VERDICT_PARSED:
                parsed += 1
            elif verdict == VERDICT_RECYCLE:
                recycled += 1
            else:
                # Plus qualifié au moment du run (re-qualif entre-temps), etc.
                skipped += 1
            self.stdout.write(f'  {key} -> {verdict}')

        tag = 'DRY-RUN — ' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'{tag}parsed={parsed} recycle={recycled} skip={skipped}'))

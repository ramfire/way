"""Déclencheur **à la demande** du moteur d'identification (§1.6-b).

Appelle ``file_identification(<file_id>)`` puis imprime les Event de stage
``identification`` émis pour ce fichier (un par rule du profil, + un éventuel
``profile_resolution``). Outil de test : le moteur n'est **pas** câblé au webhook
auto dans ce batch (le chaînage admission→identification sera décidé séparément).

``file_identification`` est rejouable, append-only, ne lève jamais et rematérialise
``control_class`` ; relancer la commande ré-émet une nouvelle salve d'Event.
"""
from django.core.management.base import BaseCommand, CommandError

from api.admission import STAGE_IDENTIFICATION, file_identification
from api.models import Event, ReceivedFile


class Command(BaseCommand):
    help = "Lance le moteur d'identification (§1.6-b) sur un fichier (par id)."

    def add_arguments(self, parser):
        parser.add_argument(
            'file_id', type=int,
            help='Id du ReceivedFile à identifier.')

    def handle(self, *args, **opts):
        file_id = opts['file_id']
        if not ReceivedFile.objects.filter(pk=file_id).exists():
            raise CommandError(f'ReceivedFile id={file_id} introuvable.')

        # Borne temporelle : on n'affiche que les Event émis par CET appel.
        before = (Event.objects
                  .filter(file_id=file_id, stage=STAGE_IDENTIFICATION)
                  .values_list('pk', flat=True))
        before_ids = set(before)

        file_identification(file_id)   # ne lève jamais ; rematérialise control_class

        emitted = (Event.objects
                   .filter(file_id=file_id, stage=STAGE_IDENTIFICATION)
                   .exclude(pk__in=before_ids)
                   .order_by('created_at', 'id'))

        rf = ReceivedFile.objects.get(pk=file_id)
        self.stdout.write(f'file id={file_id} key={rf.path or rf.s3_key}')
        if not emitted:
            self.stdout.write(self.style.WARNING(
                '  aucun Event identification émis (voir django.log).'))
        for ev in emitted:
            self.stdout.write(
                f'  [{ev.control}] {ev.result}/{ev.monitoring_class} '
                f'detail={ev.detail}')
        self.stdout.write(self.style.SUCCESS(
            f'identification jouée — control_class={rf.control_class!r} '
            f'({emitted.count()} event(s))'))

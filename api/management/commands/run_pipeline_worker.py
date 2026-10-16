"""Worker de la chaîne post-stockage (admission → … → chargement Acta).

Boucle : réclame un ``ReceivedFile`` ``pending`` (posé par le webhook SFTPGo),
déroule ``api.pipeline.run_pipeline``, marque ``done``/``error``. Sans travail,
dort ``--interval`` secondes. Au démarrage, remet en file les lignes ``running``
orphelines (worker précédent interrompu).

Lancé en continu par ``alfaway-worker.service`` (systemd). ``--once`` vide la
file puis sort (rattrapage manuel / tests). SIGTERM : termine le fichier en
cours puis sort proprement.
"""
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api import pipeline


class Command(BaseCommand):
    help = 'Exécute la chaîne post-stockage des fichiers en attente (worker).'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=2.0,
                            help='Attente (s) quand la file est vide.')
        parser.add_argument('--once', action='store_true',
                            help='Vide la file puis sort.')

    def handle(self, *args, **opts):
        self._stop = False
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)

        n = pipeline.requeue_orphans()
        if n:
            self.stdout.write(f'{n} fichier(s) running orphelin(s) remis en file')

        while not self._stop:
            if not opts['once']:
                # Process long-vivant : recycle les connexions DB mortes/expirées
                # (inutile en --once, process court ; et casserait l'atomic des tests).
                close_old_connections()
            file_id = pipeline.claim_next()
            if file_id is None:
                if opts['once']:
                    break
                time.sleep(opts['interval'])
                continue
            ok = pipeline.process(file_id)
            self.stdout.write(f'file {file_id}: {"done" if ok else "error"}')

    def _on_term(self, signum, frame):
        self._stop = True

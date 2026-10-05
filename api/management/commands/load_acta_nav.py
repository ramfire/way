"""Charge un fichier LU_PO_NAV dans Acta (loader APINAV, tout par l'API).

    manage.py load_acta_nav --file 587 --dry-run   # plan + contrôles locaux, rien d'écrit
    manage.py load_acta_nav --file 587             # crée entités/liens puis ingère les VNI
"""
import json

from django.core.management.base import BaseCommand

from api.acta_nav import load_file


class Command(BaseCommand):
    help = "Charge un ReceivedFile LU_PO_NAV dans Acta via son API."

    def add_arguments(self, parser):
        parser.add_argument('--file', type=int, required=True, help='id ReceivedFile')
        parser.add_argument('--dry-run', action='store_true',
                            help="N'écrit rien : plan + contrôles Acta rejoués localement.")

    def handle(self, *args, **opts):
        summary, res = load_file(opts['file'], dry_run=opts['dry_run'])
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        if res is not None and not opts['dry_run']:
            self.stdout.write(json.dumps(
                {k: (v if k in ('nav_counts',) else len(v)) for k, v in res.items()},
                ensure_ascii=False))

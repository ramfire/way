import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .admission import (
    STAGE, VERDICT_ADMIS, VERDICT_QUARANTINE, VERDICT_RECYCLE, file_admission,
    latest_admission_event,
)
from . import qualification as qual
from . import parsing
from .models import (
    Channel, Event, Handled, Feed, Partner, ReceivedFile, Route,
    SubTenant, current_control_rollup, refresh_control_class,
)


def default_tenant():
    """Le SubTenant par défaut ``GIL`` (créé par la migration 0009)."""
    return SubTenant.objects.get(code='GIL')


def make_file(username='acme', s3_key='in/acme/file.csv', **kw):
    """Crée une ligne `stored` minimale (l'admission tourne post-stockage)."""
    return ReceivedFile.objects.create(
        state=ReceivedFile.State.STORED,
        s3_key=s3_key, path=kw.pop('path', s3_key),
        username=username, bucket='alfaway-dev', status=1,
        sub_tenant=kw.pop('sub_tenant', None) or default_tenant(),
        **kw,
    )


def enrol(username, status=Partner.Status.ACTIVE, sub_tenant=None):
    """Enrôle un partenaire + son canal SFTP (la résolution part de l'identifier).

    Remplace l'ancien ``Partner.objects.create(username=…)`` : aujourd'hui la
    reconnaissance d'un flux passe par un ``Channel(kind='sftp', identifier=…)``.
    """
    st = sub_tenant or default_tenant()
    p = Partner.objects.create(code=username, status=status, sub_tenant=st)
    Channel.objects.create(
        kind=Channel.Kind.SFTP, identifier=username, partner=p,
        sub_tenant=st, active=(status == Partner.Status.ACTIVE))
    return p


def add_route(username, code=None, active=True):
    """Déclare une Route (descripteur transverse, non scopé locataire).

    Réutilisable : on l'accroche ensuite à une (ou plusieurs) Feed via
    ``add_feed(..., route=...)``. ``code`` dérivé de ``username`` par défaut
    (unicité **globale** sur ``code``).
    """
    route, _ = Route.objects.get_or_create(
        code=code or f'r-{username}', defaults={'active': active})
    if route.active != active:
        route.active = active
        route.save()
    return route


def add_feed(username, subfolder, filename_regex=None, route=None, priority=0):
    """Enrôle une Feed (contrat de nommage fin) pour le canal de ``username``.

    ``filename_regex=None`` ⇒ grammaire vide (attrape-tout). ``route`` (optionnel) =
    la Route portée par ce contrat : un fichier qualifié n'atteint ``push`` que si
    sa Feed porte une Route active (§1.4).
    """
    ch = Channel.objects.get(kind=Channel.Kind.SFTP, identifier=username)
    grammar = {'filename': filename_regex} if filename_regex is not None else {}
    return Feed.objects.create(
        channel=ch, sub_tenant=ch.sub_tenant, subfolder=subfolder,
        grammar=grammar, route=route, priority=priority)


def verdict_events(rf):
    return Event.objects.filter(file=rf, stage=STAGE, control='verdict')


class AdmissionVerdictTests(TestCase):
    def test_admis_when_mapped_active_authorised(self):
        enrol('acme')
        rf = make_file(username='acme')

        result = file_admission(rf.pk)

        self.assertEqual(result, VERDICT_ADMIS)
        ev = latest_admission_event(rf)
        self.assertEqual(ev.control, 'verdict')
        self.assertEqual(ev.result, Event.Result.PASSED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        self.assertEqual(ev.detail['verdict'], VERDICT_ADMIS)
        # Caches de résolution posés sur la ligne (channel + partner).
        rf.refresh_from_db()
        self.assertIsNotNone(rf.channel_id)
        self.assertIsNotNone(rf.partner_id)
        # state ne reflète QUE le stockage S3 : inchangé.
        self.assertEqual(rf.state, ReceivedFile.State.STORED)

    def test_recycle_when_partner_unmapped(self):
        rf = make_file(username='ghost')  # aucun Channel/Partner

        result = file_admission(rf.pk)

        self.assertEqual(result, VERDICT_RECYCLE)
        ev = latest_admission_event(rf)
        self.assertEqual(ev.result, Event.Result.FAILED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.RECYCLE)
        self.assertEqual(ev.detail['reason'], 'partner_not_mapped')
        # Discovery : on n'a JAMAIS auto-créé le canal ni le partenaire.
        self.assertFalse(Channel.objects.filter(identifier='ghost').exists())
        self.assertFalse(Partner.objects.filter(code='ghost').exists())
        # Caches laissés NULL (compte non mappé).
        rf.refresh_from_db()
        self.assertIsNone(rf.channel_id)
        self.assertIsNone(rf.partner_id)
        self.assertEqual(rf.state, ReceivedFile.State.STORED)

    def test_quarantine_and_warning_when_revoked(self):
        enrol('old', status=Partner.Status.REVOKED)
        rf = make_file(username='old')

        result = file_admission(rf.pk)

        self.assertEqual(result, VERDICT_QUARANTINE)
        ev = latest_admission_event(rf)
        self.assertEqual(ev.result, Event.Result.FAILED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.REJECT)
        # Un warning_action « révoqué émet encore » a été levé (action ops).
        self.assertTrue(Event.objects.filter(
            file=rf, stage=STAGE,
            monitoring_class=Event.MonitoringClass.WARNING_ACTION).exists())
        # On garde le fichier : S3 jamais supprimé, state inchangé.
        rf.refresh_from_db()
        self.assertEqual(rf.state, ReceivedFile.State.STORED)

    @override_settings(ADMISSION_PATH_RULES={'acme': ['in/acme/']})
    def test_channel_authorised_pass_and_fail(self):
        enrol('acme')
        ok_file = make_file(username='acme', s3_key='in/acme/ok.csv')
        bad_file = make_file(username='acme', s3_key='elsewhere/bad.csv')

        self.assertEqual(file_admission(ok_file.pk), VERDICT_ADMIS)
        self.assertEqual(file_admission(bad_file.pk), VERDICT_RECYCLE)
        bad_ev = latest_admission_event(bad_file)
        self.assertEqual(bad_ev.detail['reason'], 'channel_not_authorised')


class AdmissionInitMilestoneTests(TestCase):
    def test_first_admis_flagged_then_subsequent_not(self):
        enrol('acme')
        rf1 = make_file(username='acme', s3_key='in/acme/1.csv')
        rf2 = make_file(username='acme', s3_key='in/acme/2.csv')

        file_admission(rf1.pk)
        file_admission(rf2.pk)

        first = latest_admission_event(rf1)
        second = latest_admission_event(rf2)
        self.assertTrue(first.detail['first'])
        self.assertFalse(second.detail['first'])


class AdmissionRerunSafetyTests(TestCase):
    def test_recycle_then_enrol_then_rerun_admits(self):
        rf = make_file(username='newcomer')

        # 1er passage : non mappé → recycle.
        self.assertEqual(file_admission(rf.pk), VERDICT_RECYCLE)

        # Un humain enrôle le partenaire (+ canal), puis on rejoue l'admission.
        enrol('newcomer')
        self.assertEqual(file_admission(rf.pk), VERDICT_ADMIS)

        # Le verdict COURANT (dernier) est admis ; l'audit conserve les deux.
        self.assertEqual(latest_admission_event(rf).detail['verdict'], VERDICT_ADMIS)
        self.assertEqual(verdict_events(rf).count(), 2)
        # Premier admis pour ce partenaire → milestone d'init posée.
        self.assertTrue(latest_admission_event(rf).detail['first'])

    def test_rerun_is_append_only_no_short_circuit(self):
        enrol('acme')
        rf = make_file(username='acme')

        file_admission(rf.pk)
        n_after_first = Event.objects.filter(file=rf).count()
        file_admission(rf.pk)
        n_after_second = Event.objects.filter(file=rf).count()

        # Pas de court-circuit : le 2e passage réémet ses événements (append-only).
        self.assertEqual(n_after_second, 2 * n_after_first)
        # Verdict cohérent et 2e admis n'est plus le « premier ».
        self.assertEqual(latest_admission_event(rf).detail['verdict'], VERDICT_ADMIS)
        self.assertFalse(latest_admission_event(rf).detail['first'])


class AdmissionNeverRaisesTests(TestCase):
    def test_unknown_file_id_returns_none_not_raises(self):
        # ReceivedFile.DoesNotExist doit être avalé : jamais propagé à l'appelant.
        self.assertIsNone(file_admission(999999))


class ControlRollupTests(TestCase):
    """Rollup « worst-wins » générique de l'axe contrôles (board, étape 2)."""

    def test_empty_for_file_without_events(self):
        rf = make_file()
        self.assertNotIn(rf.pk, current_control_rollup([rf.pk]))

    def test_admis_rolls_up_to_push(self):
        enrol('acme')
        add_feed('acme', 'in/acme', r'.+', route=add_route('acme'))  # qualifié + routé → push
        rf = make_file(username='acme')
        file_admission(rf.pk)

        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'], Event.MonitoringClass.PUSH)

    def test_quarantine_surfaces_warning_action_over_reject(self):
        # Un fichier quarantine porte un verdict `reject` ET un `warning_action`
        # (révoqué qui émet). Le worst-wins doit remonter le warning_action (plus
        # sévère / actionnable), PAS le verdict reject — c'est le signal à surfacer.
        enrol('old', status=Partner.Status.REVOKED)
        rf = make_file(username='old')
        result = file_admission(rf.pk)
        self.assertEqual(result, VERDICT_QUARANTINE)

        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'],
                         Event.MonitoringClass.WARNING_ACTION)
        self.assertEqual(roll['control'], 'partner_status')

    def test_uses_current_state_after_rerun_not_stale(self):
        # recycle puis (enrôlement) admis : le rollup reflète l'état COURANT (push),
        # pas l'ancien recycle resté dans le journal append-only.
        rf = make_file(username='newcomer')         # subfolder par défaut 'in/acme'
        file_admission(rf.pk)                       # recycle
        enrol('newcomer')
        add_feed('newcomer', 'in/acme', r'.+', route=add_route('newcomer'))  # admis + qualifié + routé → push
        file_admission(rf.pk)                       # admis

        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'], Event.MonitoringClass.PUSH)

    def test_admission_materialises_control_class(self):
        # file_admission rematérialise ReceivedFile.control_class (read-model board).
        enrol('old', status=Partner.Status.REVOKED)
        rf = make_file(username='old')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.WARNING_ACTION)

    def test_refresh_handles_bulk_and_null(self):
        rf_none = make_file(s3_key='in/x/none.csv')        # aucun contrôle → NULL
        enrol('acme')
        add_feed('acme', 'in/acme', r'.+', route=add_route('acme'))  # admis + qualifié + routé → push
        rf_admis = make_file(username='acme', s3_key='in/acme/a.csv')
        file_admission(rf_admis.pk)

        refresh_control_class([rf_none.pk, rf_admis.pk])   # idempotent
        rf_none.refresh_from_db()
        rf_admis.refresh_from_db()
        self.assertIsNone(rf_none.control_class)
        self.assertEqual(rf_admis.control_class, Event.MonitoringClass.PUSH)


class MonitoringCausesTests(TestCase):
    """Vue agrégée « par cause » (complément, étape 4)."""

    def test_aggregates_current_failing_controls_by_cause(self):
        enrol('old', status=Partner.Status.REVOKED)
        for i in range(3):
            file_admission(make_file(username='old', s3_key=f'in/old/{i}.csv').pk)

        staff = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(staff)
        data = self.client.get('/monitoring/causes/').json()

        # 3 fichiers concernés (distincts), chacun : partner_status (warning_action)
        # + verdict (reject). partner_recognised est PASSED → exclu.
        self.assertEqual(data['files_affected'], 3)
        by = {(c['control'], c['monitoring_class']): c for c in data['causes']}
        self.assertEqual(by[('partner_status', 'warning_action')]['count'], 3)
        self.assertEqual(by[('verdict', 'reject')]['count'], 3)
        self.assertEqual(by[('partner_status', 'warning_action')]['top_users'],
                         [{'username': 'old', 'count': 3}])
        # Tri : le plus sévère (warning_action) avant reject.
        self.assertEqual(data['causes'][0]['monitoring_class'],
                         Event.MonitoringClass.WARNING_ACTION)


class HandledTests(TestCase):
    """Tampon « traité » set-once au niveau fichier + réconciliation (étape 5).

    Le triage par cause (``TriageAck``) a été retiré : le traitement se fait
    fichier par fichier via l'action « Recycle » (``recycle_file``, ex-« Handle »),
    qui ne pose le flag QUE si le re-contrôle aboutit à un OK (couplage strict).
    """

    def setUp(self):
        enrol('old', status=Partner.Status.REVOKED)
        # Feed permissive PORTANT une route : une fois le partenaire ré-activé,
        # le « Handle » peut atteindre push (admis + qualifié + routé, §1.4).
        add_feed('old', 'in/old', r'.+', route=add_route('old'))
        self.files = [make_file(username='old', s3_key=f'in/old/{i}.csv') for i in range(3)]
        for f in self.files:
            file_admission(f.pk)
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(self.staff)

    def _causes(self):
        return {c['control']: c for c in self.client.get('/monitoring/causes/').json()['causes']}

    def _handle(self, rf):
        # « Recycle » est le mécanisme unique : rejoue ET pose le flag si OK.
        return self.client.post(f'/monitoring/files/{rf.pk}/recycle/').json()

    def test_file_override_reconciliation(self):
        # On enrôle le partenaire (cause corrigée) pour que le « Handle » aboutisse.
        Partner.objects.filter(code='old').update(status=Partner.Status.ACTIVE)
        Channel.objects.filter(identifier='old').update(active=True)
        self._handle(self.files[0])
        c = self._causes().get('partner_status')
        # La cause a disparu pour ce fichier (il est repassé push) → 2 restants.
        self.assertEqual(c['count'], 2)
        self.assertEqual(c['open_count'], 2)

    def test_handled_is_set_once(self):
        # Recycler un fichier corrigé pose une (et une seule) ligne Handled.
        Partner.objects.filter(code='old').update(status=Partner.Status.ACTIVE)
        Channel.objects.filter(identifier='old').update(active=True)
        self._handle(self.files[0])   # → push + flag posé
        self._handle(self.files[0])   # déjà OK/tranché → 409, pas de doublon
        self.assertEqual(Handled.objects.filter(file=self.files[0]).count(), 1)

    def test_resolved_file_excluded_from_default_view(self):
        # Reproduit le masquage de monitoring_feed (_hide_handled = handled__isnull)
        # — le feed lui-même n'est pas testable ici (regexp_replace PostgreSQL).
        Partner.objects.filter(code='old').update(status=Partner.Status.ACTIVE)
        Channel.objects.filter(identifier='old').update(active=True)
        default = lambda: set(ReceivedFile.objects
                              .filter(handled__isnull=True)
                              .values_list('id', flat=True))
        self._handle(self.files[0])
        self.assertNotIn(self.files[0].pk, default())   # masqué (traité)
        self.assertIn(self.files[1].pk, default())      # les autres restent

    def test_handled_file_counts_respect_hide_and_handled_bucket(self):
        # per_control_class compte par VRAIE classe en respectant le masquage des
        # traités, et les traités sont comptés à part dans le bucket `handled`.
        from django.db.models import Count
        counts = lambda: {
            (r['control_class'] or 'none'): r['n']
            for r in (ReceivedFile.objects
                      .filter(handled__isnull=True)
                      .values('control_class').annotate(n=Count('id')))}
        # 3 fichiers révoqués → control_class worst-wins = warning_action.
        self.assertEqual(counts().get('warning_action'), 3)
        handled = lambda: ReceivedFile.objects.filter(handled__isnull=False).count()
        self.assertEqual(handled(), 0)
        # On corrige la cause puis on en traite un → il quitte sa classe et alimente
        # `handled` (re-contrôle → push, flag posé).
        Partner.objects.filter(code='old').update(status=Partner.Status.ACTIVE)
        Channel.objects.filter(identifier='old').update(active=True)
        self._handle(self.files[0])
        self.assertEqual(counts().get('warning_action'), 2)
        self.assertEqual(handled(), 1)

    def test_resolve_recontrols_and_flips_to_ok_when_cause_fixed(self):
        # « Handle » pose le flag ET re-contrôle : cause corrigée → Recycle → OK.
        rf = make_file(username='newcomer')        # non mappé → recycle
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        # On enrôle le partenaire (+ feed : qualifie aussi), puis on traite.
        enrol('newcomer')
        add_feed('newcomer', 'in/acme', r'.+', route=add_route('newcomer'))  # subfolder par défaut + route
        r = self._handle(rf)
        self.assertEqual(r['control_class'], Event.MonitoringClass.PUSH)
        self.assertTrue(r['handled'])
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)   # OK
        self.assertTrue(Handled.objects.filter(file=rf).exists())       # flag posé

    def test_resolve_recontrols_but_no_flag_if_cause_unfixed(self):
        # Traiter sans corriger la cause : re-contrôle → reste recycle, PAS de flag
        # (couplage strict : le tampon n'existe que pour un OK).
        rf = make_file(username='stranger')        # jamais enrôlé
        file_admission(rf.pk)
        r = self._handle(rf)
        self.assertEqual(r['control_class'], Event.MonitoringClass.RECYCLE)
        self.assertFalse(r['handled'])
        self.assertFalse(Handled.objects.filter(file=rf).exists())


class ReplayAdmissionEndpointTests(TestCase):
    """Bouton « Rejouer l'admission » de la modale (déclencheur du recycle)."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)

    def test_replay_admits_after_enrolment(self):
        # recycle (partenaire non mappé) → enrôlement → rejeu via l'endpoint → admis.
        rf = make_file(username='newcomer')         # subfolder par défaut 'in/acme'
        self.assertEqual(file_admission(rf.pk), VERDICT_RECYCLE)
        enrol('newcomer')
        add_feed('newcomer', 'in/acme', r'.+', route=add_route('newcomer'))  # admis + qualifié + routé → push

        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/admission/{rf.pk}/replay/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['verdict'], VERDICT_ADMIS)
        # Verdict courant admis ; board (control_class) rematérialisé en push.
        self.assertEqual(latest_admission_event(rf).detail['verdict'], VERDICT_ADMIS)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)
        # Audit append-only : les deux verdicts conservés.
        self.assertEqual(verdict_events(rf).count(), 2)

    def test_replay_requires_staff(self):
        rf = make_file(username='ghost')
        # Anonyme : redirigé vers le login admin (pas de rejeu).
        r = self.client.post(f'/monitoring/admission/{rf.pk}/replay/')
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Event.objects.filter(file=rf).exists())

    def test_replay_rejects_get(self):
        # Action mutante : POST only (require_POST).
        rf = make_file(username='ghost')
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(f'/monitoring/admission/{rf.pk}/replay/').status_code, 405)


class QualificationTests(TestCase):
    """Étape qualification (§1.3) : chaînée après un admis, par fichier."""

    def _admit(self, s3_key='in/way/sub/f.csv', username='way'):
        """Enrôle le partenaire et lance l'admission (qui chaîne la qualification)."""
        if not Partner.objects.filter(code=username).exists():
            enrol(username)
        rf = make_file(username=username, s3_key=s3_key)
        from .admission import file_admission
        file_admission(rf.pk)
        rf.refresh_from_db()
        return rf

    def test_no_feed_recycles(self):
        # Admis mais aucune Feed pour (canal, sous-dossier) → recycle.
        rf = self._admit(s3_key='in/way/x.csv')   # subfolder 'in/way', non enrôlé
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_RECYCLE)
        self.assertEqual(ev.cause_code, qual.CAUSE_FEED_NOT_FOUND)
        # worst-wins : recycle (30) prime sur les push de l'admission.
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

    def test_matching_filename_qualifies(self):
        enrol('way')
        add_feed('way', 'in/way', r'.+\.csv', route=add_route('way'))
        rf = self._admit(s3_key='in/way/data.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_QUALIFIED)
        # admis + qualifié + routé → tout push → board push.
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_unmatched_filename_recycles_not_rejects(self):
        # Sous-dossier enrôlé mais le nom ne matche AUCUNE Feed → recycle
        # (le moteur ne reject jamais : un humain tranche). PAS reject.
        enrol('way')
        add_feed('way', 'in/way', r'\d+\.csv')   # n'accepte que des chiffres
        rf = self._admit(s3_key='in/way/abc.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_RECYCLE)
        self.assertEqual(ev.cause_code, qual.CAUSE_FEED_NO_MATCH)
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

    def test_selects_matching_feed_among_several(self):
        # N Feeds au même sous-dossier : la qualif retient celle qui matche.
        enrol('way')
        r_csv, r_txt = add_route('way', code='r-csv'), add_route('way', code='r-txt')
        add_feed('way', 'in/way', r'.+\.csv', route=r_csv)
        add_feed('way', 'in/way', r'.+\.txt', route=r_txt)
        rf = self._admit(s3_key='in/way/data.txt')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        rf.refresh_from_db()
        self.assertEqual(rf.route_id, r_txt.pk)        # routé via la feed .txt

    def test_ambiguous_feeds_recycle(self):
        # Deux grammaires matchent le même nom, même priorité → anomalie → recycle.
        enrol('way')
        add_feed('way', 'in/way', r'.+', priority=0)
        add_feed('way', 'in/way', r'.+\.csv', priority=0)
        rf = self._admit(s3_key='in/way/data.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_RECYCLE)
        self.assertEqual(ev.cause_code, qual.CAUSE_AMBIGUOUS_FEED)

    def test_priority_breaks_overlap(self):
        # Recouvrement résolu par priority (le + haut gagne) → qualifié, pas ambigu.
        enrol('way')
        r_hi = add_route('way', code='r-hi')
        add_feed('way', 'in/way', r'.+', priority=0)
        add_feed('way', 'in/way', r'.+\.csv', priority=10, route=r_hi)
        rf = self._admit(s3_key='in/way/data.csv')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        rf.refresh_from_db()
        self.assertEqual(rf.route_id, r_hi.pk)

    def test_no_filename_constraint_qualifies(self):
        enrol('way')
        add_feed('way', 'in/way')   # grammaire vide → pas de contrainte
        rf = self._admit(s3_key='in/way/anything.bin')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)

    def test_invalid_regex_recycles(self):
        enrol('way')
        add_feed('way', 'in/way', '[')   # regex invalide (config)
        rf = self._admit(s3_key='in/way/data.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_RECYCLE)
        self.assertEqual(ev.cause_code, qual.CAUSE_GRAMMAR_INVALID)

    def test_not_run_when_admission_not_admis(self):
        # Compte non mappé → admission recycle → AUCUN événement de qualification.
        rf = make_file(username='ghost', s3_key='in/ghost/x.csv')
        from .admission import file_admission
        file_admission(rf.pk)
        self.assertFalse(
            Event.objects.filter(file=rf, stage=qual.STAGE).exists())

    def test_replay_requalifies_after_feed_enrolled(self):
        # 1er passage : admis mais pas de feed → recycle. On enrôle, rejeu → qualifié.
        rf = self._admit(s3_key='in/way/data.csv')
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        add_feed('way', 'in/way', r'.+\.csv', route=add_route('way'))  # + route → push après rejeu
        from .admission import file_admission
        file_admission(rf.pk)                     # rejeu : admission + qualification
        rf.refresh_from_db()
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_file_qualification_never_raises(self):
        self.assertIsNone(qual.file_qualification(999999))

    def test_subfolder_is_dirname_of_s3_key(self):
        enrol('way')
        # Feed sur le BON sous-dossier (dirname) → trouvée.
        add_feed('way', 'in/way/deep', r'.+')
        rf = self._admit(s3_key='in/way/deep/f.txt')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)


class EnrolFeedEndpointTests(TestCase):
    """Bouton « Enrôler la feed » de la modale (le recycle de la qualif)."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)

    def _admit_without_feed(self, s3_key='in/way/data.csv'):
        enrol('way')
        rf = make_file(username='way', s3_key=s3_key)
        file_admission(rf.pk)                       # admis, mais qualif → recycle
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        return rf

    def test_payload_flags_needs_feed(self):
        # La modale (admission_detail) expose le drapeau qui révèle le formulaire.
        rf = self._admit_without_feed()
        self.client.force_login(self.staff)
        body = self.client.get(f'/monitoring/admission/{rf.pk}/').json()
        self.assertTrue(body['needs_feed'])
        self.assertEqual(body['subfolder'], 'in/way')

    def test_enrol_creates_feed_then_route_to_push(self):
        # L'enrôlement crée la Feed SANS route → le fichier qualifie mais
        # recycle (route_not_configured). On assigne ensuite la route + rejeu → push.
        rf = self._admit_without_feed()
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/feed/{rf.pk}/enrol/',
                             {'filename_regex': r'.+\.csv'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['feed_created'])
        self.assertFalse(body['needs_feed'])   # feed désormais reconnue
        nom = Feed.objects.get(channel_id=rf.channel_id, subfolder='in/way')
        self.assertEqual(nom.grammar, {'filename': r'.+\.csv'})
        # Qualifié mais pas encore routé → recycle (route_not_configured).
        rf.refresh_from_db()
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        # On accroche une route à la feed + rejeu → push.
        nom.route = add_route('way')
        nom.save(update_fields=['route'])
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)
        self.assertEqual(rf.route_id, nom.route_id)

    def test_enrol_without_regex_means_no_constraint(self):
        rf = self._admit_without_feed(s3_key='in/way/anything.bin')
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/feed/{rf.pk}/enrol/', {})
        self.assertEqual(r.status_code, 200)
        nom = Feed.objects.get(channel_id=rf.channel_id, subfolder='in/way')
        self.assertEqual(nom.grammar, {})              # grammaire vide = pas de contrainte
        self.assertEqual(r.json()['verdict'], VERDICT_ADMIS)

    def test_enrol_rejects_invalid_regex(self):
        rf = self._admit_without_feed()
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/feed/{rf.pk}/enrol/',
                             {'filename_regex': '['})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Feed.objects.exists())   # rien créé sur regex invalide

    def test_enrol_409_when_channel_unresolved(self):
        # Fichier non admis (partenaire non mappé) → pas de canal → 409, rien créé.
        rf = make_file(username='ghost', s3_key='in/ghost/x.csv')
        file_admission(rf.pk)
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/feed/{rf.pk}/enrol/', {})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(Feed.objects.exists())

    def test_enrol_requires_staff(self):
        rf = self._admit_without_feed()
        r = self.client.post(f'/monitoring/feed/{rf.pk}/enrol/', {})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Feed.objects.exists())

    def test_enrol_rejects_get(self):
        rf = self._admit_without_feed()
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(f'/monitoring/feed/{rf.pk}/enrol/').status_code, 405)


class RemediationEndpointTests(TestCase):
    """Actions « Recycle » / « Reject » sur un fichier en échec de contrôle."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(self.staff)

    def _failing_file(self):
        # Compte non mappé → admission recycle → control_class=recycle (échec actionnable).
        rf = make_file(username='ghost', s3_key='in/ghost/x.csv')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        return rf

    def test_reject_forces_reject_over_recycle(self):
        rf = self._failing_file()
        r = self.client.post(f'/monitoring/files/{rf.pk}/reject/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['control_class'], Event.MonitoringClass.REJECT)
        rf.refresh_from_db()
        # Court-circuit : le reject terminal prime sur le recycle (worst-wins inverse).
        self.assertEqual(rf.control_class, Event.MonitoringClass.REJECT)
        self.assertTrue(Event.objects.filter(
            file=rf, stage=Event.Stage.TRIAGE, cause_code='operator_rejected').exists())

    def test_reject_is_terminal_blocks_further_actions(self):
        rf = self._failing_file()
        self.client.post(f'/monitoring/files/{rf.pk}/reject/')
        # Définitif → plus remédiable : recycle ET reject renvoient 409.
        self.assertEqual(self.client.post(f'/monitoring/files/{rf.pk}/recycle/').status_code, 409)
        self.assertEqual(self.client.post(f'/monitoring/files/{rf.pk}/reject/').status_code, 409)

    def test_recycle_replays_controls(self):
        rf = make_file(username='newcomer')   # subfolder défaut in/acme → recycle
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        enrol('newcomer')
        add_feed('newcomer', 'in/acme', r'.+', route=add_route('newcomer'))  # corrige la cause + route
        r = self.client.post(f'/monitoring/files/{rf.pk}/recycle/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['verdict'], VERDICT_ADMIS)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_actions_refused_on_ok_file(self):
        enrol('acme')
        add_feed('acme', 'in/acme', r'.+', route=add_route('acme'))  # route → push (fichier OK)
        rf = make_file(username='acme', s3_key='in/acme/a.csv')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)
        self.assertEqual(self.client.post(f'/monitoring/files/{rf.pk}/recycle/').status_code, 409)
        self.assertEqual(self.client.post(f'/monitoring/files/{rf.pk}/reject/').status_code, 409)
        self.assertFalse(Event.objects.filter(file=rf, stage=Event.Stage.TRIAGE).exists())

    def test_requires_staff(self):
        rf = self._failing_file()
        self.client.logout()
        self.assertIn(
            self.client.post(f'/monitoring/files/{rf.pk}/reject/').status_code, (302, 403))
        self.assertFalse(Event.objects.filter(file=rf, stage=Event.Stage.TRIAGE).exists())

    def test_rejects_get(self):
        rf = self._failing_file()
        self.assertEqual(self.client.get(f'/monitoring/files/{rf.pk}/reject/').status_code, 405)
        self.assertEqual(self.client.get(f'/monitoring/files/{rf.pk}/recycle/').status_code, 405)

    def test_feed_exposes_failed_state_and_remediation(self):
        # Échec MOTEUR sans décision → état affiché « failed » (jamais « rejected »),
        # avec remédiation possible.
        rf = self._failing_file()
        row = next(x for x in self.client.get('/monitoring/feed/').json()['rows']
                   if x['id'] == rf.pk)
        self.assertEqual(row['display_state'], 'failed')
        self.assertTrue(row['can_remediate'])

    def test_rejected_hidden_by_default_visible_via_chip(self):
        rf = self._failing_file()
        self.client.post(f'/monitoring/files/{rf.pk}/reject/')
        # Décision opérateur → état « rejected », absent du board par défaut ; compté
        # dans le bucket dédié `rejected`, plus dans `failed`.
        data = self.client.get('/monitoring/feed/').json()
        self.assertNotIn(rf.pk, [x['id'] for x in data['rows']])
        self.assertEqual(data['per_display_state'].get('rejected'), 1)
        self.assertIsNone(data['per_display_state'].get('failed'))
        # Chip « Rejected » : on le retrouve, terminal (non remédiable).
        row = next(x for x in self.client.get('/monitoring/feed/?control=rejected')
                   .json()['rows'] if x['id'] == rf.pk)
        self.assertEqual(row['display_state'], 'rejected')
        self.assertFalse(row['can_remediate'])
        # Réaffiché aussi par le toggle « show resolved ».
        self.assertIn(rf.pk, [x['id'] for x in
                      self.client.get('/monitoring/feed/?show_handled=1').json()['rows']])

    def test_unmatched_name_shows_failed_remediable_not_rejected(self):
        # Depuis §1.4 le moteur ne reject plus : un nom non conforme → recycle
        # (control_class=recycle) → état affiché « failed » + remédiable. Le seul
        # `rejected` du board vient d'une décision opérateur (cf. test ci-dessous).
        enrol('way')
        add_feed('way', 'in/way', r'\d+\.csv')   # n'accepte que des chiffres
        rf = make_file(username='way', s3_key='in/way/abc.csv')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        row = next(x for x in self.client.get('/monitoring/feed/').json()['rows']
                   if x['id'] == rf.pk)
        self.assertEqual(row['display_state'], 'failed')
        self.assertTrue(row['can_remediate'])

    def test_recycled_and_ok_states(self):
        # Recycle abouti (Handled) → « recycled » (masqué par défaut) ; OK natif → « ok ».
        rf = make_file(username='newcomer')
        file_admission(rf.pk)
        enrol('newcomer')
        add_feed('newcomer', 'in/acme', r'.+', route=add_route('newcomer'))  # route → push
        self.client.post(f'/monitoring/files/{rf.pk}/recycle/')   # → push + Handled
        row = next(x for x in self.client.get('/monitoring/feed/?control=recycled')
                   .json()['rows'] if x['id'] == rf.pk)
        self.assertEqual(row['display_state'], 'recycled')
        self.assertFalse(row['can_remediate'])
        enrol('acme')
        add_feed('acme', 'in/acme', r'.+', route=add_route('acme'))  # route → ok
        ok = make_file(username='acme', s3_key='in/acme/a.csv')
        file_admission(ok.pk)
        row = next(x for x in self.client.get('/monitoring/feed/').json()['rows']
                   if x['id'] == ok.pk)
        self.assertEqual(row['display_state'], 'ok')


class RoutingStageTests(TestCase):
    """Stage routing (§1.4) : pose la clé de dispatch = ``feed.route``.

    Plus de RoutePattern ni de tokens : la Feed matchée par la qualif porte
    sa Route. Jamais sticky, never-raise, le moteur ne reject jamais.
    """
    from . import routing as rout

    def _admit(self, username='way', subfolder='in/way', regex=r'.+\.csv',
               route=None, s3_key='in/way/data.csv'):
        if not Partner.objects.filter(code=username).exists():
            enrol(username)
        add_feed(username, subfolder, regex, route=route)
        rf = make_file(username=username, s3_key=s3_key)
        file_admission(rf.pk)
        rf.refresh_from_db()
        return rf

    def test_feed_route_sets_route_and_pushes(self):
        route = add_route('way') if enrol('way') else None
        rf = self._admit(route=route)
        self.assertEqual(rf.route_id, route.pk)
        ev = self.rout.latest_routing_event(rf)
        self.assertEqual(ev.result, Event.Result.PASSED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        self.assertEqual(ev.detail['route_code'], route.code)
        self.assertEqual(ev.detail['feed_id'],
                         qual.latest_qualification_event(rf).detail['feed_id'])
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_route_not_configured_recycles(self):
        rf = self._admit(route=None)   # Feed sans route
        self.assertIsNone(rf.route_id)
        ev = self.rout.latest_routing_event(rf)
        self.assertEqual(ev.cause_code, self.rout.CAUSE_ROUTE_NOT_CONFIGURED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.RECYCLE)
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

    def test_route_inactive_recycles(self):
        enrol('way')
        rf = self._admit(route=add_route('way', active=False))
        self.assertIsNone(rf.route_id)
        ev = self.rout.latest_routing_event(rf)
        self.assertEqual(ev.cause_code, self.rout.CAUSE_ROUTE_INACTIVE)
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

    def test_route_not_sticky_cleared_on_replay(self):
        enrol('way')
        route = add_route('way')
        rf = self._admit(route=route)
        self.assertEqual(rf.route_id, route.pk)
        route.active = False
        route.save()
        file_admission(rf.pk)              # rejeu : route désormais inactive
        rf.refresh_from_db()
        self.assertIsNone(rf.route_id)     # jamais sticky
        self.assertEqual(self.rout.latest_routing_event(rf).cause_code,
                         self.rout.CAUSE_ROUTE_INACTIVE)

    def test_recycle_loop_end_to_end(self):
        # Feed sans route → recycle ; on accroche la route + rejeu → push.
        enrol('way')
        rf = self._admit(route=None)
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        nom = Feed.objects.get(channel__identifier='way', subfolder='in/way')
        nom.route = add_route('way')
        nom.save(update_fields=['route'])
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.route_id, nom.route_id)
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_routing_skipped_when_not_qualified(self):
        # Admis mais aucune feed → qualif recycle → AUCUN event routing.
        enrol('way')
        rf = make_file(username='way', s3_key='in/way/data.csv')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertFalse(Event.objects.filter(file=rf, stage='routing').exists())
        self.assertIsNone(rf.route_id)

    def test_state_unchanged_throughout(self):
        enrol('way')
        rf = self._admit(route=add_route('way'))
        self.assertEqual(rf.state, ReceivedFile.State.STORED)


class RoutingTeeScenarioTests(TestCase):
    """Cas réel ``tee`` : root-dump multi-extensions, 3 Feeds fines → 3 Routes.

    Modèle final (§1.4) : une Feed par forme (csv/txt/TI.dat) à la racine,
    chacune portant sa Route. Le ``.log`` (et le ``.dat`` non-TI) ne matche aucune
    Feed → recycle (le moteur ne reject jamais).
    """
    from . import routing as rout

    SPECS = [('tee-csv', r'.*\.csv'), ('tee-txt', r'.*\.txt'),
             ('tee-dat', r'TI.*\.dat')]

    def _setup_tee(self):
        enrol('tee')
        for code, regex in self.SPECS:
            add_feed('tee', '', regex, route=add_route('tee', code=code))

    def _admit(self, s3_key):
        rf = make_file(username='tee', s3_key=s3_key)
        file_admission(rf.pk)
        rf.refresh_from_db()
        return rf

    def test_each_form_routes_to_its_route(self):
        self._setup_tee()
        for s3_key, code in (('/YANKEE_1.csv', 'tee-csv'), ('/ZULU_2.txt', 'tee-txt'),
                             ('/TITAN_3.dat', 'tee-dat')):
            rf = self._admit(s3_key)
            self.assertEqual(rf.route.code, code, s3_key)
            self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH, s3_key)

    def test_log_recycles_not_rejects(self):
        # Le moteur ne reject jamais : `.log` ne matche aucune Feed → recycle.
        self._setup_tee()
        rf = self._admit('/UNIFORM_3216.log')
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        self.assertEqual(qual.latest_qualification_event(rf).cause_code,
                         qual.CAUSE_FEED_NO_MATCH)
        self.assertIsNone(rf.route_id)
        self.assertFalse(Event.objects.filter(file=rf, stage='routing').exists())

    def test_non_ti_dat_recycles(self):
        self._setup_tee()
        rf = self._admit('/OTHER_9.dat')
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        self.assertIsNone(rf.route_id)


# Layout NAV de test (forme conforme à validate_layout) : TSV, 3 colonnes.
PARSE_LAYOUT = {
    'format': 'csv', 'delimiter': '\t', 'encoding': 'utf-8',
    'header': {'present': True, 'columns': ['A', 'B', 'C']},
}


def _fake_s3(content_bytes):
    """Client S3 factice : ``get_object(...)['Body'].read()`` rend ``content_bytes``."""
    from unittest.mock import MagicMock
    c = MagicMock()
    c.get_object.return_value = {'Body': io.BytesIO(content_bytes)}
    return c


class ParsingTests(TestCase):
    """Stage parsing (§1.5) : décodage structurel piloté par ``feed.layout``."""

    def _qualified_file(self, filename='NAV.txt', layout=PARSE_LAYOUT, file_size=None):
        """Enrôle tee + une Feed .txt et qualifie un fichier.

        Le parsing étant chaîné dans ``file_admission``, on qualifie d'abord avec
        ``layout={}`` (parse chaîné = passthrough, donc **pas de lecture S3**), PUIS
        on pose le ``layout`` réel pour le test ciblé via ``file_parsing`` (S3 mocké).
        """
        enrol('tee')
        route = add_route('tee')
        nom = add_feed('tee', subfolder='', filename_regex=r'.*\.txt', route=route)
        rf = make_file(username='tee', s3_key=f'/{filename}', path=f'tee/{filename}',
                       file_size=file_size)
        file_admission(rf.pk)  # admis → qualifié → routé → parse passthrough (layout {})
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        nom.layout = layout
        nom.save()
        return rf, nom

    def _parse(self, rf, content):
        with patch('api.parsing.get_s3_client', return_value=_fake_s3(content)):
            return parsing.file_parsing(rf.pk)

    def test_well_formed_tsv_parses(self):
        rf, _ = self._qualified_file()
        verdict = self._parse(rf, b'A\tB\tC\n1\t2\t3\n4\t5\t6\n')
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        self.assertEqual(ev.detail['record_count'], 2)
        self.assertEqual(ev.detail['column_count'], 3)

    def test_header_only_parses_zero_records(self):
        # NAV_02 : en-tête seul, 0 ligne de données → parse OK (la vacuité = §1.7).
        rf, _ = self._qualified_file()
        verdict = self._parse(rf, b'A\tB\tC')
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        self.assertEqual(parsing.latest_parsing_event(rf).detail['record_count'], 0)

    def test_empty_file_recycles_no_header(self):
        rf, _ = self._qualified_file()
        verdict = self._parse(rf, b'')
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        self.assertEqual(parsing.latest_parsing_event(rf).cause_code,
                         parsing.CAUSE_HEADER_MISMATCH)

    def test_malformed_record_recycles(self):
        rf, _ = self._qualified_file()
        verdict = self._parse(rf, b'A\tB\tC\n1\t2\n')  # 2 champs au lieu de 3
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.cause_code, parsing.CAUSE_MALFORMED_RECORD)
        self.assertEqual(ev.detail['record_index'], 1)

    def test_header_mismatch_recycles(self):
        rf, _ = self._qualified_file()
        verdict = self._parse(rf, b'A\tB\tC\tD\n')  # 4 colonnes vs 3 déclarées
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        self.assertEqual(parsing.latest_parsing_event(rf).cause_code,
                         parsing.CAUSE_HEADER_MISMATCH)

    def test_layout_not_declared_passthrough(self):
        # `{}` = accepte tout : la famille sans layout passe (push), sans décodage.
        rf, _ = self._qualified_file(layout={})
        verdict = self._parse(rf, b'anything at all')
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        self.assertTrue(ev.detail.get('passthrough'))

    def test_unsupported_format_recycles(self):
        rf, _ = self._qualified_file(layout={'format': 'xml'})
        verdict = self._parse(rf, b'<x/>')
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        self.assertEqual(parsing.latest_parsing_event(rf).cause_code,
                         parsing.CAUSE_UNSUPPORTED_FORMAT)

    def test_unreadable_s3_recycles(self):
        rf, _ = self._qualified_file()
        c = _fake_s3(b'')
        c.get_object.side_effect = RuntimeError('S3 down')
        with patch('api.parsing.get_s3_client', return_value=c):
            verdict = parsing.file_parsing(rf.pk)
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        self.assertEqual(parsing.latest_parsing_event(rf).cause_code,
                         parsing.CAUSE_UNREADABLE)

    def test_too_large_recycles_without_s3_read(self):
        rf, _ = self._qualified_file(file_size=parsing.DEFAULT_MAX_BYTES + 1)
        c = _fake_s3(b'A\tB\tC\n')
        with patch('api.parsing.get_s3_client', return_value=c):
            verdict = parsing.file_parsing(rf.pk)
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        self.assertEqual(parsing.latest_parsing_event(rf).cause_code,
                         parsing.CAUSE_TOO_LARGE)
        c.get_object.assert_not_called()  # garde taille AVANT toute lecture S3

    def test_not_qualified_returns_none(self):
        # Fichier non qualifié (partenaire non mappé) → rien à parser.
        rf = make_file(username='ghost', s3_key='/x.txt', path='in/x.txt')
        file_admission(rf.pk)
        verdict = self._parse(rf, b'A\tB\tC\n')
        self.assertIsNone(verdict)
        self.assertIsNone(parsing.latest_parsing_event(rf))

    def test_rerun_is_append_only(self):
        rf, _ = self._qualified_file()
        before = Event.objects.filter(file=rf, stage=parsing.STAGE).count()
        self._parse(rf, b'A\tB\tC\n1\t2\t3\n')
        self._parse(rf, b'A\tB\tC\n1\t2\t3\n')
        after = Event.objects.filter(file=rf, stage=parsing.STAGE).count()
        self.assertEqual(after - before, 2)


# §1.5+ — layout enrichi d'un contrat par colonne (TSV ; A=pivot non-null, B=date).
PARSE_LAYOUT_CONTRACTS = {
    'format': 'csv', 'delimiter': '\t', 'encoding': 'utf-8',
    'header': {'present': True, 'columns': ['A', 'B', 'C']},
    'column_contracts': [
        {'name': 'A', 'as': 'sub_fund', 'required': True,
         'nullable': False, 'type': 'string'},
        {'name': 'B', 'as': 'valuation_date', 'required': True,
         'nullable': False, 'type': 'date', 'format': '%Y-%m-%d'},
        {'name': 'C', 'as': 'share_class', 'required': False,
         'nullable': True, 'type': 'string'},
    ],
}


class ParsingColumnContractsTests(ParsingTests):
    """§1.5+ — contrôle de contrat par colonne (présence/non-nullité/type), niveau FICHIER.

    Réutilise les helpers de ``ParsingTests`` (``_qualified_file``/``_parse``). Le gate
    vers l'identification = ``control_class == PUSH`` : un contrôle FAILED le ferme."""

    def test_not_null_violation_recycles_and_gates_identification(self):
        # A (sub_fund, nullable:false) vide sur une ligne → column_not_null FAILED.
        rf, _ = self._qualified_file(layout=PARSE_LAYOUT_CONTRACTS)
        verdict = self._parse(rf, b'A\tB\tC\n\t2026-06-20\tx\n1\t2026-06-20\ty\n')
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.control, parsing.CTRL_COLUMN_NOT_NULL)
        self.assertEqual(ev.cause_code, parsing.CAUSE_COLUMN_NULL)
        self.assertEqual(ev.detail['columns'][0]['column'], 'A')
        self.assertEqual(ev.detail['columns'][0]['empty_rows'], 1)
        rf.refresh_from_db()
        self.assertNotEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_type_violation_recycles_and_gates_identification(self):
        # B (date %Y-%m-%d) reçue en "20260620" → column_type FAILED.
        rf, _ = self._qualified_file(layout=PARSE_LAYOUT_CONTRACTS)
        verdict = self._parse(rf, b'A\tB\tC\n1\t20260620\tx\n')
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.control, parsing.CTRL_COLUMN_TYPE)
        self.assertEqual(ev.cause_code, parsing.CAUSE_COLUMN_TYPE)
        self.assertEqual(ev.detail['columns'][0]['column'], 'B')
        self.assertEqual(ev.detail['columns'][0]['bad_rows'], 1)
        self.assertIn('20260620', ev.detail['columns'][0]['sample'])
        rf.refresh_from_db()
        self.assertNotEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_missing_required_column_recycles(self):
        # Contrat required sur une colonne absente du header → column_present FAILED.
        layout = dict(PARSE_LAYOUT_CONTRACTS)
        layout['column_contracts'] = PARSE_LAYOUT_CONTRACTS['column_contracts'] + [
            {'name': 'Z', 'as': 'isin', 'required': True,
             'nullable': False, 'type': 'string'}]
        rf, _ = self._qualified_file(layout=layout)
        verdict = self._parse(rf, b'A\tB\tC\n1\t2026-06-20\tx\n')
        self.assertEqual(verdict, parsing.VERDICT_RECYCLE)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.control, parsing.CTRL_COLUMN_PRESENT)
        self.assertEqual(ev.cause_code, parsing.CAUSE_COLUMN_MISSING)
        self.assertEqual(ev.detail['missing'], ['Z'])

    def test_conformant_file_parses_and_opens_identification_gate(self):
        # Fichier entièrement conforme au contrat → PARSED, control_class PUSH (gate ouvert).
        rf, _ = self._qualified_file(layout=PARSE_LAYOUT_CONTRACTS)
        verdict = self._parse(rf, b'A\tB\tC\n1\t2026-06-20\tx\n2\t2026-06-21\t\n')
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        ev = parsing.latest_parsing_event(rf)
        self.assertEqual(ev.control, parsing.CTRL_FILE_DECODED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_layout_without_contracts_unchanged(self):
        # Rétro-compat : aucun column_contracts → comportement parsing inchangé.
        rf, _ = self._qualified_file(layout=PARSE_LAYOUT)
        verdict = self._parse(rf, b'A\tB\tC\n\t\t\n')  # vides : OK sans contrat
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        self.assertEqual(parsing.latest_parsing_event(rf).control,
                         parsing.CTRL_FILE_DECODED)


# Contrats à 2 colonnes : B en `number` échoue sur un séparateur de milliers ;
# le passer en `string` retire le contrôle column_type (plus jamais émis).
_CONTRACT_NUM = {
    'format': 'csv', 'delimiter': '\t', 'encoding': 'utf-8',
    'header': {'present': True, 'columns': ['A', 'B']},
    'column_contracts': [
        {'name': 'A', 'type': 'string', 'required': True, 'nullable': False},
        {'name': 'B', 'type': 'number', 'required': True, 'nullable': False},
    ],
}
_CONTRACT_STR = {
    'format': 'csv', 'delimiter': '\t', 'encoding': 'utf-8',
    'header': {'present': True, 'columns': ['A', 'B']},
    'column_contracts': [
        {'name': 'A', 'type': 'string', 'required': True, 'nullable': False},
        {'name': 'B', 'type': 'string', 'required': True, 'nullable': False},
    ],
}


class RunScopeTests(TestCase):
    """``run_scope`` / ``run_id`` : le contextvar et son défaut callable."""

    def test_outside_scope_run_id_is_none(self):
        from .models import _current_run_id
        self.assertIsNone(_current_run_id())

    def test_scope_sets_and_resets(self):
        from .models import _current_run_id, run_scope
        with run_scope() as rid:
            self.assertIsNotNone(rid)
            self.assertEqual(_current_run_id(), rid)
        self.assertIsNone(_current_run_id())   # reset en sortie

    def test_scope_is_reentrant_inner_inherits_outer(self):
        from .models import _current_run_id, run_scope
        with run_scope() as outer:
            with run_scope() as inner:
                self.assertEqual(inner, outer)            # la passe externe prime
                self.assertEqual(_current_run_id(), outer)
            self.assertEqual(_current_run_id(), outer)    # toujours dans l'externe

    def test_event_created_in_scope_is_stamped(self):
        from .models import run_scope
        rf = make_file(username='acme')
        with run_scope() as rid:
            ev = Event.objects.create(
                file=rf, sub_tenant=rf.sub_tenant, stage=Event.Stage.ADMISSION,
                control='c', result=Event.Result.PASSED,
                monitoring_class=Event.MonitoringClass.PUSH)
        ev.refresh_from_db()
        self.assertEqual(ev.run_id, rid)


class RunIdRollupTests(ParsingTests):
    """Rollup scopé à la dernière passe (fix du « contrôle fantôme »).

    Réutilise les helpers de ``ParsingTests`` (``_qualified_file`` / ``_parse``)."""

    def test_admission_pass_shares_one_run_id(self):
        # Une passe file_admission (admission→qualif→routing→parsing) = un seul run.
        enrol('tee')
        route = add_route('tee')
        add_feed('tee', subfolder='', filename_regex=r'.*\.txt', route=route)
        rf = make_file(username='tee', s3_key='/X.txt', path='tee/X.txt')
        file_admission(rf.pk)
        run_ids = set(Event.objects.filter(file=rf).values_list('run_id', flat=True))
        self.assertEqual(len(run_ids), 1)
        self.assertIsNotNone(next(iter(run_ids)))

    def test_standalone_parse_starts_new_run(self):
        # file_admission = passe A (parse passthrough inclus) ; file_parsing = passe B.
        rf, _ = self._qualified_file()
        admission_runs = set(Event.objects.filter(file=rf, stage='admission')
                             .values_list('run_id', flat=True))
        self._parse(rf, b'A\tB\tC\n1\t2\t3\n')
        parse_runs = list(Event.objects.filter(file=rf, stage='parsing')
                          .order_by('id').values_list('run_id', flat=True))
        self.assertGreaterEqual(len(set(parse_runs)), 2)     # ≥2 passes de parsing
        self.assertNotIn(parse_runs[-1], admission_runs)     # dernière ≠ passe admission

    def test_stale_control_excluded_after_contract_fix(self):
        # LE cas NAV_001 : column_type échoue (B=number sur "1,000"), puis on corrige
        # le contrat (B=string) → le fichier décode, le fantôme column_type est écarté.
        rf, nom = self._qualified_file(layout=_CONTRACT_NUM)
        self._parse(rf, b'A\tB\n1\t1,000\n')                 # column_type FAILED
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

        nom.layout = _CONTRACT_STR
        nom.save()                                           # B devient string
        verdict = self._parse(rf, b'A\tB\n1\t1,000\n')       # file_decoded PASSED
        self.assertEqual(verdict, parsing.VERDICT_PARSED)
        rf.refresh_from_db()
        # Le board repasse au vert : la passe courante du parsing n'a plus de column_type.
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)
        # L'échec d'origine survit dans l'audit (append-only) mais hors passe courante.
        self.assertTrue(Event.objects.filter(
            file=rf, control=parsing.CTRL_COLUMN_TYPE,
            result=Event.Result.FAILED).exists())

    def test_rollup_keeps_worst_control_of_latest_pass(self):
        # Worst-wins INTRA-passe préservé : un échec column_type prime sur les autres
        # contrôles PUSH de la même passe de parsing.
        rf, _ = self._qualified_file(layout=_CONTRACT_NUM)
        self._parse(rf, b'A\tB\n1\tnotnum\n')
        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'], Event.MonitoringClass.RECYCLE)
        self.assertEqual(roll['stage'], parsing.STAGE)
        self.assertEqual(roll['control'], parsing.CTRL_COLUMN_TYPE)

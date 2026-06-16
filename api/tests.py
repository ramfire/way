from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .admission import (
    STAGE, VERDICT_ADMIS, VERDICT_QUARANTINE, VERDICT_RECYCLE, file_admission,
    latest_admission_event,
)
from .models import (
    Event, Partner, ReceivedFile, current_control_rollup, refresh_control_class,
)


def make_file(username='acme', s3_key='in/acme/file.csv', **kw):
    """Crée une ligne `stored` minimale (l'admission tourne post-stockage)."""
    return ReceivedFile.objects.create(
        state=ReceivedFile.State.STORED,
        s3_key=s3_key, path=kw.pop('path', s3_key),
        username=username, bucket='alfaway-dev', status=1,
        **kw,
    )


def verdict_events(rf):
    return Event.objects.filter(file=rf, stage=STAGE, control='verdict')


class AdmissionVerdictTests(TestCase):
    def test_admis_when_mapped_active_authorised(self):
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
        rf = make_file(username='acme')

        result = file_admission(rf.pk)

        self.assertEqual(result, VERDICT_ADMIS)
        ev = latest_admission_event(rf)
        self.assertEqual(ev.control, 'verdict')
        self.assertEqual(ev.result, Event.Result.PASSED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.PUSH)
        self.assertEqual(ev.detail['verdict'], VERDICT_ADMIS)
        # state ne reflète QUE le stockage S3 : inchangé.
        rf.refresh_from_db()
        self.assertEqual(rf.state, ReceivedFile.State.STORED)

    def test_recycle_when_partner_unmapped(self):
        rf = make_file(username='ghost')  # aucun Partner

        result = file_admission(rf.pk)

        self.assertEqual(result, VERDICT_RECYCLE)
        ev = latest_admission_event(rf)
        self.assertEqual(ev.result, Event.Result.FAILED)
        self.assertEqual(ev.monitoring_class, Event.MonitoringClass.RECYCLE)
        self.assertEqual(ev.detail['reason'], 'partner_not_mapped')
        # Discovery : on n'a JAMAIS auto-créé le partenaire.
        self.assertFalse(Partner.objects.filter(username='ghost').exists())
        rf.refresh_from_db()
        self.assertEqual(rf.state, ReceivedFile.State.STORED)

    def test_quarantine_and_warning_when_revoked(self):
        Partner.objects.create(username='old', status=Partner.Status.REVOKED)
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
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
        ok_file = make_file(username='acme', s3_key='in/acme/ok.csv')
        bad_file = make_file(username='acme', s3_key='elsewhere/bad.csv')

        self.assertEqual(file_admission(ok_file.pk), VERDICT_ADMIS)
        self.assertEqual(file_admission(bad_file.pk), VERDICT_RECYCLE)
        bad_ev = latest_admission_event(bad_file)
        self.assertEqual(bad_ev.detail['reason'], 'channel_not_authorised')


class AdmissionInitMilestoneTests(TestCase):
    def test_first_admis_flagged_then_subsequent_not(self):
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
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

        # Un humain enrôle le partenaire, puis on rejoue l'admission.
        Partner.objects.create(username='newcomer', status=Partner.Status.ACTIVE)
        self.assertEqual(file_admission(rf.pk), VERDICT_ADMIS)

        # Le verdict COURANT (dernier) est admis ; l'audit conserve les deux.
        self.assertEqual(latest_admission_event(rf).detail['verdict'], VERDICT_ADMIS)
        self.assertEqual(verdict_events(rf).count(), 2)
        # Premier admis pour ce partenaire → milestone d'init posée.
        self.assertTrue(latest_admission_event(rf).detail['first'])

    def test_rerun_is_append_only_no_short_circuit(self):
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
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
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
        rf = make_file(username='acme')
        file_admission(rf.pk)

        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'], Event.MonitoringClass.PUSH)

    def test_quarantine_surfaces_warning_action_over_reject(self):
        # Un fichier quarantine porte un verdict `reject` ET un `warning_action`
        # (révoqué qui émet). Le worst-wins doit remonter le warning_action (plus
        # sévère / actionnable), PAS le verdict reject — c'est le signal à surfacer.
        Partner.objects.create(username='old', status=Partner.Status.REVOKED)
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
        rf = make_file(username='newcomer')
        file_admission(rf.pk)                       # recycle
        Partner.objects.create(username='newcomer', status=Partner.Status.ACTIVE)
        file_admission(rf.pk)                       # admis

        roll = current_control_rollup([rf.pk])[rf.pk]
        self.assertEqual(roll['monitoring_class'], Event.MonitoringClass.PUSH)

    def test_admission_materialises_control_class(self):
        # file_admission rematérialise ReceivedFile.control_class (read-model board).
        Partner.objects.create(username='old', status=Partner.Status.REVOKED)
        rf = make_file(username='old')
        file_admission(rf.pk)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.WARNING_ACTION)

    def test_refresh_handles_bulk_and_null(self):
        rf_none = make_file(s3_key='in/x/none.csv')        # aucun contrôle → NULL
        Partner.objects.create(username='acme', status=Partner.Status.ACTIVE)
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
        Partner.objects.create(username='old', status=Partner.Status.REVOKED)
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


class TriageTests(TestCase):
    """Triage mutable cause × fichier + réconciliation (étape 5)."""

    def setUp(self):
        Partner.objects.create(username='old', status=Partner.Status.REVOKED)
        self.files = [make_file(username='old', s3_key=f'in/old/{i}.csv') for i in range(3)]
        for f in self.files:
            file_admission(f.pk)
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(self.staff)

    def _causes(self):
        return {c['control']: c for c in self.client.get('/monitoring/causes/').json()['causes']}

    _WARN = {'stage': 'admission', 'control': 'partner_status',
             'monitoring_class': 'warning_action', 'reason': 'revoked_partner_still_emitting'}

    def test_cause_claim_resolve_reopen(self):
        r = self.client.post('/monitoring/triage/cause/', {**self._WARN, 'action': 'claim'}).json()
        self.assertEqual((r['status'], r['owner']), ('in_progress', 'staff'))

        self.client.post('/monitoring/triage/cause/', {**self._WARN, 'action': 'resolve'})
        c = self._causes()['partner_status']
        self.assertEqual(c['triage']['status'], 'resolved')
        self.assertEqual(c['open_count'], 0)            # cause résolue → plus rien à traiter

        rr = self.client.post('/monitoring/triage/cause/', {**self._WARN, 'action': 'reopen'}).json()
        self.assertEqual((rr['status'], rr['owner']), ('open', ''))   # désassigné

    def test_file_override_reconciliation(self):
        self.client.post(f'/monitoring/triage/file/{self.files[0].pk}/', {'action': 'resolve'})
        c = self._causes()['partner_status']
        self.assertEqual(c['file_resolved_count'], 1)
        self.assertEqual(c['open_count'], 2)           # 3 - 1 override

        self.client.post(f'/monitoring/triage/file/{self.files[0].pk}/', {'action': 'reopen'})
        self.assertEqual(self._causes()['partner_status']['file_resolved_count'], 0)

    def test_files_open_drops_when_all_covering_causes_resolved(self):
        self.assertEqual(self.client.get('/monitoring/causes/').json()['files_open'], 3)
        for sig in (self._WARN, {'stage': 'admission', 'control': 'verdict',
                                 'monitoring_class': 'reject', 'reason': 'partner_revoked'}):
            self.client.post('/monitoring/triage/cause/', {**sig, 'action': 'resolve'})
        self.assertEqual(self.client.get('/monitoring/causes/').json()['files_open'], 0)

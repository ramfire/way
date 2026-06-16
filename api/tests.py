from django.test import TestCase, override_settings

from .admission import (
    STAGE, VERDICT_ADMIS, VERDICT_QUARANTINE, VERDICT_RECYCLE, file_admission,
    latest_admission_event,
)
from .models import Event, Partner, ReceivedFile


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

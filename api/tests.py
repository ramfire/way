from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .admission import (
    STAGE, VERDICT_ADMIS, VERDICT_QUARANTINE, VERDICT_RECYCLE, file_admission,
    latest_admission_event,
)
from . import qualification as qual
from .models import (
    Channel, Event, Handled, Nomenclature, Partner, ReceivedFile, SubTenant,
    current_control_rollup, refresh_control_class,
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


def add_nomenclature(username, subfolder, filename_regex=None):
    """Enrôle une Nomenclature pour le canal SFTP de ``username``.

    ``filename_regex=None`` ⇒ grammaire vide (aucune contrainte de nom).
    """
    ch = Channel.objects.get(kind=Channel.Kind.SFTP, identifier=username)
    grammar = {'filename': filename_regex} if filename_regex is not None else {}
    return Nomenclature.objects.create(
        channel=ch, sub_tenant=ch.sub_tenant, subfolder=subfolder, grammar=grammar)


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
        add_nomenclature('acme', 'in/acme', r'.+')   # qualifie aussi → push
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
        add_nomenclature('newcomer', 'in/acme', r'.+')   # admis + qualifié → push
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
        add_nomenclature('acme', 'in/acme', r'.+')         # admis + qualifié → push
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
        # Nomenclature permissive : une fois le partenaire ré-activé, le « Handle »
        # peut atteindre push (admis ET qualifié).
        add_nomenclature('old', 'in/old', r'.+')
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
        # On enrôle le partenaire (+ nomenclature : qualifie aussi), puis on traite.
        enrol('newcomer')
        add_nomenclature('newcomer', 'in/acme', r'.+')   # subfolder par défaut
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
        add_nomenclature('newcomer', 'in/acme', r'.+')   # admis + qualifié → push

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

    def test_no_nomenclature_recycles(self):
        # Admis mais aucune Nomenclature pour (canal, sous-dossier) → recycle.
        rf = self._admit(s3_key='in/way/x.csv')   # subfolder 'in/way', non enrôlé
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_RECYCLE)
        self.assertEqual(ev.cause_code, qual.CAUSE_NOMENCLATURE_NOT_FOUND)
        # worst-wins : recycle (30) prime sur les push de l'admission.
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)

    def test_matching_filename_qualifies(self):
        enrol('way')
        add_nomenclature('way', 'in/way', r'.+\.csv')
        rf = self._admit(s3_key='in/way/data.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_QUALIFIED)
        # admis + qualifié → tout push → board push.
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_mismatching_filename_quarantines(self):
        enrol('way')
        add_nomenclature('way', 'in/way', r'\d+\.csv')   # n'accepte que des chiffres
        rf = self._admit(s3_key='in/way/abc.csv')
        ev = qual.latest_qualification_event(rf)
        self.assertEqual(ev.detail['verdict'], qual.VERDICT_QUARANTINE)
        self.assertEqual(ev.cause_code, qual.CAUSE_GRAMMAR_MISMATCH)
        # quarantine (reject=20) prime sur les push → board reject.
        self.assertEqual(rf.control_class, Event.MonitoringClass.REJECT)

    def test_no_filename_constraint_qualifies(self):
        enrol('way')
        add_nomenclature('way', 'in/way')   # grammaire vide → pas de contrainte
        rf = self._admit(s3_key='in/way/anything.bin')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)

    def test_invalid_regex_recycles(self):
        enrol('way')
        add_nomenclature('way', 'in/way', '[')   # regex invalide (config)
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

    def test_replay_requalifies_after_nomenclature_enrolled(self):
        # 1er passage : admis mais pas de nomenclature → recycle. On enrôle, rejeu → qualifié.
        rf = self._admit(s3_key='in/way/data.csv')
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        add_nomenclature('way', 'in/way', r'.+\.csv')
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
        # Nomenclature sur le BON sous-dossier (dirname) → trouvée.
        add_nomenclature('way', 'in/way/deep', r'.+')
        rf = self._admit(s3_key='in/way/deep/f.txt')
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)


class EnrolNomenclatureEndpointTests(TestCase):
    """Bouton « Enrôler la nomenclature » de la modale (le recycle de la qualif)."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user('staff', is_staff=True)

    def _admit_without_nomenclature(self, s3_key='in/way/data.csv'):
        enrol('way')
        rf = make_file(username='way', s3_key=s3_key)
        file_admission(rf.pk)                       # admis, mais qualif → recycle
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.RECYCLE)
        return rf

    def test_payload_flags_needs_nomenclature(self):
        # La modale (admission_detail) expose le drapeau qui révèle le formulaire.
        rf = self._admit_without_nomenclature()
        self.client.force_login(self.staff)
        body = self.client.get(f'/monitoring/admission/{rf.pk}/').json()
        self.assertTrue(body['needs_nomenclature'])
        self.assertEqual(body['subfolder'], 'in/way')

    def test_enrol_creates_nomenclature_and_qualifies(self):
        rf = self._admit_without_nomenclature()
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/nomenclature/{rf.pk}/enrol/',
                             {'filename_regex': r'.+\.csv'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['nomenclature_created'])
        self.assertFalse(body['needs_nomenclature'])   # plus de recycle après enrôlement
        # Nomenclature posée sur (canal, sous-dossier) avec la grammaire saisie.
        nom = Nomenclature.objects.get(channel_id=rf.channel_id, subfolder='in/way')
        self.assertEqual(nom.grammar, {'filename': r'.+\.csv'})
        # Rejeu enchaîné : qualifié → board push.
        rf.refresh_from_db()
        self.assertEqual(qual.latest_qualification_event(rf).detail['verdict'],
                         qual.VERDICT_QUALIFIED)
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_enrol_without_regex_means_no_constraint(self):
        rf = self._admit_without_nomenclature(s3_key='in/way/anything.bin')
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/nomenclature/{rf.pk}/enrol/', {})
        self.assertEqual(r.status_code, 200)
        nom = Nomenclature.objects.get(channel_id=rf.channel_id, subfolder='in/way')
        self.assertEqual(nom.grammar, {})              # grammaire vide = pas de contrainte
        self.assertEqual(r.json()['verdict'], VERDICT_ADMIS)

    def test_enrol_rejects_invalid_regex(self):
        rf = self._admit_without_nomenclature()
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/nomenclature/{rf.pk}/enrol/',
                             {'filename_regex': '['})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Nomenclature.objects.exists())   # rien créé sur regex invalide

    def test_enrol_409_when_channel_unresolved(self):
        # Fichier non admis (partenaire non mappé) → pas de canal → 409, rien créé.
        rf = make_file(username='ghost', s3_key='in/ghost/x.csv')
        file_admission(rf.pk)
        self.client.force_login(self.staff)
        r = self.client.post(f'/monitoring/nomenclature/{rf.pk}/enrol/', {})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(Nomenclature.objects.exists())

    def test_enrol_requires_staff(self):
        rf = self._admit_without_nomenclature()
        r = self.client.post(f'/monitoring/nomenclature/{rf.pk}/enrol/', {})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Nomenclature.objects.exists())

    def test_enrol_rejects_get(self):
        rf = self._admit_without_nomenclature()
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(f'/monitoring/nomenclature/{rf.pk}/enrol/').status_code, 405)


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
        add_nomenclature('newcomer', 'in/acme', r'.+')   # corrige la cause
        r = self.client.post(f'/monitoring/files/{rf.pk}/recycle/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['verdict'], VERDICT_ADMIS)
        rf.refresh_from_db()
        self.assertEqual(rf.control_class, Event.MonitoringClass.PUSH)

    def test_actions_refused_on_ok_file(self):
        enrol('acme')
        add_nomenclature('acme', 'in/acme', r'.+')
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

    def test_feed_exposes_can_remediate(self):
        rf = self._failing_file()
        row = next(x for x in self.client.get('/monitoring/feed/').json()['rows']
                   if x['id'] == rf.pk)
        self.assertTrue(row['can_remediate'])

    def test_rejected_hidden_by_default_visible_via_chip(self):
        rf = self._failing_file()
        self.client.post(f'/monitoring/files/{rf.pk}/reject/')
        # Tranché → absent du board par défaut ; compté dans le bucket dédié `rejected`,
        # plus dans la classe affichée `reject`.
        data = self.client.get('/monitoring/feed/').json()
        self.assertNotIn(rf.pk, [x['id'] for x in data['rows']])
        self.assertEqual(data['per_control_class'].get('rejected'), 1)
        self.assertIsNone(data['per_control_class'].get('reject'))
        # Chip « Rejeté » : on le retrouve, terminal (non remédiable).
        row = next(x for x in self.client.get('/monitoring/feed/?control=rejected')
                   .json()['rows'] if x['id'] == rf.pk)
        self.assertFalse(row['can_remediate'])
        self.assertEqual(row['control_class'], Event.MonitoringClass.REJECT)
        # Réaffiché aussi par le toggle « show resolved ».
        self.assertIn(rf.pk, [x['id'] for x in
                      self.client.get('/monitoring/feed/?show_handled=1').json()['rows']])

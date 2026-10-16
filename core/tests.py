from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from .context_processors import layout
from .layouts import DEFAULT_LAYOUT, get_layout


class AdminLayoutTests(TestCase):
    """Layout client appliqué à l'admin (core/layouts.py, ALFAWAY_LAYOUT)."""

    def test_login_page_uses_generali_css(self):
        resp = self.client.get('/admin/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'layouts/generali/login.css')
        self.assertContains(resp, 'layouts/generali/admin.css')
        self.assertContains(resp, 'layout-generali')

    def test_login_bad_password_shows_alert(self):
        get_user_model().objects.create_superuser('boss', 'boss@example.com', 'good-pass-123')
        resp = self.client.post('/admin/login/', {'username': 'boss', 'password': 'wrong'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="login-alert"')

    def test_superuser_admin_index_has_layout(self):
        user = get_user_model().objects.create_superuser('boss', 'boss@example.com', 'good-pass-123')
        self.client.force_login(user)
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'layouts/generali/admin.css')
        self.assertContains(resp, 'layout-brand')
        self.assertContains(resp, 'Data Transfer Platform')

    @override_settings(ALFAWAY_LAYOUT='inconnu')
    def test_unknown_layout_falls_back_to_generali(self):
        with self.assertLogs('core.layouts', level='WARNING'):
            self.assertEqual(get_layout()['name'], DEFAULT_LAYOUT)
        ctx = layout(RequestFactory().get('/'))
        self.assertEqual(ctx['alfaway_layout']['name'], 'generali')
        resp = self.client.get('/admin/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'layouts/generali/login.css')

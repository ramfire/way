"""Client minimal de l'API Acta (référentiel d'entités, ``acta.algotech.lu``).

Acta fait foi sur les fonds / compartiments / parts : l'identification d'AlfaWay
y vérifie l'existence d'un code au lieu du référentiel local quand le
``Referential`` porte ``backend='acta'``.

Authentification : Acta n'expose que la session Django (``SessionAuthentication``,
TODO(OIDC) côté Acta). Le client se connecte avec le compte de service
``ACTA_USER`` (groupe ``acta.ingest.write``) via ``/accounts/login/`` puis réutilise
le cookie ``sessionid``. Les cookies Acta sont ``Secure`` : on appelle Gunicorn en
local (``ACTA_URL``) avec ``Host`` + ``X-Forwarded-Proto: https`` et on gère les
cookies à la main (``http.cookiejar`` ne renverrait pas un cookie ``Secure`` en http).

Stdlib uniquement (pas de ``requests`` dans le venv). Toute erreur réseau / HTTP /
JSON remonte en ``ActaError`` : l'appelant décide (l'identification → recycle).
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

SOURCE_CODE = 'SOURCE_CODE'


class ActaError(Exception):
    """Acta injoignable, refus d'authentification ou réponse inattendue."""


class ActaTimeout(ActaError):
    """Pas de réponse d'Acta dans le délai : la requête a pu aboutir côté serveur."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None     # le 302 du login est un succès : on lit ses cookies


class ActaClient:
    def __init__(self, base_url=None, host=None, user=None, password=None,
                 timeout=None):
        self.base_url = (base_url or settings.ACTA_URL).rstrip('/')
        self.host = host or settings.ACTA_HOST
        self.user = user or settings.ACTA_USER
        self.password = password or settings.ACTA_PASSWORD
        self.timeout = timeout or settings.ACTA_TIMEOUT
        self._cookies = {}
        self._opener = urllib.request.build_opener(_NoRedirect)
        if not (self.base_url and self.user and self.password):
            raise ActaError('ACTA_URL / ACTA_USER / ACTA_PASSWORD non configurés')

    # --- transport ----------------------------------------------------------
    def _request(self, method, path, data=None, content_type=None, timeout=None):
        headers = {
            'Host': self.host,
            'X-Forwarded-Proto': 'https',
            'Referer': f'https://{self.host}/',
            'Origin': f'https://{self.host}',
            'Accept': 'application/json',
        }
        if self._cookies:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in self._cookies.items())
        if content_type:
            headers['Content-Type'] = content_type
        if method not in ('GET', 'HEAD') and 'csrftoken' in self._cookies:
            headers['X-CSRFToken'] = self._cookies['csrftoken']
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method=method)
        try:
            resp = self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            resp = e     # 3xx/4xx/5xx : on lit statut + cookies, l'appelant tranche
        except TimeoutError as e:
            raise ActaTimeout(f'Acta injoignable ({self.base_url}) : {e}') from e
        except (urllib.error.URLError, OSError) as e:
            if isinstance(getattr(e, 'reason', None), TimeoutError):
                raise ActaTimeout(f'Acta injoignable ({self.base_url}) : {e}') from e
            raise ActaError(f'Acta injoignable ({self.base_url}) : {e}') from e
        for sc in resp.headers.get_all('Set-Cookie') or []:
            name, _, value = sc.split(';', 1)[0].partition('=')
            self._cookies[name.strip()] = value.strip()
        return resp.status, resp.read()

    def login(self):
        status, _ = self._request('GET', '/accounts/login/')
        if status != 200 or 'csrftoken' not in self._cookies:
            raise ActaError(f'page de login Acta inattendue (HTTP {status})')
        body = urllib.parse.urlencode({
            'username': self.user, 'password': self.password,
            'csrfmiddlewaretoken': self._cookies['csrftoken'],
        }).encode()
        status, _ = self._request('POST', '/accounts/login/', body,
                                  'application/x-www-form-urlencoded')
        # Succès = 302 + session ; un échec ré-affiche le formulaire (200).
        if status != 302 or 'sessionid' not in self._cookies:
            raise ActaError(f'authentification Acta refusée (HTTP {status})')

    def get_json(self, path):
        if 'sessionid' not in self._cookies:
            self.login()
        try:
            status, body = self._request('GET', path)
        except ActaTimeout as e:
            # Lecture sans effet de bord : un 2e essai absorbe un ralentissement
            # passager d'Acta (page de 15 s sous charge, fichier 595).
            logger.warning('GET %s : timeout, nouvel essai (%s)', path, e)
            status, body = self._request('GET', path)
        if status != 200:
            raise ActaError(f'GET {path} → HTTP {status}')
        try:
            return json.loads(body)
        except ValueError as e:
            raise ActaError(f'GET {path} : JSON invalide') from e

    def send_json(self, method, path, payload, timeout=None):
        """POST/PUT/DELETE JSON → ``(statut, corps décodé ou None)``. Ne lève que
        sur transport : un 4xx est rendu à l'appelant (motif de rejet Acta).
        ``timeout`` : délai propre à cet appel (sinon ``ACTA_TIMEOUT``)."""
        if 'sessionid' not in self._cookies:
            self.login()
        data = json.dumps(payload).encode() if payload is not None else None
        status, body = self._request(method, path, data,
                                     'application/json' if data else None, timeout)
        try:
            return status, (json.loads(body) if body else None)
        except ValueError:
            return status, {'detail': body[:500].decode('utf-8', 'replace')}

    def iter_results(self, path):
        """Parcourt une liste DRF paginée (``next`` absolu → ramené au chemin)."""
        while path:
            page = self.get_json(path)
            yield from page.get('results') or []
            nxt = page.get('next')
            if nxt:
                parts = urllib.parse.urlsplit(nxt)
                path = parts.path + (f'?{parts.query}' if parts.query else '')
            else:
                path = None

    # --- référentiel --------------------------------------------------------
    def source_codes(self, source):
        """Codes ``SOURCE_CODE`` courants d'une source → ``{code: uid entité}``.

        L'API ne filtre pas par code : on lit toute la source (quelques pages),
        une fois par fichier identifié."""
        q = urllib.parse.urlencode({'source': source})
        return {r['code']: r['entity']
                for r in self.iter_results(f'/api/v1/entity-external-codes/?{q}')
                if r.get('code_type', SOURCE_CODE) == SOURCE_CODE}

    def currencies(self):
        """``{code: {quote_priority, nav_per_unit_decimals, minor_units, …}}``."""
        data = self.get_json('/api/v1/currencies/')
        rows = data if isinstance(data, list) else data.get('results') or []
        return {r['code']: r for r in rows}

    def share_classes_of(self, fund_uid):
        """uids des classes liées à un fonds (aujourd'hui)."""
        data = self.get_json(f'/api/v1/entities/{fund_uid}/share-classes/')
        return {(r.get('entity') or {}).get('uid')
                for r in data.get('share_classes') or []}

    # --- journal des imports de NAV (Acta entities 0020) --------------------
    def nav_imports(self, file_ref):
        """Imports de NAV par l'API portant ce ``file_ref``, du plus récent au plus ancien."""
        q = urllib.parse.urlencode({'file_ref': file_ref, 'channel': 'API'})
        return self.iter_results(f'/api/v1/nav-imports/?{q}')

    def nav_import_lines(self, import_id, outcome=None):
        """Lignes reçues par un import (``outcome='REJECTED'`` pour les refus)."""
        q = f'?{urllib.parse.urlencode({"outcome": outcome})}' if outcome else ''
        return self.iter_results(f'/api/v1/nav-imports/{import_id}/lines/{q}')


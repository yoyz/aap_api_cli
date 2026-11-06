#!/usr/bin/env python3
"""
Serveur AAP factice pour tester aap_api_cli.py sans reseau ni identifiants reels.

  python3 tests/mock_aap.py [port]

Fidelite avec le comportement reel (AAP 2.5, gateway django-ansible-base) :
  * l'URL routing Django a lieu AVANT l'authentification : un chemin inconnu
    repond 404 meme sans credentials, un chemin connu protege repond 401. C'est
    ce qui permet a l'outil de distinguer « route non montee » de « auth refusee »,
    donc le mock respecte cette regle via une table de routes.
  * /api/controller/ -> 204 vide et sous-chemins en 404 HTML : un controller
    heberge sur un autre vhost, la base ne doit pas etre retenue.
  * vue de login en 406 si Accept: application/json (piege qui empechait d'obtenir
    le cookie csrftoken) ; le POST exige Referer + X-Csrftoken egal au cookie.
  * pagination `next` (format controller) pour les users, `next_url` (format
    gateway DAB) pour les job_runs, avec plafond serveur sur page_size.
  * les ressources sont servies aussi bien sous /api/gateway/ que sous
    /api/gateway/v1/ : c'est ce qui verifie la decouverte de variantes d'URL.
  * sortie de job en text/plain avec ANSI + retours CR ; job 102 sans `related`
    (fallback d'URL stdout), job 103 en 404 sur l'objet.
"""
import base64
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
USER_DB = ('testuser', 'testpass')
TOKEN_DB = 'mock-token'
SESSIONS = {}          # cookie aap_session -> login qui a cree la session

USERS = [{'id': i, 'username': 'user%02d' % i, 'email': 'u%d@lab' % i,
          'is_superuser': i == 1, 'is_active': True, 'last_login': None,
          'summary': {'jobs': {'running': i}}}
         for i in range(1, 46)]

TEMPLATES = [
    {'id': 11, 'name': 'patch-linux', 'description': 'Met a jour les rhel',
     'job_type': 'run', 'last_job_run': '2026-11-01T08:00:00Z', 'survey_enabled': False,
     'summary_fields': {'organization': {'name': 'Ops'}}},
    {'id': 12, 'name': 'deploy-app', 'description': 'Deploy app',
     'job_type': 'check', 'last_job_run': None, 'survey_enabled': True,
     'summary_fields': {'organization': {'name': 'Devs'}}},
    # organization sans nom : verifie que dig() ne casse pas
    {'id': 13, 'name': 'backup-etl', 'description': '', 'job_type': 'run',
     'last_job_run': '2026-10-31T22:10:00Z', 'summary_fields': {'organization': {'id': 3}}},
]

WORKFLOWS = [{'id': 21, 'name': 'nightly-full', 'description': 'chaine complete',
              'job_type': 'workflow', 'last_job_run': '2026-11-02T02:00:00Z',
              'summary_fields': {'organization': {'name': 'Ops'}}}]

RUNS = [
    {'id': 101, 'name': 'patch-linux', 'status': 'successful', 'elapsed': 12.5,
     'finished': '2026-11-02T10:00:00Z', 'job_template_name': 'patch-linux',
     'related': {'stdout': '/api/gateway/v1/job_runs/101/stdout/'}},
    # 102 : pas de champ related, le client doit deviner l'URL stdout
    {'id': 102, 'name': 'deploy-app', 'status': 'failed', 'elapsed': 3.2,
     'finished': '2026-11-02T11:00:00Z', 'job_template_name': 'deploy-app'},
    # 103 : objet en 404, le client doit quand meme tenter stdout
    {'id': 103, 'name': 'backup-etl', 'status': 'running', 'elapsed': None,
     'finished': None, 'job_template_name': 'backup-etl'},
]

JOB_LOG = (b'Using /etc/ansible/ansible.cfg\x1b[0m\r\n'
           b'\x1b[91m\x1b[0mPLAY [all] *\x1b[0m\r\n'
           b'TASK [Gathering Facts] *\x1b[0m\r\n'
           b'\x1b[0;32mok: [host1]\x1b[0m\r\n'
           b'\x1b[0;31mfailed: [host2] (item=1) => {"msg": "boom"}\x1b[0m\r\n'
           b'\rProgress: 10%\rProgress: 50%\rProgress: 100%\r\n'
           b'PLAY RECAP *\x1b[0m\r\n'
           b'host1  : ok=2  changed=1  unreachable=0  failed=0\r\n'
           b'host2  : ok=0  changed=0  unreachable=0  failed=1\x07\r\n')

GATEWAY = r'/api/gateway/(?:v1/)?'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # --- ecritures / lecture ---------------------------------------------

    def _send(self, code, body=b'', ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _not_found(self):
        # HTML, comme la page 404 de Django : l'outil doit conclure « route non montee »
        return self._send(404, '<html><head><title>Not Found</title></head>'
                               '<body>Not Found</body></html>', 'text/html')

    def _unauthorized(self):
        return self._send(401, {'detail': 'Authentication credentials were not provided.'})

    def _cookie(self, name):
        for part in self.headers.get('Cookie', '').split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                if key.strip() == name:
                    return value.strip()
        return None

    def _basic(self):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return None
        try:
            user, _, secret = base64.b64decode(header[6:]).decode().partition(':')
        except Exception:
            return None
        return (user, secret)

    def _bearer(self):
        header = self.headers.get('Authorization', '')
        return header[7:] if header.startswith('Bearer ') else None

    def _authed(self):
        if self._cookie('aap_session') in SESSIONS:
            return True
        if self._bearer() == TOKEN_DB:
            return True
        return self._basic() == USER_DB

    def _who(self):
        basic = self._basic()
        if basic:
            return basic[0]
        if self._bearer():
            return 'tokenuser'
        # Une session vaut pour le login qui l'a creee : renvoyer un nom fige
        # ne prouverait pas que le client a transporte le bon cookie.
        return SESSIONS.get(self._cookie('aap_session'), 'anonymous')

    def _reject_json_negotiation(self):
        """Vue DAB hors DRF : refuse Accept: application/json pur (HTTP 406)."""
        accept = self.headers.get('Accept') or ''
        if 'application/json' in accept and 'text/html' not in accept and '*/*' not in accept:
            self._send(406, 'Not acceptable', 'text/html')
            return False
        return True

    @staticmethod
    def _paged(items, qs, self_path, style='next'):
        """`next` = format controller, `next_url` = format gateway DAB."""
        page = int(qs.get('page', ['1'])[0])
        size = int(qs.get('page_size', ['20'])[0])
        size = max(1, min(size, 20))          # plafond serveur, comme l'API reelle
        start = (page - 1) * size
        last = max(1, (len(items) + size - 1) // size)
        nxt = ('%s?page=%d&page_size=%d' % (self_path, page + 1, size)
               if page < last else None)
        return {'count': len(items), 'previous': None, style: nxt,
                'results': items[start:start + size]}

    # --- table de routes (le routing precede l'auth, comme Django) --------

    def routes(self):
        def paged(source, style='next', filter_name=None, filter_template=False):
            def handler(self, qs, path):
                items = source
                if filter_name and qs.get('name'):
                    items = [i for i in source if i['name'] == qs['name'][0]]
                if filter_template and qs.get('job_template__name'):
                    wanted = qs['job_template__name'][0]
                    items = [i for i in source if i.get('job_template_name') == wanted]
                self._send(200, self._paged(items, qs, path, style))
            return handler

        def job_detail(self, qs, path):
            match = re.search(r'/job_runs/(\d+)/', path)
            job_id = int(match.group(1))
            if job_id == 103:
                return self._not_found()
            run = next((r for r in RUNS if r['id'] == job_id), None)
            if not run:
                return self._not_found()
            payload = dict(run, job_template={'name': run['job_template_name']})
            if job_id == 101:
                payload['stdout'] = path + 'stdout/'
            self._send(200, payload)

        return [
            # (regex, authentification requise, handler)
            (r'/$', False, lambda s, q, p: s._send(200, {'aap': 'gateway'})),
            (r'^/api/$', False, lambda s, q, p: s._send(200, {
                'current_version': 'v1',
                'apis': {'gateway': '/api/gateway/', 'controller': '/api/controller/'}})),
            (r'^/api/gateway/$', False, lambda s, q, p: s._send(200, {
                'ping': '/api/gateway/v1/ping/', 'login': '/api/gateway/v1/login/',
                'me': '/api/gateway/v1/me/', 'status': '/api/gateway/v1/status/',
                'users': '/api/gateway/v1/users/', 'session': '/api/gateway/v1/session/',
                'ui_auth': '/api/gateway/v1/ui_auth/',
                'job_templates': '/api/gateway/v1/job_templates/',
                'workflow_job_templates': '/api/gateway/v1/workflow_job_templates/',
                'job_runs': '/api/gateway/v1/job_runs/',
                'nope': '/api/gateway/v1/nope/'})),
            (r'^/api/controller/$', False, lambda s, q, p: s._send(204, b'')),
            (r'^/api/gateway/v1/ping/$', False, lambda s, q, p: s._send(200, {'data': 'ok'})),
            (r'^/api/gateway/v1/ui_auth/$', False, lambda s, q, p: s._send(
                200, {'login_url': '/login', 'is_logged_in': False})),
            (r'^/api/gateway/v1/login/$', False, lambda s, q, p: s.login_view(q, p)),
            (r'^/api/gateway/v1/error406/$', False, lambda s, q, p: s.error_406(q, p)),
            (r'^/api/gateway/v1/error503/$', False, lambda s, q, p: s._send(
                503, {'detail': 'Service Unavailable'})),
            (r'^/api/gateway/v1/forbidden/$', True, lambda s, q, p: s._send(
                403, {'detail': 'Forbidden'})),
            (r'^/api/gateway/v1/nope/$', False, lambda s, q, p: s._not_found()),
            (r'^%sme/$' % GATEWAY, True, lambda s, q, p: s.me_view(q, p)),
            (r'^%ssession/$' % GATEWAY, True, lambda s, q, p: s._send(
                200, {'session': {'pk': 'abc'}, 'agent': {'instances': []}})),
            (r'^%sstatus/$' % GATEWAY, True, lambda s, q, p: s._send(
                200, {'services': {'database': {'healthy': True}},
                      'application_version': '2.5.0'})),
            (r'^%susers/$' % GATEWAY, True, paged(USERS)),
            (r'^%sjob_templates/$' % GATEWAY, True, paged(TEMPLATES, filter_name=True)),
            (r'^%sworkflow_job_templates/$' % GATEWAY, True, paged(WORKFLOWS)),
            (r'^%sjob_runs/$' % GATEWAY, True,
             paged(RUNS, style='next_url', filter_template=True)),
            (r'^%sforeign_next/$' % GATEWAY, True, lambda s, q, p: s._send(200, {
                'count': 2,
                # Piege : le client ne doit jamais envoyer Authorization ici.
                'next': 'http://evil.invalid/api/gateway/v1/foreign_next/?page=2',
                'results': [{'id': 1, 'username': 'user01', 'email': 'u1@lab',
                             'is_superuser': False, 'is_active': True,
                             'last_login': None}]})),
            (r'^%sjob_runs/\d+/stdout/?$' % GATEWAY, True,
             lambda s, q, p: s._send(200, JOB_LOG, 'text/plain')),
            (r'^%sjob_runs/\d+/$' % GATEWAY, True, job_detail),
        ]

    def login_view(self, qs, path):
        if not self._reject_json_negotiation():
            return None
        return self._send(200, '<html><body>login</body></html>', 'text/html',
                          {'Set-Cookie': 'csrftoken=fakecsrf; Path=/; SameSite=Lax'})

    def error_406(self, qs, path):
        if not self._reject_json_negotiation():
            return None
        return self._send(200, {'detail': 'route existe'})

    def me_view(self, qs, path):
        return self._send(200, {'username': self._who(), 'email': 'a@b.c',
                                'is_superuser': True, 'is_system_auditor': False,
                                # Champs sensibles : whoami doit les masquer par
                                # defaut et les reveler seulement avec --full.
                                'password': 'pbkdf2_sha256$mock$hash',
                                'internal_note': 'do-not-show'})

    # --- dispatch --------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        for pattern, auth_required, handler in self.routes():
            if not re.match(pattern, path):
                continue
            if auth_required and not self._authed():
                return self._unauthorized()
            return handler(self, qs, path)
        return self._not_found()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not re.match(r'^/api/gateway/v1/login/$', parsed.path):
            return self._send(405, {'detail': 'Method not allowed'})
        length = int(self.headers.get('Content-Length') or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if not (self.headers.get('Referer') or '').startswith('http://127.0.0.1:%d' % PORT):
            return self._send(403, 'Referer check failed', 'text/html')
        if self._cookie('csrftoken') != self.headers.get('X-Csrftoken'):
            return self._send(403, 'CSRF check failed', 'text/html')
        login = form.get('username', [''])[0]
        secret = form.get('password', [''])[0]
        if (login, secret) != USER_DB:
            return self._send(401, {'detail': 'Invalid credentials provided.'})
        name = 'session-%s' % login
        SESSIONS[name] = login
        return self._send(302, b'', 'text/html', {
            'Location': '/login?next=/',
            'Set-Cookie': 'aap_session=%s; Path=/; httponly' % name})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    print('mock AAP sur http://127.0.0.1:%d' % PORT, flush=True)
    HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()

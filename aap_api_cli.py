#!/usr/bin/env python3
"""
aap_api_cli.py - CLI client for the Red Hat Ansible Automation Platform (AAP 2.5+) API.

Python standard library only (no pip dependencies).

Principles:
  * one config profile = one host + user/password + tls_insecure
  * nothing is fixed: the API bases actually published by the host are discovered,
    because the gateway does not necessarily mount controller/hub on the same vhost
  * commands are declared with decorators (@command + @options): adding a feature
    = one function + one decorator (+ a logical endpoint if needed)
  * table output (human) or json/csv/yaml (scriptable: jq, awk...)

Main commands: templates (job templates), logs (job output), jobs.
"""

VERSION = '0.1'

import os
import re
import sys
import ssl
import json
import cmd
import csv
import base64
import shlex
import getpass
import argparse
import configparser
import http.cookiejar
import urllib.request
import urllib.error
import urllib.parse

CONFIG_PATH = os.path.expanduser('~/.aap_api_cli.conf')

# Fallback bases when the server publishes none usable during discovery.
API_BASE_FALLBACKS = ['/api/gateway/', '/api/controller/']

# Probe suffixes used to verify that an API base actually responds.
BASE_PROBE_SUFFIXES = ('', 'v1/', 'v2/', 'api/v2/')

DEFAULT_ENDPOINTS = {
    # gateway (django-ansible-base)
    'status': '/api/gateway/v1/status/',
    'ping': '/api/gateway/v1/ping/',
    'me': '/api/gateway/v1/me/',
    'users': '/api/gateway/v1/users/',
    'session': '/api/gateway/v1/session/',
    'login': '/api/gateway/v1/login/',
    'ui_auth': '/api/gateway/v1/ui_auth/',
    'settings': '/api/gateway/v1/settings/',
    'tokens': '/api/gateway/v1/tokens/',
    'organizations': '/api/gateway/v1/organizations/',
    'teams': '/api/gateway/v1/teams/',
    # controller: the API is AWX's, mounted under /api/controller/v2/.
    # /api/controller/ is only an index, and /api/controller/api/v2/ (the
    # historical pipeline) does not exist on this installation.
    'job_templates': '/api/controller/v2/job_templates/',
    'job_runs': '/api/controller/v2/jobs/',
    'jobs': '/api/controller/v2/jobs/',
    'workflow_job_templates': '/api/controller/v2/workflow_job_templates/',
    'inventories': '/api/controller/v2/inventory/',
    'execution_environments': '/api/controller/v2/execution_environments/',
}

# Segments with no resource name: an API index publishes them as versions, never
# as collections. A default path such as /api/controller/v2/jobs/ must therefore
# be re-anchored on a base discovered by its resource name alone ("jobs"),
# otherwise it builds /api/controller/v2/v2/jobs/.
NON_RESOURCE_SEGMENTS = re.compile(r'^(api|restapi|v\d+(\.\d+)*)$', re.IGNORECASE)

# The gateway and the controller do not name collections the same way.
# These aliases let us find /api/controller/v2/jobs/ when asking for
# "job_runs" (the CLI's logical name), or the reverse.
ENDPOINT_ALIASES = {
    'job_runs': ['jobs', 'unified_jobs'],
    'jobs': ['job_runs', 'unified_jobs'],
    'inventories': ['inventory'],
    'inventory': ['inventories'],
    'job_templates': ['unified_job_templates'],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def yesno(value):
    if value is None:
        return None
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


def env_value(name, legacy=None):
    """
    Environment variable, with a historical alias (RHAAP_* -> AAP_*).
    """
    value = os.environ.get(name)
    if value is None and legacy:
        value = os.environ.get(legacy)
    return value


def non_negative_int(value):
    """
    argparse type: integer >= 0 (--tail 0 must mean "no line").
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('integer expected, got %r' % value) from None
    if number < 0:
        raise argparse.ArgumentTypeError('must be positive or zero, got %d' % number)
    return number


def warn(msg):
    print('[WARNING] %s' % msg, file=sys.stderr)


def to_list(data):
    """
    Items from a REST response, across all pagination formats.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('results', 'data', 'items', 'objects'):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def unwrap_object(data):
    """
    Single instance, whether the server returned it raw or in an envelope.
    
    /me/ returns either the user object or its paginated envelope
    {count, next, results:[object]} depending on the component serving the route.
    Both are accepted without assumptions, as everywhere else in the client.
    """
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return {}
    items = to_list(data)
    if items and isinstance(items[0], dict):
        return items[0]
    return data


def next_url(data):
    """
    URL of the next page (controller / gateway / DRF formats).
    """
    if not isinstance(data, dict):
        return ''
    for key in ('next_url', 'next'):
        if data.get(key):
            return data[key]
    links = data.get('links')
    if isinstance(links, dict) and links.get('next'):
        return links['next']
    return ''


def rel_url(url, base):
    """
    URL made relative to the base, for compact messages.
    """
    if not url:
        return '-'
    if base and url.startswith(base):
        return url[len(base):] or '/'
    return url


def dig(item, key):
    """
    Access a nested field by 'a.b.c' path (used by --fields).
    """
    value = item
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def hdr(headers, name, default=''):
    """
    Value of a header. Headers are a list of pairs, not a dict: an HTTP response
    often carries several Set-Cookie headers (csrftoken + session), and converting
    them to a dict would keep only one, breaking the CSRF token.
    """
    for key, value in headers or ():
        if key.lower() == name.lower():
            return value
    return default


def hdr_all(headers, name):
    """
    All values of a repeatable header (typically Set-Cookie).
    """
    return [value for key, value in headers or () if key.lower() == name.lower()]


def template_name(item):
    """
    Name of the job template for a run.
    
    The controller API (AWX) puts neither the name nor the id in clear: job_template
    carries the id, and the name only appears in summary_fields.job_template.name
    (or unified_job_template for a system job). The other forms (inline dict, flat
    field) remain supported for the gateway and older versions.
    """
    summary = item.get('summary_fields')
    if isinstance(summary, dict):
        for key in ('job_template', 'unified_job_template'):
            nested = summary.get(key)
            if isinstance(nested, dict) and nested.get('name'):
                return nested['name']
    tpl = item.get('job_template')
    if isinstance(tpl, dict):
        return tpl.get('name')
    return item.get('job_template_name') or item.get('name')


ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
VT_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def clean_stdout(text, mode='auto'):
    """
    Clean a job's raw output (ANSI colors, progress CR, BELL).
      auto   : strip ANSI unless stdout is a terminal
      always : always strip ANSI
      raw    : touch nothing (for a faithful `less -R`)
    """
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if mode == 'raw':
        return text
    text = text.replace('\x1b[91m\x1b[0m', '')
    if mode == 'always' or not sys.stdout.isatty():
        text = ANSI_RE.sub('', text)
    text = text.replace('\x07', '')
    return VT_RE.sub('', text)


def _table_helpers():
    """
    Real character width (CJK glyphs count as 2 columns).
    """
    try:
        import unicodedata

        def clen(s):
            return sum(0 if unicodedata.combining(c)
                       else (2 if unicodedata.east_asian_width(c) in 'WF' else 1)
                       for c in s)
    except ImportError:
        def clen(s):
            return len(s)

    def pad(value, width):
        return value + ' ' * max(0, width - clen(value))
    return clen, pad


def table(rows, headers, widths, sep='  '):
    """
    Aligned table. An 'auto' width adjusts to the widest cell.
    """
    clen, pad = _table_helpers()
    rows = [list(row) for row in rows]
    # 'auto' widths are first reduced to 0; the flag is kept separately:
    # without it, an empty header (width 0) froze the column and truncated values.
    automatic = [width == 'auto' for width in widths]
    widths = [0 if flag else width for flag, width in zip(automatic, widths)]

    def consider(value, index):
        if automatic[index]:
            widths[index] = max(widths[index], clen('' if value is None else str(value)))

    if headers:
        headers = ['' if head is None else str(head) for head in headers]
        for index, head in enumerate(headers):
            consider(head, index)
    for row in rows:
        for index, cell in enumerate(row):
            consider(cell, index)
    for index, flag in enumerate(automatic):
        if flag and not widths[index]:
            widths[index] = 12

    def cell_str(value, width):
        value = '' if value is None else str(value)
        if clen(value) > width:
            value = value[:max(0, width - 1)] + '~'
        return pad(value, width)

    out = []
    if headers:
        out.append(sep.join(cell_str(headers[i], widths[i])
                            for i in range(len(headers))).rstrip())
    for row in rows:
        out.append(sep.join(cell_str(row[i], widths[i])
                            for i in range(len(row))).rstrip())
    return out


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

REGISTRY = {}
ORDER = []


def command(name, help_text):
    """
    Declare a command. One registration per name: a second @command with the same
    name would silently replace the action, so we guard against it.
    """
    def deco(func):
        existing = REGISTRY.get(name)
        if existing and existing['action'] is not None:
            raise RuntimeError('@command(%r) declared twice' % name)
        REGISTRY[name] = {'name': name, 'help': help_text,
                          'action': func, 'options': None}
        ORDER.append(name)
        return func
    return deco


def options(name):
    """
    Attach a declared command's argparse options before it.
    """
    def deco(func):
        if name not in REGISTRY:
            raise RuntimeError('@options(%r) precedes @command(%r)' % (name, name))
        if REGISTRY[name]['options'] is not None:
            raise RuntimeError('@options(%r) declared twice' % name)
        REGISTRY[name]['options'] = func
        return func
    return deco


def no_options(sub):
    """
    Subparser with no specific options.
    """
    return None


# ---------------------------------------------------------------------------
# Business errors + diagnostic tips
# ---------------------------------------------------------------------------

def transport_tip(base):
    """One-line hint for an unreachable host, with the real host and URL."""
    host = urllib.parse.urlparse(base).hostname or base
    return ('[TIP] check VPN/network and DNS (`getent hosts %s`), '
            'then `curl -vk %s/api/`' % (host, base))


def join_failure(base, reason, detail=None):
    """
    Error for "no usable HTTP response".

    Must be absolutely distinguished from a 401 or 404: here no request was
    understood by the server, so no tip about credentials or the vhost is correct.
    The tip still depends on the cause: a TLS certificate rejection calls for
    `ca_bundle`, not a VPN check.
    """
    text = str(reason or 'no response')
    lowered = text.lower()
    tls = any(word in lowered for word in ('tls', 'ssl', 'certificate'))
    return AapError('cannot reach %s: %s' % (base, text),
                    detail=detail, transport=True,
                    tip=TIP_TLS if tls else transport_tip(base))


class AapError(Exception):
    def __init__(self, msg, detail=None, status=None, tip=None, transport=False):
        super().__init__(msg)
        self.detail = detail
        self.status = status
        # Business tip forced by the raiser: a network failure has nothing to do
        # with a 401, and inheriting the default HTTP-status TIP would make us look
        # for a credentials problem that does not exist.
        self.tip = tip
        # True when no HTTP response could be obtained (DNS, connection
        # refused, timeout, certificate). Autodetection probes must not
        # interpret that silence as a 404, and login() must report it as-is
        # instead of concluding that credentials were refused.
        self.transport = transport


TIPS = {
    401: '[TIP] 401: credentials rejected, or auth method not accepted by this gateway. Check the profile, or create a token and set auth_method = token.',
    403: '[TIP] 403: insufficient role (role_user_assignments), or CSRF token rejected.',
    404: '[TIP] 404: this component is not published on this vhost (normal for a controller hosted elsewhere). See `endpoints` and `api --discover`, then override with --endpoint key=/path or another profile.',
    503: '[TIP] 503: service or dependency unavailable; check `api /api/gateway/v1/ping/` (db_connected / proxy_connected).',
}


def _error_tip(err):
    """
    Business tip: the one carried by the error, otherwise the one for its HTTP status.
    """
    return getattr(err, 'tip', None) or TIPS.get(getattr(err, 'status', None))


def print_error(err, file=None):
    """
    Render an error readably. `file` is stdout first in the interactive shell (the
    shell already prints its own messages to stdout: mixing both streams loses
    display order when output is captured or replayed); stderr elsewhere, so a
    script can separate data from diagnostics.
    """
    stream = sys.stderr if file is None else file
    print('[ERROR] %s' % err, file=stream)
    detail = getattr(err, 'detail', None)
    debug = bool(env_value('AAP_DEBUG', 'RHAAP_DEBUG'))
    # For a transport failure the detail is the list of probed bases: useful for
    # debugging, noise for an operator who mostly needs the one-line tip.
    if detail and (debug or not getattr(err, 'transport', False)):
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        print('  ' + text[:600].replace('\n', ' '), file=stream)
    tip = _error_tip(err)
    if tip:
        print(tip, file=stream)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

class Credentials:
    """
    Holds a profile's credentials, deliberately separate from the rest: values are
    never displayed (neither in logs, nor `endpoints`, nor the repr); only auth
    methods are named.
    
    `method = token` without a token is an error, never a fallback to basic: the
    password is not read in that mode, so the 401 that would follow would be
    misleading.
    """

    def __init__(self, login=None, passe=None, jeton=None, method='basic'):
        self.login = login or ''
        self.passe = passe or ''
        self.jeton = jeton or ''
        self.method = (method or 'basic').strip().lower()
        if self.method not in ('basic', 'session', 'token'):
            warn('unknown auth method %r, using basic' % self.method)
            self.method = 'basic'
        if self.method == 'token' and not self.jeton:
            # No silent fallback to basic: the password was not read in that case,
            # and the following 401 would look like a basic-credentials refusal.
            raise AapError('auth_method = token but no token configured: set `token` in the profile or `--token`, or choose --auth basic/session')

    def describe(self):
        return self.method

    def __repr__(self):
        return '<Credentials login=%r method=%s>' % (self.login, self.method)


# ---------------------------------------------------------------------------
# Client HTTP
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Login POSTs must not be followed: the 302 signals success.
    """

    def redirect_request(self, *args, **kwargs):
        return None


class AAPClient:
    """
    REST client for the AAP API.
    
    Auth, three methods (carried by Credentials):
      basic   : Authorization: Basic    (the AAP 2.5 gateway accepts it directly)
      session : POST x-www-form-urlencoded + X-Csrftoken header + Referer -> cookie
      token   : Authorization: Bearer
    
    No hard-coded path is assumed correct: API bases are published by the gateway
    itself; we read them and probe. What is verified here was confirmed on the
    target installation; the rest is overridable (`endpoints`).
    """

    def __init__(self, host, creds, insecure=False, timeout=30, ca_bundle=None):
        self.base = self.normalize(host)
        self.creds = creds
        self.timeout = timeout
        self.insecure = bool(insecure)
        self.ca_bundle = ca_bundle
        self._api_bases = None
        self._resource_index = None
        self._forced = set()
        self._endpoints = dict(DEFAULT_ENDPOINTS)
        self._resolved = {}
        self._cj = http.cookiejar.CookieJar()
        self._ssl = self._build_ssl()
        # Last login() failure kept as an exception: main() needs to
        # know whether the failure is network or business, so it does not dump a tip
        # about credentials when no request succeeded.
        self._login_failure = None

    @property
    def auth_method(self):
        return self.creds.method

    # --- transport -------------------------------------------------------

    @staticmethod
    def normalize(host):
        host = (host or '').strip().rstrip('/')
        if not host:
            raise AapError('`host` key required in the profile')
        if not host.startswith(('http://', 'https://')):
            host = 'https://' + host
        return host.rstrip('/')

    def _build_ssl(self):
        """
        TLS context. Disabling verification remains an explicit profile choice
        (`tls_insecure = true`) and is reminded on every run: an AAP gateway grants
        access to privileged credentials, so certificate verification is not disabled
        silently.
        """
        if self.ca_bundle:
            path = os.path.expanduser(self.ca_bundle)
            if not os.path.exists(path):
                raise AapError('ca_bundle not found: %s' % path)
            ctx = ssl.create_default_context(cafile=path)
        else:
            ctx = ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            warn('TLS verification DISABLED (tls_insecure): acceptable in a lab with a self-signed certificate, replace with ca_bundle as soon as possible')
        return ctx

    def _opener(self, follow=False):
        handlers = [urllib.request.HTTPCookieProcessor(self._cj),
                    urllib.request.HTTPSHandler(context=self._ssl)]
        if not follow:
            handlers.append(_NoRedirect())
        return urllib.request.build_opener(*handlers)

    def abs_url(self, url):
        return self.base + url if url.startswith('/') else url

    def _auth_header(self):
        if self.auth_method == 'basic' and self.creds.login:
            raw = ('%s:%s' % (self.creds.login, self.creds.passe)).encode()
            return 'Basic ' + base64.b64encode(raw).decode()
        if self.auth_method == 'token' and self.creds.jeton:
            return 'Bearer ' + self.creds.jeton
        return None

    def http(self, path, method='GET', body=None, headers=None, follow=False, auth=True):
        """
        Low level. Returns (status|None, headers, text) without ever raising for an
        HTTP status >= 400: the caller decides what to do with each status. Only
        network/TLS failures raise AapError.
        
        auth=False is for discovery probes: sending a wrong Basic header makes even
        public views fail (ping, ui_auth), which would make all autodetection silent.
        """
        data = None
        ctype = None
        if body is not None:
            if isinstance(body, bytes):
                data = body
            else:
                data = json.dumps(body).encode()
                ctype = 'application/json'
        req = urllib.request.Request(self.abs_url(path), data=data, method=method)
        req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', 'aap_api_cli/' + VERSION)
        if ctype:
            req.add_header('Content-Type', ctype)
        headers = dict(headers or {})
        for key, value in headers.items():
            req.add_header(key, value)
        authorization = self._auth_header() if auth else None
        if authorization:
            req.add_header('Authorization', authorization)
        if self.auth_method == 'session' and auth and 'Referer' not in headers:
            # Django uses SameSite=Lax with strict Referer checking
            req.add_header('Referer', self.base + '/')

        if env_value('AAP_DEBUG', 'RHAAP_DEBUG'):
            print('# > %s %s (auth=%s)' % (method, self.abs_url(path), self.auth_method),
                  file=sys.stderr)
        try:
            with self._opener(follow=follow).open(req, timeout=self.timeout) as resp:
                return resp.getcode(), list(resp.headers.items()), resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode('utf-8', 'replace')
            except Exception:
                text = ''
            return exc.code, list((exc.headers or {}).items()), text
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', exc)
            if 'CERTIFICATE_VERIFY_FAILED' in str(reason) or 'SSL' in type(reason).__name__:
                raise AapError(
                    """TLS certificate rejected for %s
  -> ca_bundle = /path/to/my-ca.pem in the profile (recommended), or
  -> tls_insecure = true to disable verification""" % self.base)
            return None, [], str(reason)
        except ssl.SSLError as exc:
            raise AapError('TLS error to %s: %s' % (self.base, exc))
        except OSError as exc:
            raise AapError('network error to %s: %s'
                           % (self.base, getattr(exc, 'strerror', None) or exc))

    @staticmethod
    def parse_body(text):
        text = (text or '').strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    @staticmethod
    def error_detail(body):
        """
        Extract a useful message, including when the response is HTML.
        """
        data = AAPClient.parse_body(body)
        if isinstance(data, dict):
            for key in ('detail', 'msg', 'message', 'error'):
                if data.get(key):
                    return data[key]
            if 'fields' in data:
                return json.dumps(data, ensure_ascii=False)[:400]
            return None
        if isinstance(data, str):
            match = re.search(r'CSRF[^<]{0,200}', data)
            if match:
                return match.group(0)
            match = re.search(r'<title>([^<]{0,120})', data, re.IGNORECASE)
            if match:
                return 'HTML: %s' % match.group(1).strip()
            return next((line[:200] for line in data.splitlines() if line.strip()), None)
        return None

    def request(self, path, method='GET', params=None, body=None, headers=None,
                quiet=False, follow=False, auth=True):
        """
        Object-level request, with an actionable AapError on failure.
        """
        url = path
        if params:
            query = urllib.parse.urlencode([(k, v) for k, v in params.items()
                                            if v is not None and v != ''])
            if query:
                url += ('&' if '?' in url else '?') + query
        status, hdrs, text = self.http(url, method, body, headers, follow=follow, auth=auth)
        if status is None:
            if not quiet and env_value('AAP_DEBUG', 'RHAAP_DEBUG'):
                print('[ERROR] %s %s : %s' % (method, self.abs_url(url), str(text)[:300]),
                      file=sys.stderr)
            raise AapError('cannot reach %s: %s' % (self.base, text or 'no response'),
                           detail=text, tip=transport_tip(self.base), transport=True)
        if 200 <= status < 400:
            return status, hdrs, text
        raise AapError('HTTP %d on %s %s' % (status, method, rel_url(url, self.base)),
                       detail=self.error_detail(text), status=status)

    def get_json(self, path, **kwargs):
        _, _, text = self.request(path, **kwargs)
        return self.parse_body(text)

    # --- authentication ------------------------------------------------

    def _csrf_endpoint(self):
        """
        Return (path, token). The URL that delivers the csrftoken cookie depends on the
        installation: probe the logical 'login' endpoint (overridable in config), then
        the discovered bases, and remember the one that works.
        """
        if 'csrf' in self._resolved:
            return self._resolved['csrf']
        candidates = []
        login = self._endpoints.get('login')
        if login:
            candidates.append(login)
        for base in self.api_bases():
            candidates.append(base.rstrip('/') + '/login/')
        errors, seen = [], set()
        silence, last_transport = 0, None
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                status, hdrs, _ = self.http(path, auth=False,
                                            headers={'Accept': 'text/html, */*'})
            except AapError as exc:
                errors.append('%s: %s' % (rel_url(path, self.base), exc))
                silence += 1
                last_transport = str(exc)
                continue
            if status is None:
                silence += 1
            for value in hdr_all(hdrs, 'set-cookie'):
                if 'csrftoken=' in value:
                    csrf = value.split('csrftoken=', 1)[1].split(';')[0].strip()
                    if csrf:
                        found = (path, csrf)
                        self._resolved['csrf'] = found
                        return found
            errors.append('%s: HTTP %s, no csrftoken cookie'
                          % (rel_url(path, self.base), status))
        if candidates and silence == len(candidates):
            raise AapError('cannot reach %s while fetching the CSRF token: %s'
                           % (self.base, last_transport or 'probe silent'),
                           detail='; '.join(errors) or None,
                           tip=transport_tip(self.base), transport=True)
        raise AapError('no login endpoint found to obtain a CSRF token',
                       detail='; '.join(errors) or None)

    def _transport_failure(self, exc):
        """
        A network failure translated into an authentication message would be a false
        diagnosis: "credentials refused" when no request reached the server wastes
        considerable time. Report the raw cause instead, without concluding anything
        about auth.
        """
        self._login_failure = exc
        return False, str(exc)

    def login(self, creds=None, method=None):
        """
        Authenticate the client. Returns (ok, readable_message_without_secret).
        
        Failures are also remembered on self._login_failure as an exception: the caller
        needs to distinguish an authentication refusal from a total absence of HTTP
        response, because the tip to give has nothing in common and the latter wrongly
        points to credentials.
        """
        self._login_failure = None
        if creds:
            self.creds = creds
        if method:
            self.creds.method = method.strip().lower()

        if self.auth_method == 'token':
            try:
                data = self.get_json(self.resolve('me'))
            except AapError as exc:
                if getattr(exc, 'transport', False):
                    return self._transport_failure(exc)
                self._login_failure = exc
                return False, 'HTTP %s with the token: %s' % (
                    exc.status, (str(exc.detail) or str(exc))[:150])
            return True, 'token OK, identity = %s' % self._who(data)

        if self.auth_method == 'session':
            return self._login_session()

        last = None
        for key in ('me', 'users'):
            try:
                params = None if key == 'me' else {'page_size': 1}
                data = self.get_json(self.resolve(key), params=params)
            except AapError as exc:
                if getattr(exc, 'transport', False):
                    return self._transport_failure(exc)
                last = exc
                continue
            if key == 'me':
                return True, 'basic OK, identity = %s' % self._who(data)
            return True, 'basic OK (verified via %s)' % rel_url(
                self._resolved.get(key, ''), self.base)
        self._login_failure = last
        detail = (str(getattr(last, 'detail', '')) or str(last))[:160]
        return False, 'HTTP %s: %s' % (getattr(last, 'status', '?'), detail)

    @staticmethod
    def _who(data):
        """
        Name of the current identity. /me/ may return the raw object OR a paginated
        envelope {count, next, results[...]} depending on the component.
        """
        data = unwrap_object(data)
        return data.get('username') or data.get('email') or '?'

    def _login_session(self):
        if not self.creds.login or not self.creds.passe:
            return False, 'login and password required for auth_method = session'
        try:
            path, csrf = self._csrf_endpoint()
        except AapError as exc:
            if getattr(exc, 'transport', False):
                return self._transport_failure(exc)
            return False, 'CSRF token unavailable: %s' % exc
        form = urllib.parse.urlencode({'username': self.creds.login,
                                       'password': self.creds.passe})
        # The UI sends X-Csrftoken + Referer; without Referer Django returns 403, and the
        # token must match the csrftoken cookie (double submit).
        try:
            status, hdrs, text = self.http(
                path, method='POST', body=form.encode(),
                headers={'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                         'Accept': 'text/html, */*',
                         'X-Csrftoken': csrf,
                         'Referer': self.base + '/login'})
        except AapError as exc:
            if getattr(exc, 'transport', False):
                return self._transport_failure(exc)
            return False, 'POST %s: %s' % (rel_url(path, self.base), exc)
        if status is None:
            exc = AapError('cannot reach %s (POST): %s'
                           % (rel_url(path, self.base), text or 'probe silent'),
                           tip=transport_tip(self.base), transport=True)
            return self._transport_failure(exc)
        if status in (301, 302, 303, 308):
            cookies = hdr_all(hdrs, 'set-cookie')
            names = sorted(set(re.findall(r'([A-Za-z0-9_\-]{3,})=', ' '.join(cookies))))
            return True, 'login OK (HTTP %d -> %s) cookies=%s' % (
                status, hdr(hdrs, 'Location', '?'), ','.join(names) or 'none')
        detail = self.error_detail(text) or ''
        if status == 403:
            return False, 'HTTP 403 CSRF rejected on %s (%s)' % (
                rel_url(path, self.base), detail[:120])
        if status == 401:
            return False, 'HTTP 401: credentials rejected by %s' % rel_url(path, self.base)
        return False, 'HTTP %s: %s' % (status, detail[:160])

    # --- API base discovery ---------------------------------------------

    def published_bases(self):
        """
        Bases published by the API root ({"apis":{"gateway":"/api/gateway/"}}).
        """
        for root in ('/api/', '/'):
            try:
                _, _, text = self.http(root, auth=False)
            except AapError:
                continue
            data = self.parse_body(text)
            if not isinstance(data, dict):
                continue
            found = []
            groups = [data]
            if isinstance(data.get('apis'), dict):
                groups.append(data['apis'])
            for group in groups:
                for value in group.values():
                    if (isinstance(value, str) and value.startswith('/')
                            and value not in found):
                        found.append(value)
            if found:
                return found
        return []

    @staticmethod
    def versions_of(data):
        """
        Version URLs published by an API index.
        
        An index such as /api/controller/ is only a directory: it returns 200 with JSON
        but serves no resource. Its current_version / available_versions fields point to
        the real base (/api/controller/v2/), hence the value of descending into them
        rather than stopping at the directory.
        """
        if not isinstance(data, dict):
            return []
        found = []
        current = data.get('current_version')
        if isinstance(current, str) and current.startswith('/'):
            found.append(current)
        versions = data.get('available_versions')
        if isinstance(versions, dict):
            for value in versions.values():
                if isinstance(value, str) and value.startswith('/') and value not in found:
                    found.append(value)
        return found

    def api_bases(self, force=False):
        """
        Actually usable bases: keep those that answer with JSON.
        
        A 404 in HTML means the route is not mounted on this vhost (typical for a
        controller hosted elsewhere); a 401 conversely proves the route exists and is
        only waiting for authentication.
        
        Versions published by an index are probed BEFORE the index itself: the first
        retained response is thus the resource base, not its directory. This is
        essential here, where /api/controller/ (200, directory) would hide
        /api/controller/v2/ where everything is actually served.
        """
        if self._api_bases is not None and not force:
            return self._api_bases
        working = []
        # First network failure encountered, kept as-is: what
        # prevents reaching the server is not information to discard; it is
        # the only useful answer when no base could be verified.
        transport_error = None
        candidates = self.published_bases() or list(API_BASE_FALLBACKS)
        for base in candidates:
            root = base if base.endswith('/') else base + '/'
            probes = []
            try:
                status, _, text = self.http(root, auth=False)
            except AapError as exc:
                status, text = None, str(exc)
            if status is None and transport_error is None:
                transport_error = text or 'no HTTP response'
            if status is not None:
                probes = [version for version in self.versions_of(self.parse_body(text))
                          if version.startswith(root)]
            probes += [root.rstrip('/') + '/' + suffix for suffix in BASE_PROBE_SUFFIXES]
            for probe in probes:
                try:
                    status, _, text = self.http(probe, auth=False)
                except AapError as exc:
                    if transport_error is None:
                        transport_error = str(exc)
                    continue
                if status is None:
                    # Neither an HTTP response nor an exception: the server does not respond at
                    # all. Insisting on the other bases would only add
                    # to an already wasted delay and drown out the real cause.
                    if transport_error is None:
                        transport_error = text or 'no HTTP response'
                    break
                if 200 <= status < 300 and isinstance(self.parse_body(text), dict):
                    working.append(probe)
                    break
                if status in (401, 403):
                    working.append(probe)
                    break
        if not working:
            # The network failure cause is expected here (unreachable host, unknown
            # DNS, filtered port): without it, the diagnosis boils down to "the
            # commands may fail with 404", which is true but useless,
            # and points the investigation toward authentication.
            self._transport_error = transport_error
            if transport_error:
                raise AapError(
                    'cannot reach %s: %s' % (self.base, transport_error),
                    detail='tested bases: %s' % ', '.join(candidates),
                    tip=transport_tip(self.base), transport=True)
            # The server responds but none of its bases verifies: a real
            # case (components mounted anywhere); keep the default paths
            # and warn; it is up to the operator to override.
            working = [base.rstrip('/') + '/' for base in candidates]
            warn('no API base verified on %s: using default paths, commands may fail with 404' % self.base)
        self._api_bases = self._dedupe(working)
        return self._api_bases

    @staticmethod
    def _dedupe(bases):
        """
        Keep the most specific base (…/api/v2/ rather than its prefix).
        """
        out = [b for b in bases if not any(o != b and o.startswith(b) for o in bases)]
        return out or list(bases)

    def resource_index(self):
        """
        Map {resource name: URL} published by the discovered API indexes.
        
        This is the most reliable source of information: the server itself says what
        its collections are called and where they are mounted. The gateway and the
        controller do not use the same names (users vs users, but jobs on the controller
        side when we look for job_runs), hence the aliases.
        """
        if self._resource_index is not None:
            return self._resource_index
        index = {}
        roots = []
        for base in self.api_bases():
            roots.append(base)
            roots.append(base.rstrip('/') + '/v1/')
        for root in roots:
            try:
                status, _, text = self.http(root, auth=False)
            except AapError:
                continue
            data = self.parse_body(text)
            if status is None or not isinstance(data, dict):
                continue
            for key, value in data.items():
                if key.startswith('_') or not isinstance(value, str):
                    continue
                if not value.startswith('/') or not value.rstrip('/').endswith(key):
                    continue
                index.setdefault(key, value)
        self._resource_index = index
        return index

    def candidates(self, key):
        """
        All plausible URLs for a logical endpoint, in order.
        
        Three families, from safest to least safe:
          1. the default path, if the installation serves it as-is
          2. what the server publishes for this resource name or its aliases (see
             resource_index): this is what makes /api/controller/v2/jobs/ appear when
             looking for job_runs
          3. the resource name re-anchored on each discovered base, for a component
             mounted somewhere other than its usual path
        """
        path = self._endpoints.get(key)
        if not path:
            raise AapError('unknown logical endpoint %r (see `endpoints`)' % key)
        if path.startswith('http'):
            return [path]
        tries = []

        def add(url):
            if not isinstance(url, str):
                return               # name absent from the published index: normal
            url = re.sub(r'(?<!:)/{2,}', '/', url)
            if url and url not in tries:
                tries.append(url)

        add(path)
        index = self.resource_index()
        for name in [key] + list(ENDPOINT_ALIASES.get(key, [])):
            add(index.get(name))
        segments = [segment for segment in path.strip('/').split('/')
                    if not NON_RESOURCE_SEGMENTS.match(segment)]
        resource = segments[-1] if segments else key
        for base in self.api_bases():
            add(base + resource + '/')
            add(base.rstrip('/') + '/' + path.lstrip('/'))
        for base in API_BASE_FALLBACKS:
            add(base + resource + '/')
        return tries

    def resolve(self, key, probe_params=None):
        """
        Path of the endpoint that is actually served.
        
        Probe anonymously (auth=False): a 401/403 proves the route exists and is only
        waiting for authentication, so it is NOT grounds for rejection; conversely a 404
        in HTML signals a route not mounted on this vhost.
        
        The choice is remembered: requests appear in the server's audit logs, so staying
        quiet is better.
        """
        if key in self._resolved:
            return self._resolved[key]
        direct = self._endpoints.get(key) or ''
        tries = self.candidates(key)
        if direct.startswith('/api/'):
            # already-qualified path: bound the number of probes to stay
            # quiet in audit logs, while keeping the discovered-base
            # variants (a component may be mounted under another base)
            tries = [candidate for candidate in tries if candidate == direct] + \
                    [candidate for candidate in tries if candidate != direct][:3]
        probed = []
        silence = 0                    # probes with no HTTP response at all
        last_transport = None
        forced = self._forced_endpoint(key)
        if forced:
            # Path imposed by the operator (--endpoint or the [endpoints] section
            # of the config): an explicit choice, not a client assumption.
            # Do not mix it with guessed candidates, and above all
            # do not walk back to other paths if it does not respond: a 404 on
            # an override must become a named error, not a success obtained
            # by silently falling back to a path the operator rejected.
            tries = [forced]
        for candidate in tries:
            try:
                status, _, text = self.http(candidate, auth=False,
                                            headers={'Accept': '*/*'})
            except AapError as exc:
                probed.append('%s[%s]' % (rel_url(candidate, self.base), exc))
                silence += 1
                last_transport = str(exc)
                continue
            if status is None:
                silence += 1
                last_transport = text or 'no HTTP response'
            probed.append('%s[%s]' % (rel_url(candidate, self.base), status))
            if self._route_is_served(status, text):
                self._resolved[key] = candidate
                if env_value('AAP_DEBUG', 'RHAAP_DEBUG'):
                    print('#   resolve %s -> %s' % (key, candidate), file=sys.stderr)
                return candidate
        detail = ('probes: %s' % ', '.join(probed)) if probed else 'no candidate URL'
        # No probe obtained an HTTP response: this is not a 404, and saying so
        # is the only useful thing. Distinguishing it from an authentication
        # failure saves the operator from hunting for credentials
        # when no request reached the server.
        if probed and silence == len(probed):
            raise AapError('cannot reach %s (%d probe(s)): %s'
                           % (self.base, len(probed), last_transport or 'probe silent'),
                           detail=detail, tip=transport_tip(self.base), transport=True)
        # Targeted diagnosis: a route that answers 401/403 everywhere is not
        # absent; authentication failed. The right tip has nothing
        # to do with a real 404, and users rarely guess it.
        statuses = [int(match.group(1)) for match in
                    re.finditer(r'\[(\d{3})\]', ' '.join(probed))]
        if statuses and all(code in (401, 403) for code in statuses):
            raise AapError('endpoint %r: the server refuses authentication (HTTP %d)' % (key, statuses[0]),
                           detail='check the profile credentials, or the vhost: %s' % self.base,
                           status=statuses[0])
        raise AapError('endpoint %r not served on %s' % (key, self.base),
                       detail=detail, status=(statuses[0] if statuses else 404))

    @staticmethod
    def _route_is_served(status, text):
        """
        A route is considered served if it is not a 404/405 "Not Found" error page
        generated by Django. A JSON 401/403, a 200, or a redirect count as served.
        """
        if status is None:
            return False
        if status in (404, 405, 410):
            return False
        if status == 406:
            # the URL exists; format negotiation failed
            return True
        if status < 300 or status in (301, 302, 303, 307, 308):
            return True
        # 5xx: the route exists (the backend is reachable); keep it traceable
        data = AAPClient.parse_body(text)
        if status in (500, 502, 503, 504):
            return True
        return isinstance(data, dict)

    def _forced_endpoint(self, key):
        """
        Path imposed by the operator for this key, otherwise None.
        """
        return self._endpoints.get(key) if key in self._forced else None

    def set_endpoint(self, key, value):
        value = (value or '').strip()
        if not value.startswith(('/', 'http')):
            raise AapError('invalid path for endpoints.%s: expected /api/... or http://...'
                           % key)
        self._endpoints[key] = value
        self._forced.add(key)
        for cached in (key, 'csrf'):
            self._resolved.pop(cached, None)

    def get_stdout(self, url):
        """
        Raw output of a job, returning text regardless of the server.
        
        The stdout endpoint is a special case in the API: its representation depends on
        content negotiation, and the client cannot assume. On the AWX controller
        verified here, the same URL returns:
          Accept: text/plain      -> the plain-text log (the only good one)
          Accept: application/json-> {"range":…, "content":…} (log in content)
          Accept: */*             -> a full HTML page
        A third format, txt_audit, only exists on other versions: send it only as a last
        resort, otherwise the log is lost to a 404.
        """
        attempts = [('text/plain', ''), ('application/json', ''),
                    ('text/plain', '?format=txt_audit')]
        errors = []
        for index, (accept, suffix) in enumerate(attempts):
            path = url + suffix
            try:
                _, _, raw = self.request(path, headers={'Accept': accept}, follow=True,
                                         quiet=True)
            except AapError as exc:
                # last resort: report the real error, not the fallback detail
                errors.append('%s%s -> HTTP %s' % (rel_url(path, self.base), accept,
                                                   exc.status))
                if index + 1 < len(attempts):
                    continue
                raise AapError('stdout unavailable for %s' % rel_url(url, self.base),
                               detail='; '.join(errors))
            text = self._stdout_as_text(raw)
            if text is not None:
                if env_value('AAP_DEBUG', 'RHAAP_DEBUG'):
                    print('#   stdout: %s (Accept: %s)'
                          % (rel_url(path, self.base), accept), file=sys.stderr)
                return text
            errors.append('%s -> unexpected HTML response' % rel_url(path, self.base))
        raise AapError('stdout unavailable for %s' % rel_url(url, self.base),
                       detail='; '.join(errors))

    @staticmethod
    def _stdout_as_text(raw):
        """
        Flatten the stdout response: plain text, JSON with a content field, or None.
        """
        data = AAPClient.parse_body(raw)
        if isinstance(data, dict):
            for key in ('content', 'stdout', 'data'):
                if isinstance(data.get(key), str):
                    return data[key]
            return None
        text = raw or ''
        if text.lstrip()[:15].lower().startswith(('<!doctype', '<html')):
            return None
        return text

    def paginated(self, path, limit=None, params=None, max_pages=200):
        """
        Walk pages (offset/limit as well as page/page_size).
        """
        collected = []
        page_params = dict(params or {})
        current = path
        for _ in range(max_pages):
            data = self.get_json(current, params=page_params)
            items = to_list(data)
            collected.extend(items)
            if limit is not None and len(collected) >= limit:
                return collected[:limit]
            nxt = next_url(data) if isinstance(data, dict) else ''
            if not items or not nxt:
                break
            parsed = urllib.parse.urlparse(nxt)
            page_params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            current = nxt if parsed.scheme else parsed.path
        return collected

    def summarize(self):
        return {'host': self.base, 'auth_method': self.auth_method,
                'tls_insecure': self.insecure, 'login': self.creds.login,
                'api_bases': self.api_bases()}


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self.client = None
        self.config = None
        self.profile = None
        self.config_path = CONFIG_PATH
        self.output = 'table'
        self.limit = 25
        self.debug = False


app_state = AppState()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _yaml_scalar(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == '' or re.search(r'[:#\[\]{}&*!|>\'"%@`]|^\s|\s$|\n', text):
        return json.dumps(text, ensure_ascii=False)
    return text


def to_yaml(data, indent=0):
    """
    Minimal YAML converter (stdlib only), for --output yaml.
    """
    pad = '  ' * indent
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append('%s%s:' % (pad, key))
                lines.extend(to_yaml(value, indent + 1))
            else:
                lines.append('%s%s: %s' % (pad, key, _yaml_scalar(value)))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                # usual compact form: the first field stays on the
                # dash line (`- id: 11`) instead of a dash on its own line.
                sub = to_yaml(item, indent + 1)
                lines.append('%s- %s' % (pad, sub[0].strip()))
                lines.extend(sub[1:])
            elif isinstance(item, list) and item:
                lines.append('%s-' % pad)
                lines.extend(to_yaml(item, indent + 1))
            else:
                lines.append('%s- %s' % (pad, _yaml_scalar(item)))
    else:
        lines.append(pad + _yaml_scalar(data))
    return lines


def emit(items, headers, widths, renderer=None):
    """
    Display according to --output: table (human), json/csv/yaml (scriptable).
    """
    mode = app_state.output
    if mode == 'json':
        print(json.dumps(items, indent=2, ensure_ascii=False, default=str))
        return
    if mode == 'yaml':
        print('\n'.join(to_yaml(items)))
        return
    if renderer is None:
        renderer = lambda item: [item.get(h) if isinstance(item, dict) else ''  # noqa: E731
                                 for h in headers]
    rows = [list(renderer(item)) for item in items]
    if mode == 'csv':
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(rows)
        return
    for line in table(rows, headers, widths):
        print(line)
    print('(%d item(s))' % len(rows))


def emit_mapping(data, key_label='key'):
    mode = app_state.output
    if mode == 'json':
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return
    if mode == 'yaml':
        print('\n'.join(to_yaml(data)))
        return
    rows = [[key, value] for key, value in data.items()]
    if mode == 'csv':
        writer = csv.writer(sys.stdout)
        writer.writerow([key_label, 'value'])
        writer.writerows(rows)
        return
    for line in table(rows, [key_label, ''], [20, 'auto']):
        print(line)


def _csv_value(value):
    """
    CSV cell: nested structures are serialized as JSON.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def emit_raw(data):
    """
    Render arbitrary data (`api` response, `--discover`) according to --output.
    
    table and json keep the native JSON representation: it is the readable view of a
    raw API response. yaml serializes the same structure; csv flattens objects to
    `key,value` and lists of objects to one row per object.
    """
    mode = app_state.output
    if mode == 'yaml':
        print('\n'.join(to_yaml(data)))
        return
    if mode == 'csv':
        writer = csv.writer(sys.stdout)
        if isinstance(data, dict):
            writer.writerow(['key', 'value'])
            for key, value in data.items():
                writer.writerow([key, _csv_value(value)])
            return
        if isinstance(data, list) and data and all(isinstance(i, dict) for i in data):
            headers = list(data[0].keys())
            writer.writerow(headers)
            for item in data:
                writer.writerow([_csv_value(item.get(h)) for h in headers])
            return
        writer.writerow(['value'])
        for item in (data if isinstance(data, list) else [data]):
            writer.writerow([_csv_value(item)])
        return
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif data is not None:
        text = str(data)
        sys.stdout.write(text if text.endswith('\n') else text + '\n')


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@command('status', 'Gateway state (status + ping: db/proxy connections, version)')
def cmd_status(args):
    client = app_state.client
    data = {'host': client.base, 'auth': client.auth_method,
            'tls_insecure': client.insecure}
    for key in ('status', 'ping'):
        try:
            payload = client.get_json(client.resolve(key)) or {}
            if isinstance(payload, dict):
                data.update(payload)
            else:
                data[key] = payload
        except AapError as exc:
            data[key + '_error'] = 'HTTP %s' % exc.status
    emit_mapping(data)
    return 0


@command('whoami', 'Current identity (me endpoint)')
def cmd_whoami(args):
    client = app_state.client
    data = unwrap_object(client.get_json(client.resolve('me')) or {})

    # Allow-list: the real /me/ payload contains a
    # `password` (hash) and other internal fields. Printing them as-is,
    # especially with --output json which dumps everything, has no operational
    # value and exposes a secret in a terminal, a script, or a history file.
    safe_fields = ('id', 'username', 'email', 'first_name', 'last_name',
                   'is_superuser', 'is_system_auditor', 'is_active',
                   'is_partner', 'is_platform_auditor', 'last_login',
                   'created', 'modified')
    full = getattr(args, 'full', False)
    if not full:
        shown = {k: data[k] for k in safe_fields if k in data}
        if not shown:                      # unknown profile: do not trim anything
            shown = {k: v for k, v in data.items()
                     if v is None or isinstance(v, (str, int, float, bool))}
        if len(shown) < len(data):
            hidden = [k for k in data if k not in shown]
            print('# %d field(s) masked (hash/internal email/linked field); --full to see everything, but the password field remains a secret'
                  % len(hidden), file=sys.stderr)
    else:
        shown = data

    if app_state.output in ('json', 'yaml', 'csv'):
        emit_mapping(shown, key_label='field')
        return 0
    rows = [[key, value] for key, value in shown.items()
            if value is None or isinstance(value, (str, int, float, bool))]
    for line in table(rows, ['field', 'value'], [22, 'auto']):
        print(line)
    return 0


@command('templates', 'List job templates (--workflow for workflows)')
def cmd_templates(args):
    client = app_state.client
    key = 'workflow_job_templates' if args.workflow else 'job_templates'
    path = client.resolve(key)
    params = {'page_size': min(args.limit, 200)}
    if args.name and args.exact:
        params['name'] = args.name
    items = client.paginated(path, limit=args.limit, params=params)
    if args.name and not args.exact:
        needle = args.name.lower()
        items = [i for i in items if needle in str(i.get('name', '')).lower()]
    if args.only_ids:
        for item in items:
            print(item.get('id'))
        return 0
    if args.fields and app_state.output in ('json', 'yaml'):
        wanted = [f.strip() for f in args.fields.split(',') if f.strip()]
        projected = []
        for item in items:
            row = {}
            for field in wanted:
                value = dig(item, field)
                if value is None:
                    # a missing path (gateway vs controller variants) must
                    # not create a fake null key: omit it, so the output only
                    # reports what was actually retrieved.
                    continue
                # return the same structure as the input (nested keys): a
                # consumer that asks for a.b.c must be able to read a.b.c back.
                cursor = row
                parts = field.split('.')
                for part in parts[:-1]:
                    cursor = cursor.setdefault(part, {})
                    if not isinstance(cursor, dict):
                        break
                else:
                    cursor[parts[-1]] = value
            projected.append(row)
        items = projected

    def render(item):
        org = item.get('organization_name') or dig(item, 'summary_fields.organization.name')
        return [item.get('id'), item.get('name'), org,
                item.get('job_type') or ('survey' if item.get('survey_enabled') else ''),
                item.get('last_job_run') or item.get('modified')]

    emit(items, ['id', 'name', 'organization', 'type', 'last_run'],
         [6, 'auto', 22, 9, 26], render)
    return 0


@command('jobs', 'List the latest job runs (id, template, status, duration)')
def cmd_jobs(args):
    client = app_state.client
    path = client.resolve('job_runs')
    params = {'page_size': min(args.limit, 200), 'order_by': args.order}
    if args.template:
        params['job_template__name'] = args.template
    if args.status:
        params['status'] = args.status
    items = client.paginated(path, limit=args.limit, params=params)
    if args.only_ids:
        for item in items:
            print(item.get('id'))
        return 0

    def render(item):
        return [item.get('id'), item.get('name'), template_name(item),
                item.get('status'), item.get('elapsed'), item.get('finished')]

    emit(items, ['id', 'name', 'template', 'status', 'elapsed', 'finished'],
         [7, 'auto', 24, 11, 8, 25], render)
    return 0


@command('logs', 'Job stdout: --job ID, <ID>, job:<ID> or --template NAME (most recent)')
def cmd_logs(args):
    client = app_state.client
    job_id = args.job
    template = args.template

    for token in list(args.positional):
        digits = re.search(r'\d+', token or '')
        if re.fullmatch(r'(job|job_run|stdout)[:#\s]*\d+', token or '', re.IGNORECASE):
            job_id = int(digits.group(0))
        elif re.fullmatch(r'\d+', token or ''):
            job_id = int(token)
        elif not template:
            template = token

    runs_base = client.resolve('job_runs')
    detail = None

    if not job_id:
        if not template:
            raise AapError('specify a job (--job 42 / logs 42 / job:42) or a template (--template NAME)')
        needle = template.lower()
        found = client.paginated(runs_base, limit=args.limit,
                                 params={'page_size': 100,
                                         'job_template__name': template,
                                         'order_by': args.order})
        # An unknown search filter is ignored by DRF without error: the
        # server then returns every page, and the first item would not be
        # the requested template. So always verify client-side.
        found = [i for i in found if needle in str(template_name(i) or '').lower()]
        if not found:
            # broader fallback: the name may be exact, a substring, or changed
            recent = client.paginated(runs_base, limit=200,
                                      params={'page_size': 200, 'order_by': args.order})
            found = [i for i in recent if needle in str(template_name(i) or '').lower()]
        if not found:
            raise AapError('no job run found for template %r (on %s; loaded runs are named: %s)'
                           % (template, rel_url(runs_base, client.base),
                              ', '.join(sorted({str(template_name(i)) for i in recent[:10]}))
                              if recent else 'no run retrieved'))
        detail = found[0]
        job_id = detail.get('id')
        if not job_id:
            raise AapError('job run object without id', detail=str(detail)[:200])

    stdout_url = args.url
    if stdout_url is None or detail is None:
        try:
            obj = client.get_json(runs_base.rstrip('/') + '/%s/' % job_id)
        except AapError as exc:
            obj = None
            if detail is None:
                print('# (job object %s unreadable: %s)' % (job_id, exc), file=sys.stderr)
        if isinstance(obj, dict):
            detail = obj
            stdout_url = (stdout_url or obj.get('stdout') or obj.get('stdout_url')
                          or dig(obj, 'related.stdout'))

    if isinstance(detail, dict):
        print('# job %s | %s | status=%s | duration=%ss | end=%s'
              % (job_id, template_name(detail), detail.get('status'),
                 detail.get('elapsed'), detail.get('finished')), file=sys.stderr)
    if not stdout_url:
        stdout_url = runs_base.rstrip('/') + '/%s/stdout/' % job_id

    if env_value('AAP_DEBUG', 'RHAAP_DEBUG'):
        print('# stdout <- %s' % rel_url(stdout_url, client.base), file=sys.stderr)
    # get_stdout(), not request(): the endpoint depends on content
    # negotiation, and a default Accept would return the JSON envelope instead of the log.
    raw = client.get_stdout(stdout_url)
    text = clean_stdout(raw, args.clean)

    lines = text.splitlines()
    if args.grep:
        try:
            pattern = re.compile(args.grep, re.IGNORECASE)
        except re.error as exc:
            raise AapError('invalid --grep regular expression: %s' % exc) from None
        lines = [line for line in lines if pattern.search(line)]
    if args.tail is not None:
        lines = lines[-args.tail:] if args.tail else []
    payload = '\n'.join(lines) + ('\n' if lines else '')

    # Filters are applied before rendering, so --tail and --grep also
    # apply to json/yaml/csv (the old json returned the full log).
    if app_state.output == 'json':
        print(json.dumps({'job_id': job_id, 'stdout': payload}, ensure_ascii=False))
        return 0
    if app_state.output == 'yaml':
        print('\n'.join(to_yaml({'job_id': job_id, 'stdout': payload})))
        return 0
    if app_state.output == 'csv':
        writer = csv.writer(sys.stdout)
        writer.writerow(['job_id', 'stdout'])
        writer.writerow([job_id, payload])
        return 0
    sys.stdout.write(payload)
    if not lines:
        print('# (no line to display)', file=sys.stderr)
    return 0


@command('api', 'Arbitrary request: api <path> [-X METH -d json] | api --discover')
def cmd_api(args):
    client = app_state.client
    if args.discover:
        out = client.summarize()
        out['published'] = {}
        for root in ('/api/', '/'):
            try:
                status, _, text = client.http(root)
            except AapError as exc:
                out['published'][root] = {'error': str(exc)}
                continue
            out['published'][root] = {'status': status, 'body': AAPClient.parse_body(text)}
        out['probed'] = {}
        for base in client.api_bases():
            try:
                out['probed'][base] = client.get_json(base)
            except AapError as exc:
                out['probed'][base] = {'error': str(exc), 'status': exc.status}
        emit_raw(out)
        return 0
    if not args.positional:
        raise AapError('path required, e.g. api /api/gateway/v1/users/  (or --discover)')
    path = args.positional[0]
    if not path.startswith(('http://', 'https://', '/')):
        raise AapError('path must start with / (e.g. /api/gateway/v1/)')
    params = {}
    for pair in args.param:
        if '=' not in pair:
            raise AapError('--param expects key=value, got %r' % pair)
        key, value = pair.split('=', 1)
        params[key] = value
    try:
        body = json.loads(args.body) if args.body else None
    except json.JSONDecodeError as exc:
        raise AapError('invalid JSON for -d/--body: %s' % exc) from None
    method = args.method
    if body is not None and method == 'GET':
        method = 'POST'
    _, _, text = client.request(path, method=method, params=params or None, body=body,
                                follow=args.follow)
    emit_raw(AAPClient.parse_body(text))
    return 0


@command('endpoints', 'View/override logical endpoints and discovered bases')
def cmd_endpoints(args):
    client = app_state.client
    changes = {}
    for pair in args.set or []:
        if '=' not in pair:
            raise AapError('--set expects key=/path, got %r' % pair)
        key, value = pair.split('=', 1)
        client.set_endpoint(key.strip(), value.strip())
        changes[key.strip()] = value.strip()
    if args.set:
        if app_state.output == 'table':
            for key, value in changes.items():
                print('endpoints.%s = %s' % (key, value))
            print('(in-memory override only; to make it permanent: the [endpoints] section of the config, otherwise --endpoint on every run)')
        else:
            emit_mapping(changes, key_label='endpoint')
        return 0

    bases = client.api_bases()
    resolved = {k: v for k, v in client._resolved.items() if k != 'csrf'}
    endpoints = {key: {'path': value,
                       'state': 'verified' if key in client._resolved else 'not verified'}
                 for key, value in sorted(client._endpoints.items())}
    data = {'host': client.base, 'auth': client.auth_method,
            'api_bases': bases, 'resolved': resolved, 'endpoints': endpoints}

    mode = app_state.output
    if mode == 'json':
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if mode == 'yaml':
        print('\n'.join(to_yaml(data)))
        return 0
    if mode == 'csv':
        flat = {'host': data['host'], 'auth': data['auth'],
                'api_bases': ', '.join(bases) or '-'}
        for key, value in sorted(resolved.items()):
            flat['resolved.' + key] = value
        for key, item in endpoints.items():
            flat['endpoints.' + key] = item['path']
        emit_mapping(flat, key_label='key')
        return 0

    print('Host          : %s' % client.base)
    print('Auth          : %s' % client.auth_method)
    print('API bases     : %s' % (', '.join(bases) or '-'))
    if resolved:
        print('Resolved      :')
        for line in table([[k, v] for k, v in sorted(resolved.items())],
                          ['  key', 'path'], [24, 'auto']):
            print(line)
    print('Logical endpoints:')
    rows = [[key, item['path'], item['state']] for key, item in endpoints.items()]
    for line in table(rows, ['  key', 'path', 'state'], [24, 'auto', 14]):
        print(line)
    print("""
Override: --endpoint key=/path  or  [endpoints] section of the config file""")
    return 0


@command('users', 'List gateway users')
def cmd_users(args):
    client = app_state.client
    items = client.paginated(client.resolve('users'), limit=args.limit,
                             params={'page_size': min(args.limit, 200)})
    emit(items, ['id', 'username', 'email', 'superuser', 'active', 'last_login'],
         [5, 22, 30, 9, 7, 25],
         lambda i: [i.get('id'), i.get('username'), i.get('email'),
                    i.get('is_superuser'), i.get('is_active'), i.get('last_login')])
    return 0


@command('organizations', 'List gateway organizations')
def cmd_organizations(args):
    client = app_state.client
    items = client.paginated(client.resolve('organizations'), limit=args.limit,
                             params={'page_size': min(args.limit, 200)})
    emit(items, ['id', 'name', 'description'], [5, 26, 'auto'],
         lambda i: [i.get('id'), i.get('name'), i.get('description')])
    return 0


# --- command-specific options ----------------------------------------------

@options('templates')
def opt_templates(sub):
    sub.add_argument('--workflow', action='store_true', help='target workflow_job_templates')
    sub.add_argument('--name', help='filter by name (substring by default)')
    sub.add_argument('--exact', action='store_true', help='exact name (server filter)')
    sub.add_argument('--fields', help='fields to keep in json/yaml, e.g. id,name')
    sub.add_argument('--only-ids', action='store_true',
                     help='only print ids (for chaining with logs)')


@options('jobs')
def opt_jobs(sub):
    sub.add_argument('--template', help='filter by job template name')
    sub.add_argument('--status', help='successful | failed | running | canceled ...')
    sub.add_argument('--order', default='-finished', help='sort order (default: -finished)')
    sub.add_argument('--only-ids', action='store_true', help='only print ids')


@options('whoami')
def opt_whoami(sub):
    sub.add_argument('--full', action='store_true',
                     help='show every /me/ field (by default the password hash and internal fields are masked)')


@options('logs')
def opt_logs(sub):
    sub.add_argument('--job', type=int, help='job run id')
    sub.add_argument('--template', help='most recent job for this template')
    sub.add_argument('--url', help='forced stdout URL (if naming differs)')
    sub.add_argument('--tail', type=non_negative_int,
                     help='last N lines (0 = none, negative rejected)')
    sub.add_argument('--order', default='-finished',
                     help='server-side sort for --template (default: -finished)')
    sub.add_argument('--grep', help='line filtering regex')
    sub.add_argument('--clean', choices=['auto', 'always', 'raw'], default='auto',
                     help='ANSI/CR cleanup (auto: when stdout is not a tty)')


@options('api')
def opt_api(sub):
    sub.add_argument('--discover', action='store_true',
                     help='show API bases and URLs published by the server')
    sub.add_argument('-X', '--method', default='GET', help='HTTP method')
    sub.add_argument('-d', '--body', help='JSON body (automatically switches to POST)')
    sub.add_argument('--param', action='append', default=[], metavar='KEY=VALUE',
                     help='query-string parameter, repeatable')
    sub.add_argument('--follow', action='store_true', help='follow redirects')


@options('endpoints')
def opt_endpoints(sub):
    sub.add_argument('--set', action='append', metavar='KEY=/PATH',
                     help='override an endpoint for this session (repeatable)')


for _name in ('status', 'users', 'organizations'):
    options(_name)(no_options)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def make_parser(spec, prog):
    parser = argparse.ArgumentParser(prog=prog, description=spec['help'])
    parser.add_argument('--limit', type=int, default=None, help='maximum number of items')
    parser.add_argument('--output', choices=['table', 'json', 'csv', 'yaml'], default=None,
                        help='output format (valid for every command)')
    if spec['options']:
        spec['options'](parser)
    parser.add_argument('positional', nargs='*', help=argparse.SUPPRESS)
    return parser


def dispatch(name, argv):
    spec = REGISTRY.get(name)
    if not spec:
        raise AapError('unknown command: %s (available: %s)'
                       % (name, ', '.join(sorted(REGISTRY))))
    args = make_parser(spec, 'aap_api_cli %s' % name).parse_args(argv)
    if args.output:
        app_state.output = args.output
    args.limit = args.limit or app_state.limit
    app_state.limit = args.limit
    return spec['action'](args)


# ---------------------------------------------------------------------------
# Interactive shell (same philosophy as pys3: cmd.Cmd + readline)
# ---------------------------------------------------------------------------

class AapShell(cmd.Cmd):
    intro = ''
    prompt = 'aap> '

    def __init__(self):
        super().__init__()
        client = app_state.client
        self.intro = ("""aap_api_cli %s | profile %s | %s | auth %s
commands: %s   (detailed help: help <cmd>)"""
                      % (VERSION, app_state.profile, client.base, client.auth_method,
                         ', '.join(sorted(REGISTRY))))
        self.history_path = os.path.expanduser('~/.aap_api_cli_history')
        try:
            import readline
            readline.set_completer_delims(' \t\n;')
            readline.set_completer(self._completer)
            readline.parse_and_bind('tab: complete')
            try:
                readline.read_history_file(self.history_path)
            except OSError:
                pass
        except ImportError:
            pass

    def _completer(self, text, state):
        import readline
        words = readline.get_line_buffer().lstrip().split()
        if len(words) <= 1:
            pool = [c + ' ' for c in sorted(REGISTRY) if c.startswith(text)]
            pool += [c for c in ('help', 'history', 'exit') if c.startswith(text)]
        else:
            pool = [c + ' ' for c in _flag_hints(words[0]) if c.startswith(text)]
        return pool[state] if state < len(pool) else None

    def postloop(self):
        try:
            import readline
            readline.write_history_file(self.history_path)
        except Exception:
            pass

    def default(self, line):
        print('unknown command: %s (help for the list)' % (line.split() or [''])[0])

    def emptyline(self):
        return None

    def do_exit(self, arg):
        print('goodbye')
        return True

    do_quit = do_exit
    do_q = do_exit

    def do_EOF(self, arg):
        print()
        return True

    def do_history(self, arg):
        try:
            import readline
        except ImportError:
            print('readline unavailable')
            return None
        for index in range(readline.get_current_history_length()):
            print(readline.get_history_item(index + 1))
        return None


def _flag_hints(command_name):
    """
    Options of a command, for Tab completion.
    """
    spec = REGISTRY.get(command_name)
    if not spec:
        return []
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument('--limit')
    probe.add_argument('--output')
    try:
        if spec['options']:
            spec['options'](probe)
    except Exception:
        return ['--limit', '--output']
    hints = []
    for action in probe._actions:  # noqa: SLF001 (completion introspection)
        hints.extend(action.option_strings)
    return hints


def install_shell_commands():
    """
    Expose each command in the registry as a do_<name> shell method.
    """
    for name, spec in REGISTRY.items():
        setattr(AapShell, 'do_' + name, _shell_runner(name))
        setattr(AapShell, 'help_' + name, _shell_help(spec['help']))


def _shell_help(help_text):
    def show(_ignored=None):
        # cmd.Cmd calls do_help_<cmd>(self): a positional parameter is
        # therefore required here, even if the help has no argument to show.
        print(help_text)
    return show


def _shell_runner(name):
    def runner(self, arg):
        argv = shlex.split(arg) if arg else []
        saved_output, saved_limit = app_state.output, app_state.limit
        try:
            dispatch(name, argv)
        except AapError as exc:
            print_error(exc, file=sys.stdout)
        except SystemExit:
            pass
        except BrokenPipeError:
            pass
        except Exception as exc:
            print_error(AapError('%s: %s' % (type(exc).__name__, exc)), file=sys.stdout)
        finally:
            app_state.output, app_state.limit = saved_output, saved_limit
        return None
    return runner


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """# aap_api_cli -- INI configuration file
# Parsed WITHOUT interpolation: %% and ${} remain literal (the classic password trap).
#
# One profile = one platform.  chmod 600 %(path)s
#
# Password sources, highest priority first:
#   `pass` key in the profile  |  --ask-password  |  --password-stdin
#   --password-env AAP_PASSWORD  |  --password-cmd 'pass show aap/admin'

[default]
host = aap.apps.example.internal
user = admin
pass = CHANGEME
tls_insecure = true
auth_method = basic
timeout = 30
# ca_bundle = ~/.internal-ca.pem

# --- another platform / controller hosted on a separate host ----------------
# [controller]
# host = aap-controller.apps.example.internal
# user = admin
# pass = CHANGEME
# tls_insecure = true

# --- global override of logical endpoints -----------------------------------
# Autodetection finds the bases published by the server on its own. Force here
# only what it cannot guess (a component not mounted on this vhost).
# [endpoints]
# job_templates = /api/controller/api/v2/job_templates/
# job_runs = /api/controller/api/v2/job_runs/
"""


def print_config_template(path):
    print(CONFIG_TEMPLATE % {'path': path})
    print('# After editing, check that the endpoints match the installation:')
    print('#   aap_api_cli.py endpoints        then  aap_api_cli.py api --discover')


def load_config(path):
    parser = configparser.RawConfigParser(interpolation=None)
    if not os.path.exists(path):
        raise AapError("""no config file: %s
  -> aap_api_cli.py --generate-config > %s && chmod 600 %s"""
                       % (path, path, path))
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError) as exc:
        raise AapError('unreadable config file (%s): %s' % (path, exc))
    try:
        if os.stat(path).st_mode & 0o077:
            warn('%s is accessible to other users (chmod 600 recommended)' % path)
    except OSError:
        pass
    return parser


def read_passe(section, args, label=''):
    """
    Resolve the profile password, without ever displaying it.
    """
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise AapError('--password-stdin: nothing on stdin')
        return line.rstrip('\r\n')
    if args.password_env:
        if args.password_env not in os.environ:
            raise AapError('environment variable %s is not set' % args.password_env)
        return os.environ[args.password_env]
    if args.password_cmd:
        import subprocess
        try:
            proc = subprocess.run(args.password_cmd, shell=True, capture_output=True,
                                  timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AapError('--password-cmd failed (%s): %s' % (args.password_cmd, exc))
        if proc.returncode != 0:
            raise AapError('--password-cmd failed (exit %d): %s'
                           % (proc.returncode,
                              proc.stderr.decode('utf-8', 'replace')[:200]))
        value = proc.stdout.decode('utf-8', 'replace').rstrip('\r\n')
        if not value:
            raise AapError('--password-cmd wrote nothing to stdout')
        return value
    value = (section.get('pass') or section.get('password') or '').strip()
    if args.ask_password or value in ('', 'CHANGEME'):
        if not sys.stdin.isatty():
            raise AapError("""no password available and stdin is not a terminal:
  -> --password-env AAP_PASSWORD, --password-stdin or --password-cmd""")
        return getpass.getpass('password (%s): ' % (label or 'profile'))
    return value


def build_creds(section, args):
    """
    Profile + CLI overrides -> Credentials. The password is only read when needed.
    """
    login = (args.user or section.get('user') or section.get('username')
             or section.get('login') or '').strip()
    jeton = (args.jeton or section.get('token') or '').strip()
    if jeton == 'CHANGEME':
        # Same convention as for the password: an unfilled template is not
        # a token, and must produce the "missing token" error, not a 401.
        jeton = ''
    method = args.auth or section.get('auth_method') or ('token' if jeton else 'basic')
    passe = '' if method == 'token' else read_passe(section, args,
                                                    label='%s@%s' % (login or '?',
                                                                      args.host or section.get('host') or '?'))
    return Credentials(login, passe, jeton, method)


def build_client(config, profile, args):
    if profile not in config:
        raise AapError('profile %r not in %s (profiles: %s)'
                       % (profile, app_state.config_path,
                          ', '.join(config.sections()) or 'none'))
    section = config[profile]
    host = args.host or section.get('host')
    if not host:
        raise AapError('`host` key missing in profile [%s]' % profile)
    args.jeton = args.jeton or None
    creds = build_creds(section, args)
    insecure = True if args.insecure else yesno(section.get('tls_insecure'))
    ca_bundle = args.ca_bundle or section.get('ca_bundle') or section.get('ca_file')
    raw_timeout = args.timeout or section.get('timeout') or 30
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        raise AapError('invalid timeout in profile [%s]: %r (integer expected)'
                       % (profile, raw_timeout)) from None
    client = AAPClient(host, creds, insecure=insecure, ca_bundle=ca_bundle,
                       timeout=timeout)

    if config.has_section('endpoints'):
        for key, value in config['endpoints'].items():
            if value.strip():
                try:
                    client.set_endpoint(key, value)
                except AapError as exc:
                    warn('endpoints.%s ignored: %s' % (key, exc))
    for override in (args.endpoint or []):
        if '=' not in override:
            raise AapError('--endpoint expects key=/path, got %r' % override)
        key, value = override.split('=', 1)
        client.set_endpoint(key.strip(), value.strip())
    return client


def build_args(argv):
    parser = argparse.ArgumentParser(
        prog='aap_api_cli.py',
        description='CLI for the Red Hat Ansible Automation Platform API (templates, job logs, arbitrary API).',
        epilog="""Examples:
  aap_api_cli.py --generate-config > ~/.aap_api_cli.conf
  aap_api_cli.py --list-profiles
  aap_api_cli.py endpoints
  aap_api_cli.py templates --name patch
  aap_api_cli.py jobs --limit 10 --output json | jq .[0]
  aap_api_cli.py logs --job 42 --tail 50
  aap_api_cli.py logs 42 --grep failed
  aap_api_cli.py api /api/gateway/v1/users/
  aap_api_cli.py            # interactive shell
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-f', '--config-file', default=CONFIG_PATH,
                        help='config file path (default: %(default)s)')
    parser.add_argument('-P', '--profile',
                        help='profile (otherwise: the only one, [default], or $AAP_PROFILE)')
    parser.add_argument('--host', help='override the host')
    parser.add_argument('--user', help='override the login')
    parser.add_argument('--password-env', metavar='VAR', help='password taken from $VAR')
    parser.add_argument('--password-cmd', metavar='CMD',
                        help='password taken from a command stdout (e.g. pass show aap/admin)')
    parser.add_argument('--password-stdin', action='store_true', help='password read from stdin')
    parser.add_argument('--ask-password', action='store_true', help='prompt for the password')
    parser.add_argument('--auth', choices=['basic', 'session', 'token'],
                        help='auth method (default: profile, otherwise basic)')
    parser.add_argument('--token', dest='jeton', metavar='TOKEN',
                        help='Bearer token for --auth token')
    parser.add_argument('--jeton', dest='jeton', help=argparse.SUPPRESS)  # legacy alias
    parser.add_argument('--insecure', action='store_true',
                        help='disable TLS certificate verification')
    parser.add_argument('--ca-bundle', help='CA file (PEM) used to verify the certificate')
    parser.add_argument('--timeout', type=int, help='HTTP timeout in seconds (default 30)')
    parser.add_argument('--endpoint', action='append', metavar='KEY=/PATH',
                        help='override a logical endpoint (repeatable)')
    parser.add_argument('--list-profiles', action='store_true',
                        help='list profiles in the config and exit')
    parser.add_argument('--generate-config', action='store_true',
                        help='print a config file template')
    parser.add_argument('--no-login', action='store_true',
                        help='skip the auth check before the command (saves a round trip)')
    parser.add_argument('--output', choices=['table', 'json', 'csv', 'yaml'], default='table',
                        help='universal output format (default: table)')
    parser.add_argument('-v', '--verbose', action='store_true', help='trace HTTP requests')
    parser.add_argument('--version', action='version', version='aap_api_cli ' + VERSION)
    parser.add_argument('command', nargs='?', help='command (otherwise: interactive shell)')
    return parser.parse_known_args(argv)


def resolve_profile(config, requested):
    sections = [s for s in config.sections() if s != 'endpoints']
    if requested:
        return requested
    profile = env_value('AAP_PROFILE', 'RHAAP_PROFILE')
    if profile:
        return profile
    if len(sections) == 1:
        print('[*] using the only profile: %s' % sections[0], file=sys.stderr)
        return sections[0]
    if 'default' in sections:
        return 'default'
    return None


def _run(argv=None):
    global app_state
    args, extra = build_args(list(sys.argv[1:] if argv is None else argv))
    app_state = AppState()
    app_state.config_path = os.path.expanduser(args.config_file)
    app_state.output = args.output
    if args.verbose:
        os.environ['AAP_DEBUG'] = '1'
        app_state.debug = True

    if args.generate_config:
        print_config_template(app_state.config_path)
        return 0

    config = load_config(app_state.config_path)
    app_state.config = config

    if args.list_profiles:
        sections = [s for s in config.sections() if s != 'endpoints']
        print('profiles in %s:' % app_state.config_path)
        for name in sections:
            print('  - %-14s %s' % (name, config[name].get('host', '(host not defined)')))
        if not sections:
            print('  (none)')
        return 0

    profile = resolve_profile(config, args.profile)
    if not profile:
        raise AapError('no default profile; choose one with -P from: %s'
                       % ', '.join(s for s in config.sections() if s != 'endpoints'))
    app_state.profile = profile
    app_state.client = build_client(config, profile, args)
    client = app_state.client

    name = args.command

    # Auth also precedes the interactive shell: without it, the first command
    # in the shell would fail with 401 without the problem being named.
    if not args.no_login:
        ok, message = client.login()
        if not ok:
            # The AapError object is remembered by login() when there is one: a
            # network failure must definitely not inherit the tip about
            # credentials; it is the most costly diagnostic trap of the lot.
            failure = getattr(client, '_login_failure', None)
            transport = bool(getattr(failure, 'transport', False))
            if transport:
                # One concise error and one host-specific tip: the probe list
                # (error detail) is only shown when AAP_DEBUG is set.
                print_error(failure if failure is not None else
                            AapError(message, tip=transport_tip(client.base)))
                return 1
            print('[ERROR] authentication (%s) failed: %s'
                  % (client.auth_method, message), file=sys.stderr)
            detail = getattr(failure, 'detail', None)
            if detail:
                print('  %s' % str(detail)[:600].replace('\n', ' '), file=sys.stderr)
            if client.auth_method == 'basic':
                print('[TIP] the gateway rejects Basic: try --auth session, check the profile, or create a token (auth_method = token).', file=sys.stderr)
            else:
                tip = _error_tip(failure) if failure is not None else None
                if tip:
                    print(tip, file=sys.stderr)
            return 1
        if app_state.debug or name in ('status', 'whoami'):
            print('[*] %s' % message, file=sys.stderr)

    if not name:
        install_shell_commands()
        try:
            AapShell().cmdloop()
        except KeyboardInterrupt:
            print('^C')
        return 0

    return dispatch(name, extra) or 0


def main(argv=None):
    """
    CLI entry point.
    
    Numeric options are validated by argparse (SystemExit 2, no traceback). This net
    covers the rest of the user inputs that would otherwise surface as a raw
    traceback: --grep regex, -d JSON body, and integers read from the config. The
    relevant sites already raise a contextual AapError; this guarantees that no
    forgotten path exposes a call stack to the operator.
    """
    try:
        return _run(argv)
    except re.error as exc:
        print_error(AapError('invalid regular expression: %s' % exc))
        return 1
    except json.JSONDecodeError as exc:
        print_error(AapError('invalid JSON: %s' % exc))
        return 1
    except ValueError as exc:
        print_error(AapError('invalid numeric value: %s' % exc))
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except AapError as exc:
        print_error(exc)
        sys.exit(1)
    except KeyboardInterrupt:
        print("""
interrupted""")
        sys.exit(130)
    except BrokenPipeError:
        os._exit(0)


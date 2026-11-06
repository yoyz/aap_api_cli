#!/usr/bin/env python3
"""
Jeu de tests pour aap_api_cli.py contre tests/mock_aap.py.

  python3 tests/test_aap_api_cli.py           # demarre le mock, teste, puis l'arrete
  python3 tests/test_aap_api_cli.py -v        # montre la sortie des cas en echec
  python3 tests/test_aap_api_cli.py --keep    # conserve la configuration et le log temporaires

La suite est autonome : elle choisit un port libre, lance tests/mock_aap.py,
ecrit son propre fichier de configuration dans un repertoire temporaire (chmod
600) et nettoie tout en sortie. Aucun mock ni fichier tests/mock.conf n'est requis.

MOCK_PORT permet de forcer le port (utile en cas de pare-feu local) ; le mock
reste lance par la suite. HOME est redirige vers le repertoire temporaire pour ne
pas toucher l'historique du shell de l'utilisateur.

Les verifications sont exprimees par de petites fonctions nommees (want_*) :
les exceptions Python remplacent les assertions muettes, et un echec affiche la
sortie reelle du CLI.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = [sys.executable, os.path.join(ROOT, 'aap_api_cli.py')]
MOCK = os.path.join(HERE, 'mock_aap.py')

# Renseignes par start_mock() avant le premier test.
CONF = None
LOCAL = []
_PORT = None
_TMPDIR = None
_MOCK = None
_MOCK_LOG = None
_MOCK_LOG_HANDLE = None

RESULTS = []
VERBOSE = '-v' in sys.argv or '--verbose' in sys.argv
KEEP = '--keep' in sys.argv


# ---------------------------------------------------------------------------
# environnement jetable (mock + config)
# ---------------------------------------------------------------------------

def free_port():
    """Port TCP libre sur la loopback (le mock ne doit pas entrer en conflit)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def mock_log_tail(lines=40):
    """Dernieres lignes du log du mock, pour un echec de demarrage."""
    try:
        with open(_MOCK_LOG, 'r', encoding='utf-8', errors='replace') as handle:
            return ''.join(handle.readlines()[-lines:])
    except (OSError, TypeError):
        return ''


def write_config(path):
    """Config minimale : trois profils, tous pointes sur le mock du jour."""
    content = """\
# Genere par tests/test_aap_api_cli.py -- ne pas editer.

[local]
host = http://127.0.0.1:%(port)d
user = testuser
pass = testpass
auth_method = basic

[alias_keys]
host = http://127.0.0.1:%(port)d
username = testuser
password = testpass

[session_profile]
host = http://127.0.0.1:%(port)d
user = testuser
pass = testpass
auth_method = session

[bad_timeout]
host = http://127.0.0.1:%(port)d
user = testuser
pass = testpass
timeout = pas_un_entier

[missing_token]
host = http://127.0.0.1:%(port)d
user = testuser
auth_method = token

[placeholder_token]
host = http://127.0.0.1:%(port)d
user = testuser
auth_method = token
token = CHANGEME
""" % {'port': _PORT}
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def stop_mock():
    """Arrete le mock et supprime le repertoire temporaire (sauf --keep)."""
    global _MOCK, _MOCK_LOG_HANDLE, _TMPDIR
    if _MOCK is not None and _MOCK.poll() is None:
        _MOCK.terminate()
        try:
            _MOCK.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _MOCK.kill()
            _MOCK.wait()
    if _MOCK_LOG_HANDLE is not None:
        _MOCK_LOG_HANDLE.close()
        _MOCK_LOG_HANDLE = None
    _MOCK = None
    if _TMPDIR:
        if KEEP:
            print('[*] fichiers de test conserves dans %s' % _TMPDIR)
        else:
            shutil.rmtree(_TMPDIR, ignore_errors=True)
        _TMPDIR = None


def start_mock():
    """Lance le mock et ecrit la config. Leve RuntimeError si le port ne repond pas."""
    global _PORT, _TMPDIR, _MOCK, _MOCK_LOG, _MOCK_LOG_HANDLE, CONF, LOCAL
    if not os.path.exists(MOCK):
        raise RuntimeError('mock introuvable: %s' % MOCK)
    _TMPDIR = tempfile.mkdtemp(prefix='aap-tests-')
    CONF = os.path.join(_TMPDIR, 'mock.conf')
    _MOCK_LOG = os.path.join(_TMPDIR, 'mock.log')
    _PORT = int(os.environ['MOCK_PORT']) if os.environ.get('MOCK_PORT') else free_port()

    _MOCK_LOG_HANDLE = open(_MOCK_LOG, 'w', encoding='utf-8')
    _MOCK = subprocess.Popen([sys.executable, MOCK, str(_PORT)],
                             stdout=_MOCK_LOG_HANDLE, stderr=subprocess.STDOUT)

    ready = False
    deadline = time.time() + 10
    ping = 'http://127.0.0.1:%d/api/gateway/v1/ping/' % _PORT
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        if _MOCK.poll() is not None:
            break
        try:
            with opener.open(ping, timeout=0.5) as response:
                if response.getcode() == 200:
                    ready = True
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.05)

    if not ready:
        log = mock_log_tail()
        stop_mock()
        raise RuntimeError('le mock n\'a pas demarre sur le port %d\n%s'
                           % (_PORT, log or '(aucun log)'))

    write_config(CONF)
    # Repertoire HOME ne contenant que l'ancien nom de config : il doit etre
    # ignore (pas de repli), ce que verifie suite_config().
    legacy_home = os.path.join(_TMPDIR, 'legacy-home')
    os.makedirs(legacy_home)
    with open(os.path.join(legacy_home, '.rhaap_api_cli.conf'), 'w',
              encoding='utf-8') as handle:
        handle.write('[old]\nhost = old.example\n')
    LOCAL = ['-f', CONF, '-P', 'local']


# ---------------------------------------------------------------------------
# harnais
# ---------------------------------------------------------------------------

class CheckFailed(Exception):
    pass


def fail(message):
    raise CheckFailed(message)


def run(argv, stdin=None, env=None):
    environ = dict(os.environ)
    for name in ('AAP_DEBUG', 'RHAAP_DEBUG', 'AAP_PROFILE', 'RHAAP_PROFILE',
                 'AAP_PASSE'):
        environ.pop(name, None)
    if _TMPDIR:
        # Le shell interactif ecrit ~/.aap_api_cli_history : on l'isole.
        environ['HOME'] = _TMPDIR
    # Ne pas laisser un proxy d'entreprise intercepter la loopback.
    environ['NO_PROXY'] = '127.0.0.1,localhost'
    environ['no_proxy'] = '127.0.0.1,localhost'
    environ.update(env or {})
    try:
        proc = subprocess.run(CLI + argv, input=stdin, capture_output=True,
                              text=True, timeout=60, env=environ)
    except subprocess.TimeoutExpired:
        fail('timeout de 60s sur: %s' % ' '.join(argv))
    return proc.returncode, proc.stdout, proc.stderr


def check(name, argv, verify=None, stdin=None, env=None, expect=0):
    code, out, err = run(argv, stdin=stdin, env=env)
    problem = None
    if code != expect:
        problem = 'exit=%d attendu=%d' % (code, expect)
    elif verify:
        try:
            verify(out, err)
        except CheckFailed as exc:
            problem = str(exc)
        except Exception as exc:                       # erreur du test lui-meme
            problem = 'erreur du test: %s: %s' % (type(exc).__name__, exc)
    if problem:
        RESULTS.append((False, name, problem, out, err))
        print('FAIL  %s\n      %s' % (name, problem.replace('\n', '\n      ')))
    else:
        RESULTS.append((True, name, '', '', ''))
        print('ok    %s' % name)
    return out, err


# ---------------------------------------------------------------------------
# verifications reutilisables
# ---------------------------------------------------------------------------

def want_json(body):
    try:
        return json.loads(body)
    except ValueError:
        fail('sortie JSON invalide: %s' % body[:200])


def want_out(*needles):
    def verify(out, _err):
        for needle in needles:
            if needle not in out:
                fail('%r absent de stdout' % needle)
    return verify


def want_err(*needles):
    def verify(_out, err):
        for needle in needles:
            if needle not in err:
                fail('%r absent de stderr' % needle)
    return verify


def want_clean_error(*needles):
    """Message d'erreur attendu, et surtout aucune pile d'appels Python."""
    def verify(_out, err):
        for needle in needles:
            if needle not in err:
                fail('%r absent de stderr' % needle)
        if 'Traceback' in err:
            fail('traceback Python expose sur stderr')
    return verify


def want_lines(pattern, count, on='out'):
    def verify(out, err):
        found = re.findall(pattern, out if on == 'out' else err)
        if len(found) != count:
            fail('%d lignes /%s/ attendues, %d trouvees' % (count, pattern, len(found)))
    return verify


def want_no(*forbidden):
    def verify(out, err):
        for needle in forbidden:
            if needle in out or needle in err:
                fail(' %r ne doit pas apparaitre' % needle)
    return verify


def want_order(*needles):
    """Les `needles` doivent apparaitre dans cet ordre dans stdout."""
    def verify(out, _err):
        positions = []
        for needle in needles:
            index = out.find(needle)
            if index < 0:
                fail('%r absent de stdout' % needle)
            positions.append(index)
        if positions != sorted(positions):
            fail('ordre incorrect: %r' % (needles,))
    return verify


def want_line(label, needle, present=True):
    """La derniere ligne contenant `label` doit (ou non) contenir `needle`.

    Indispensable pour `endpoints` : la sortie liste aussi les endpoints logiques
    par defaut, donc un simple want_no() sur toute la sortie ne dit rien de la
    base reellement decouverte.
    """
    def verify(out, _err):
        lines = [line for line in out.splitlines() if label in line]
        if not lines:
            fail('aucune ligne contenant %r' % label)
        found = any(needle in line for line in lines)
        if found != present:
            fail('%r %s dans %r : %s'
                 % (needle, 'attendu' if present else 'interdit', label, lines[-1]))
    return verify


# ---------------------------------------------------------------------------
# suites
# ---------------------------------------------------------------------------

def suite_config():
    print('\n--- configuration ---')
    check('--list-profiles', LOCAL[:2] + ['--list-profiles'],
          want_out('local', 'alias_keys', 'session_profile'))
    check('AAP_PROFILE selectionne le profil',
          ['-f', CONF, 'whoami'], env={'AAP_PROFILE': 'local'},
          verify=want_out('testuser'))
    check('RHAAP_PROFILE reste accepte (alias)',
          ['-f', CONF, 'whoami'], env={'RHAAP_PROFILE': 'local'},
          verify=want_out('testuser'))
    check('--generate-config complete', ['--generate-config'],
          want_out('[default]', 'tls_insecure', 'auth_method', 'host ='))
    check('--generate-config: priorite des mots de passe',
          ['--generate-config'],
          want_order('--password-stdin', '--password-env', '--password-cmd',
                     '`pass` key'))
    check('--generate-config: endpoints controleur valides',
          ['--generate-config'],
          want_out('/api/controller/v2/job_templates/',
                   '/api/controller/v2/jobs/'))
    check('--generate-config: plus de chemin fantome',
          ['--generate-config'], want_no('/api/controller/api/v2/'))
    check('--version', ['--version'])
    check('--help sortie 0', ['--help'])
    check('config absente -> conseil --generate-config',
          ['-f', CONF + '.absent', 'users'],
          want_err('--generate-config'), expect=1)
    check('ancienne config ignoree (pas de repli)',
          ['--list-profiles'],
          env={'HOME': os.path.join(_TMPDIR, 'legacy-home')},
          expect=1, verify=want_clean_error('aap_api_cli.conf'))
    check('profil absent -> liste des profils existants',
          LOCAL[:2] + ['-P', 'inexistant', 'status'],
          want_err('local'), expect=1)
    check('endpoint illimal forme -> erreur explicite',
          LOCAL + ['--endpoint', 'users=api/sans/slash', 'users'],
          want_err('invalid path'), expect=1)


def suite_auth():
    print('\n--- authentification ---')
    check('basic depuis la config -> whoami', LOCAL + ['whoami'],
          want_out('testuser'))
    check('whoami --output json', LOCAL + ['whoami', '--output', 'json'],
          lambda o, e: None if want_json(o).get('username') == 'testuser' else fail('username'))
    check('whoami --output csv', LOCAL + ['whoami', '--output', 'csv'],
          want_out('field,value', 'testuser'))
    check('whoami masque le hash par defaut',
          LOCAL + ['whoami', '--output', 'json'],
          lambda o, e: None if 'pbkdf2' not in o and 'masked' in e
          else fail('hash visible ou avertissement absent'))
    check('whoami --full expose le hash',
          LOCAL + ['whoami', '--full', '--output', 'json'],
          want_out('pbkdf2'))
    check('whoami: avertissement coherent avec --full',
          LOCAL + ['whoami'], want_err('exposes the password hash'))
    check('whoami: message trompeur retire',
          LOCAL + ['whoami'], want_no('remains a secret'))
    check('alias de cles (login/password)',
          ['-f', CONF, '-P', 'alias_keys', 'whoami'], want_out('testuser'))
    check('auth session: csrf + referer + cookie',
          ['-f', CONF, '-P', 'local', '--auth', 'session', 'whoami'],
          want_out('testuser'))
    check('auth session depuis le profil',
          ['-f', CONF, '-P', 'session_profile', 'whoami'], want_out('testuser'))
    check('auth token (Bearer)',
          LOCAL + ['--auth', 'token', '--token', 'mock-token', 'whoami'],
          want_out('tokenuser'))
    check('legacy --jeton alias still works',
          LOCAL + ['--auth', 'token', '--jeton', 'mock-token', 'whoami'],
          want_out('tokenuser'))
    check('status agrege status+ping', LOCAL + ['status'],
          want_out('application_version', '2.5.0'))
    check('status --output csv: structures en JSON',
          LOCAL + ['status', '--output', 'csv'],
          lambda o, e: None if '""database""' in o and "'database'" not in o
          else fail('structure non JSON: %r' % o[:200]))
    check('passe invalide -> 401 et conseil',
          LOCAL + ['--user', 'testuser', '--password-stdin', 'whoami'],
          stdin='mauvais-passe\n', expect=1,
          verify=want_err('credentials', 'TIP'))
    check('login 403 -> TIP role, pas le conseil Basic generique',
          LOCAL + ['--endpoint', 'me=/api/gateway/v1/forbidden/',
                   '--endpoint', 'users=/api/gateway/v1/forbidden/', 'whoami'],
          expect=1,
          verify=lambda o, e: None
          if 'role_user_assignments' in e and 'rejects Basic' not in e
          else fail('403 tip incorrect: %r' % e[:300]))
    check('token invalide -> message', LOCAL + ['--auth', 'token', '--token', 'mauvais', 'whoami'],
          expect=1, verify=want_err('token'))
    check('--auth token sans jeton -> erreur explicite',
          LOCAL + ['--auth', 'token', 'whoami'], expect=1,
          verify=want_clean_error('token'))
    check('profil token sans jeton -> erreur explicite',
          ['-f', CONF, '-P', 'missing_token', 'whoami'], expect=1,
          verify=want_clean_error('token'))
    check('token = CHANGEME -> traite comme absent',
          ['-f', CONF, '-P', 'placeholder_token', 'whoami'], expect=1,
          verify=want_clean_error('token'))
    check('--no-login economise la verification',
          LOCAL + ['--no-login', 'whoami'], want_out('testuser'))
    check('--password-env', LOCAL + ['--password-env', 'AAP_PASSE', 'whoami'],
          env={'AAP_PASSE': 'testpass'}, verify=want_out('testuser'))
    check('--password-env inexistant -> erreur claire',
          LOCAL + ['--password-env', 'AAP_VIDE_XYZ', 'whoami'], expect=1)
    check('--password-cmd', LOCAL + ['--password-cmd', 'printf testpass', 'whoami'],
          verify=want_out('testuser'))
    check('--password-cmd en echec -> erreur',
          LOCAL + ['--password-cmd', 'exit 3', 'whoami'], expect=1)
    check('le passe n\'est jamais echo (endpoints)', LOCAL + ['endpoints'],
          want_no('testpass'))
    check('le passe n\'est jamais echo (erreur)',
          LOCAL + ['--endpoint', 'me=/api/gateway/v1/nope/', 'whoami'],
          want_no('testpass'), expect=1)


def suite_templates():
    print('\n--- templates ---')
    check('tableau + organisation inline', LOCAL + ['templates'],
          want_out('patch-linux', 'deploy-app', 'backup-etl', 'Ops', 'Devs',
                   '(3 item(s))'))
    check('organisation sans nom ne casse pas', LOCAL + ['templates'],
          want_out('backup-etl'))
    check('--output json', LOCAL + ['templates', '--output', 'json'],
          lambda o, e: None if len(want_json(o)) == 3 else fail('3 templates'))
    check('--output csv avec en-tete', LOCAL + ['templates', '--output', 'csv'],
          want_out('id,name,organization,type,last_run'))
    check('--output yaml', LOCAL + ['templates', '--output', 'yaml'],
          want_out('- id:'))
    check('--only-ids brut', LOCAL + ['templates', '--only-ids'],
          lambda o, e: None if o.split() == ['11', '12', '13'] else fail('ids'))
    check('--name --exact (filtre serveur)',
          LOCAL + ['templates', '--name', 'deploy-app', '--exact', '--only-ids'],
          lambda o, e: None if o.split() == ['12'] else fail('attendu 12, got %s' % o))
    check('--name substring (filtre client)',
          LOCAL + ['templates', '--name', 'patch', '--only-ids'],
          lambda o, e: None if o.split() == ['11'] else fail('attendu 11'))
    check('--name substring au-dela de --limit',
          LOCAL + ['templates', '--limit', '2', '--name', 'backup', '--only-ids'],
          lambda o, e: None if o.split() == ['13'] else fail('attendu 13, got %r' % o))
    check('--fields avec chemin imbrique',
          LOCAL + ['templates', '--output', 'json',
                   '--fields', 'id,name,summary_fields.organization.name'],
          lambda o, e: None if want_json(o)[0]['summary_fields']['organization']['name'] == 'Ops'
          else fail('champ imbrique'))
    check('--fields en table -> avertissement',
          LOCAL + ['templates', '--fields', 'id,name'],
          want_err('--fields', 'ignored'))
    check('--fields en json -> pas d avertissement',
          LOCAL + ['templates', '--output', 'json', '--fields', 'id,name'],
          want_no('is ignored'))
    check('--workflow (endpoint different)', LOCAL + ['templates', '--workflow'],
          want_out('nightly-full'))
    check('--limit respecte', LOCAL + ['templates', '--limit', '2', '--only-ids'],
          lambda o, e: None if len(o.split()) == 2 else fail('2 ids'))


def suite_pagination():
    print('\n--- pagination ---')
    check('depasse le plafond serveur page_size=20',
          LOCAL + ['users', '--limit', '45'],
          want_out('user45', '(45 item(s))'))
    check('exactement 45 lignes', LOCAL + ['users', '--limit', '45'],
          want_lines(r'\buser\d\d\b', 45))
    check('pagination gateway next_url (job_runs)',
          LOCAL + ['jobs', '--limit', '3'], want_out('(3 item(s))'))
    check('jobs --template exact',
          LOCAL + ['jobs', '--template', 'deploy-app', '--only-ids'],
          lambda o, e: None if o.split() == ['102'] else fail('102 attendu, got %r' % o))
    check('jobs --template substring (fallback client)',
          LOCAL + ['jobs', '--template', 'patch', '--only-ids'],
          lambda o, e: None if o.split() == ['101'] else fail('101 attendu, got %r' % o))
    check('--limit borne la sortie', LOCAL + ['users', '--limit', '5'],
          want_out('(5 item(s))'))
    check('--limit 1', LOCAL + ['users', '--limit', '1'], want_out('(1 item(s))'))
    check('--limit avant la commande', LOCAL + ['--limit', '2', 'templates', '--only-ids'],
          lambda o, e: None if len(o.split()) == 2 else fail('2 ids attendus'))
    check('--limit negatif refuse', LOCAL + ['templates', '--limit', '-1'],
          expect=2, verify=want_clean_error('--limit'))
    check('--limit 0 -> aucun element',
          LOCAL + ['templates', '--limit', '0', '--only-ids'],
          lambda o, e: None if o.strip() == '' else fail('stdout non vide: %r' % o[:80]))
    check('tri par defaut preserve', LOCAL + ['users', '--limit', '3', '--output', 'json'],
          lambda o, e: None if want_json(o)[0]['id'] == 1 else fail('ordre'))


def suite_logs():
    print('\n--- logs de jobs ---')
    check('--job 101 nettoie ANSI et CR', LOCAL + ['logs', '--job', '101'],
          want_out('PLAY RECAP', 'Progress: 100%'))
    check('aucun code ANSI en sortie nettoyee', LOCAL + ['logs', '--job', '101'],
          want_no('\x1b[', '\x07'))
    check('resume du job sur stderr', LOCAL + ['logs', '--job', '101'],
          want_err('job 101', 'successful', '12.5'))
    check('positionnel nu (logs 102)', LOCAL + ['logs', '102'],
          want_err('job 102', 'failed'))
    check('forme job:101', LOCAL + ['logs', 'job:101'], want_out('PLAY'))
    check('--grep', LOCAL + ['logs', '--job', '101', '--grep', 'failed'],
          want_out('boom'))
    check('--grep exclut le reste', LOCAL + ['logs', '--job', '101', '--grep', 'failed'],
          want_no('Gathering'))
    check('--tail 2', LOCAL + ['logs', '--job', '101', '--tail', '2'],
          want_lines('\n', 2))
    check('--tail 0 -> aucune ligne',
          LOCAL + ['logs', '--job', '101', '--tail', '0'],
          lambda o, e: None if o == '' else fail('stdout non vide: %r' % o[:80]))
    check('--tail negatif refuse',
          LOCAL + ['logs', '--job', '101', '--tail', '-1'],
          expect=2, verify=want_clean_error('--tail'))
    check('--clean raw conserve l ANSI', LOCAL + ['logs', '--job', '101', '--clean', 'raw'],
          want_out('\x1b['))
    check('job sans related -> fallback stdout/', LOCAL + ['logs', '102'],
          want_out('PLAY RECAP'))
    check('objet 404 -> avertit puis tente stdout', LOCAL + ['logs', '103'],
          want_err('103'))
    check('--template (le plus recent)', LOCAL + ['logs', '--template', 'patch-linux'],
          want_err('job 101'))
    check('--template par substring', LOCAL + ['logs', '--template', 'deploy'],
          want_err('job 102'))
    check('template inexistant -> erreur nommee',
          LOCAL + ['logs', '--template', 'inexistant'], want_err('inexistant'), expect=1)
    check('sans argument -> aide', LOCAL + ['logs'], want_err('--job'), expect=1)
    check('--output json avec job_id',
          LOCAL + ['logs', '--job', '101', '--output', 'json'],
          lambda o, e: None if want_json(o).get('job_id') == 101 else fail('job_id'))
    check('--output yaml', LOCAL + ['logs', '--job', '101', '--output', 'yaml'],
          want_out('job_id:', 'stdout:'))
    check('--output csv', LOCAL + ['logs', '--job', '101', '--output', 'csv'],
          want_out('job_id,stdout'))
    check('--output json respecte --tail 0',
          LOCAL + ['logs', '--job', '101', '--output', 'json', '--tail', '0'],
          lambda o, e: None if want_json(o).get('stdout') == ''
          else fail('stdout devrait etre vide'))
    check('--url forcee', LOCAL + ['logs', '--job', '101',
                                   '--url', '/api/gateway/v1/job_runs/101/stdout/'],
          want_out('PLAY RECAP'))


def suite_api():
    print('\n--- api / endpoints / decouverte ---')
    check('GET arbitraire', LOCAL + ['api', '/api/gateway/v1/users/'],
          lambda o, e: None if want_json(o).get('count') == 45 else fail('count'))
    check('--param page=2', LOCAL + ['api', '/api/gateway/v1/users/',
                                     '--param', 'page=2', '--param', 'page_size=1'],
          lambda o, e: None if want_json(o)['results'][0]['id'] == 2 else fail('page'))
    check('--discover publie les bases', LOCAL + ['api', '--discover'],
          lambda o, e: None if 'published' in want_json(o) else fail('published'))
    check('api --output yaml', LOCAL + ['api', '/api/gateway/v1/users/',
                                        '--output', 'yaml'],
          want_out('count:', 'results:'))
    check('api --output csv', LOCAL + ['api', '/api/gateway/v1/users/',
                                       '--output', 'csv'],
          want_out('key,value', 'count'))
    check('endpoints --output json', LOCAL + ['endpoints', '--output', 'json'],
          lambda o, e: None if want_json(o).get('host') else fail('host'))
    check('endpoints --output csv', LOCAL + ['endpoints', '--output', 'csv'],
          want_out('key,value', 'endpoints.users'))
    check('api sans chemin -> erreur', LOCAL + ['api'], expect=1)
    check('chemin relatif refuse', LOCAL + ['api', 'api/gateway/v1/'], expect=1)
    check('404 -> TIP 404', LOCAL + ['api', '/api/gateway/v1/nope/'],
          want_err('TIP'), expect=1)
    check('503 -> TIP 503', LOCAL + ['api', '/api/gateway/v1/error503/'],
          want_err('TIP'), expect=1)
    check('pagination: lien vers un autre hote refuse',
          LOCAL + ['--endpoint', 'users=/api/gateway/v1/foreign_next/', 'users'],
          expect=1, verify=want_clean_error('another host'))
    check('POST via -d', LOCAL + ['api', '/api/gateway/v1/ping/', '-d', '{"a":1}'],
          expect=1, verify=want_err('405'))
    check('406 tolere (me est une vue DAB)', LOCAL + ['whoami'], want_out('testuser'))
    check('endpoints: base 204 non retenue', LOCAL + ['endpoints'],
          want_line("API bases", '/api/controller/', present=False))
    check('endpoints: base gateway retenue', LOCAL + ['endpoints'],
          want_line("API bases", '/api/gateway/'))
    check('endpoints --set confirme',
          LOCAL + ['endpoints', '--set', 'users=/api/gateway/v1/users/'],
          want_out('endpoints.users'))
    check('endpoints --set --output json',
          LOCAL + ['endpoints', '--output', 'json',
                   '--set', 'users=/api/gateway/v1/users/'],
          lambda o, e: None if want_json(o).get('users') else fail('users'))
    check('endpoints --set invalide', LOCAL + ['endpoints', '--set', 'users=bad'],
          want_err('invalid path'), expect=1)
    check('endpoint inexistant -> erreur nommee',
          LOCAL + ['--endpoint', 'me=/api/gateway/v1/nope/', 'whoami'],
          want_err('me'), expect=1)
    check('controller non monte -> conseil cible',
          LOCAL + ['--endpoint',
                   'job_templates=/api/controller/api/v2/job_templates/', 'templates'],
          expect=1, verify=want_err('job_templates'))
    check('decouverte: ping public sans auth', LOCAL + ['--no-login', 'api',
                                                        '/api/gateway/v1/ping/'],
          want_out('"ok"'))


def suite_shell():
    print('\n--- shell interactif ---')
    check('intro + historique + exit', LOCAL + [], stdin='status\nexit\n',
          verify=want_out('commands:', 'goodbye'))
    check('commande inconnue ne casse pas', LOCAL + [], stdin='zzzbzz\nexit\n',
          verify=want_out('unknown command'))
    check('survit a une erreur', LOCAL + [], stdin='logs\nstatus\nexit\n',
          verify=want_out('ERROR', 'application_version'))
    check('--output ne deborde pas sur la suite', LOCAL + [],
          stdin='whoami --output json\nwhoami\nexit\n',
          verify=want_out('"username"', 'field'))
    check('help <cmd> documente', LOCAL + [], stdin='help logs\nexit\n',
          verify=want_out('stdout'))
    check('help liste les commandes', LOCAL + [], stdin='help\nexit\n',
          verify=want_out('templates', 'logs'))
    check('quit comme exit', LOCAL + [], stdin='quit\n', verify=want_out('goodbye'))
    check('ligne vide ignoree', LOCAL + [], stdin='\n\nexit\n',
          verify=want_out('goodbye'))
    check('quotes protegees (grep avec espace)', LOCAL + [],
          stdin="logs --job 101 --grep 'PLAY RECAP'\nexit\n",
          verify=want_out('PLAY RECAP'))
    check('--limit local au shell', LOCAL + [], stdin='users --limit 3\nexit\n',
          verify=want_out('(3 item(s))'))


def suite_robustesse():
    print('\n--- robustesse ---')
    check('commande inconnue en ligne', LOCAL + ['zzzbzz'], expect=1)
    check('hote injoignable -> erreur reseau',
          LOCAL + ['--host', 'http://127.0.0.1:1', 'status'], expect=1,
          verify=want_err('ERROR'))
    check('unreachable host -> concise error',
          LOCAL + ['--host', 'http://127.0.0.1:1', 'status'], expect=1,
          verify=lambda o, e: None if len([line for line in e.splitlines()
                                           if line.strip()]) == 2
          else fail('expected 2 lines, got: %r' % e))
    check('TLS vers un serveur http -> erreur',
          LOCAL + ['--host', 'https://127.0.0.1:%d' % port(), 'status'], expect=1)
    check('TLS -> TIP ca_bundle conserve jusqu au login',
          LOCAL + ['--host', 'https://127.0.0.1:%d' % port(), 'whoami'],
          expect=1, verify=want_err('ca_bundle'))
    check('status: transport -> no response',
          LOCAL + ['--no-login', '--host', 'https://127.0.0.1:%d' % port(), 'status'],
          verify=lambda o, e: None if 'no response' in o and 'HTTP None' not in o
          else fail('status transport: %r' % o[:200]))
    check('insecure rappele sur du http (sans TLS)',
          LOCAL + ['--no-login', 'endpoints'], want_no('DISABLED'))
    check('AAP_DEBUG trace les requetes',
          LOCAL + ['whoami'], env={'AAP_DEBUG': '1'}, verify=want_err('# >'))
    check('AAP_DEBUG=0 ne trace pas',
          LOCAL + ['whoami'], env={'AAP_DEBUG': '0'}, verify=want_no('# >'))
    check('RHAAP_DEBUG=0 ne trace pas',
          LOCAL + ['whoami'], env={'RHAAP_DEBUG': '0'}, verify=want_no('# >'))
    check('RHAAP_DEBUG reste accepte (alias)',
          LOCAL + ['whoami'], env={'RHAAP_DEBUG': '1'}, verify=want_err('# >'))
    check('tous les profils listent leurs cles',
          LOCAL[:2] + ['--list-profiles'], want_out('alias_keys', 'session_profile'))
    check('--grep invalide -> erreur claire, pas de traceback',
          LOCAL + ['logs', '--job', '101', '--grep', '['],
          expect=1, verify=want_clean_error('regular expression'))
    check('-d JSON invalide -> erreur claire, pas de traceback',
          LOCAL + ['api', '/api/gateway/v1/ping/', '-d', '{bad'],
          expect=1, verify=want_clean_error('invalid JSON'))
    check('timeout de config non numerique -> erreur claire',
          ['-f', CONF, '-P', 'bad_timeout', '--no-login', 'endpoints'],
          expect=1, verify=want_clean_error('invalid timeout'))
    check('--limit non numerique -> argparse propre',
          LOCAL + ['templates', '--limit', 'abc'], expect=2,
          verify=want_clean_error('--limit'))


def port():
    return _PORT


def main():
    try:
        start_mock()
    except Exception as exc:
        stop_mock()
        print('[FATAL] %s' % exc, file=sys.stderr)
        return 2

    log_tail = ''
    try:
        for suite in (suite_config, suite_auth, suite_templates, suite_pagination,
                      suite_logs, suite_api, suite_shell, suite_robustesse):
            suite()
    finally:
        log_tail = mock_log_tail()
        stop_mock()

    failures = [item for item in RESULTS if not item[0]]
    print('\n' + '=' * 72)
    print('%d ok, %d echec(s) sur %d' % (len(RESULTS) - len(failures), len(failures),
                                         len(RESULTS)))
    if failures and VERBOSE:
        for _, name, problem, out, err in failures:
            print('\n--- %s\n%s\n--- stdout ---\n%s\n--- stderr ---\n%s'
                  % (name, problem, out[:900], err[:900]))
        if log_tail.strip():
            print('\n--- mock.log ---\n%s' % log_tail)
    elif failures:
        print('(relance avec -v pour le detail)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

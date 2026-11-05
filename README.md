# aap_api_cli

**A small, dependency-free companion for exploring Red Hat Ansible Automation
Platform (AAP) 2.5 from your terminal.**

It is one executable Python file. 
No package installation, no virtual environment, or build step is required.

AAP installations do not always expose the same URL layout: the gateway and
controller may live on different bases, and a reverse proxy can add another
layer of surprise. This CLI asks the server what it actually publishes, then
helps you inspect templates, jobs, stdout, users, and the raw API without
memorising those paths.

## Start here

### 1. Run the script

You need Python **3.8+** and network access to your AAP gateway (and controller
if it is hosted separately). `readline` is optional; without it, one-shot
commands still work but the interactive shell has no history or Tab completion.

Run it directly from this repository:

```bash
./aap_api_cli.py --version
```

Or install it somewhere on your `PATH`:

```bash
install -m 755 aap_api_cli.py ~/.local/bin/aap_api_cli
```

### 2. Tell it how to reach AAP

Generate a private configuration file, then add your host and user:

```bash
./aap_api_cli.py --generate-config > ~/.aap_api_cli.conf
chmod 600 ~/.aap_api_cli.conf
$EDITOR ~/.aap_api_cli.conf
```

The smallest useful configuration looks like this:

```ini
[default]
host = aap.apps.example.internal
user = admin
pass = CHANGEME
auth_method = basic
timeout = 30

# Prefer an internal CA over disabling certificate verification:
# ca_bundle = ~/.internal-ca.pem
```

`CHANGEME` deliberately triggers a password prompt rather than sending a known
placeholder to the server. For a test environment only, `tls_insecure = true`
is available and produces a warning on every run.

### 3. Find a template, its jobs, and their logs

Most people come here to answer three questions: *which template should I
use?*, *what happened to its recent jobs?*, and *what does a particular run
say?* Start with that familiar path:

```bash
./aap_api_cli.py templates --name inventory       # find a template
./aap_api_cli.py jobs --template "Daily inventory check" # inspect its runs
./aap_api_cli.py logs 8174 --tail 50              # read one run's stdout
```

Run `./aap_api_cli.py` with no command to open an interactive shell. It has
history, Tab completion, and built-in `help`.

If those commands cannot find a resource, `./aap_api_cli.py endpoints` shows
the API bases and endpoints discovered on that AAP installation.

> The regular commands only make `GET` requests. The `api` command is a
> deliberate diagnostic escape hatch and can send writes when asked to do so.

## Connection details

### Profiles

A profile is one host/account pair. Use several when the controller is hosted
separately or when you need different roles:

```ini
[default]
host = aap.apps.example.internal
user = admin
pass = CHANGEME
auth_method = basic

[controller]
host = aap-controller.apps.example.internal
user = admin
pass = CHANGEME
```

Select one with `-P/--profile`, the `AAP_PROFILE` environment variable, or let
the tool choose the only profile (or `[default]`):

```bash
./aap_api_cli.py --list-profiles
./aap_api_cli.py -P controller status
```

### Supported configuration keys

| Key | Aliases | Meaning |
|---|---|---|
| `host` | — | Gateway base URL; `https://` is added when absent |
| `user` | `username`, `login` | API login |
| `pass` | `password` | Password |
| `token` | — | Bearer token; switches `auth_method` to `token` |
| `auth_method` | — | `basic` (default), `session`, `token` |
| `tls_insecure` | — | Disable certificate verification (warns on every run) |
| `ca_bundle` | `ca_file` | Internal CA bundle (PEM), preferred over `tls_insecure` |
| `timeout` | — | HTTP timeout in seconds (default `30`) |

The file is parsed **without interpolation**, so `%` and `${…}` in passwords
stay literal. `load_config()` warns if the file is readable by other users.

### Passwords

Resolution order, highest priority first:

1. `--password-stdin`
2. `--password-env VAR`
3. `--password-cmd 'pass show aap/admin'`
4. `pass` / `password` from the profile
5. interactive prompt (only when stdin is a terminal)

This keeps secrets out of shell history for unattended use:

```bash
./aap_api_cli.py --password-env AAP_PASS whoami
pass show aap/admin | ./aap_api_cli.py --password-stdin logs 42
```

### Tokens

Token auth is recommended when a token exists:

```ini
[default]
host = aap.apps.example.internal
auth_method = token
token = your-token-here
```

`--token` overrides it. A missing token, or the placeholder `CHANGEME`, is a
**named error**, never a silent fallback to Basic. Prefer the config file for
tokens: `--token` is visible in shell history and process listings.

### Endpoint overrides

Discovery normally finds the mounted bases. Override a logical endpoint only
when a component is hosted somewhere unexpected:

```ini
[endpoints]
job_templates = /api/controller/v2/job_templates/
```

Paths must start with `/` or `http`. The same override is available per command
with `--endpoint key=/path`; it is not persisted.

## Everyday use

Here are a few useful one-shot commands. Add `--output json`, `--output csv`,
or `--output yaml` whenever the result is going into another tool:

```bash
./aap_api_cli.py status                 # gateway status + ping
./aap_api_cli.py whoami                 # current identity (sensitive fields masked)
./aap_api_cli.py whoami --full          # show every field, including the password hash
./aap_api_cli.py endpoints              # discovered bases and logical endpoints
./aap_api_cli.py api --discover         # raw map published by the server
```

Without a command, the tool opens an interactive shell with history and Tab
completion. `help`, `help <command>`, `history`, `exit`/`quit`/`Ctrl-D` all work
there.

### The usual path: templates → jobs → logs

The following interactive session uses entirely fictional names, IDs, dates,
and log content. It is the normal investigation flow on a real platform.
Commands are plural: use `templates`, not `template`.

```text
aap> templates --name inventory
id    name                     organization  type  last_run
1042  Daily inventory check    Default       run   2026-04-18T07:30:12Z
1061  Inventory report (test)  Default       run   2026-04-17T16:10:43Z
(2 item(s))

aap> jobs --template "Daily inventory check" --limit 3
id    name                   template               status      elapsed  finished
8174  Daily inventory check  Daily inventory check  failed      42.118   2026-04-18T07:30:12Z
8169  Daily inventory check  Daily inventory check  successful  39.804   2026-04-17T07:30:11Z
8164  Daily inventory check  Daily inventory check  successful  41.006   2026-04-16T07:30:12Z
(3 item(s))

aap> logs 8174 --tail 4
TASK [Validate managed hosts] *************************************************
fatal: [node-01.example.test]: FAILED! => {"msg": "inventory service unavailable"}
PLAY RECAP *******************************************************************
node-01.example.test : ok=8 changed=0 unreachable=0 failed=1
```

For the most recent run of a template, you can jump straight to its output:

```bash
./aap_api_cli.py logs --template "Daily inventory check" --tail 50
```

When looking for failures across all templates, narrow the job list first, then
open the relevant log:

```bash
./aap_api_cli.py jobs --status failed --limit 20
./aap_api_cli.py logs 8174 --grep 'failed|unreachable' --tail 80
```

### Global options

Useful on any command:

| Option | Effect |
|---|---|
| `-f/--config-file PATH` | Alternate config file |
| `-P/--profile NAME` | Select a profile |
| `--host`, `--user` | Override the profile values |
| `--auth basic\|session\|token` | Override the authentication method |
| `--token TOKEN` | Bearer token (or use the profile's `token`); `--jeton` is a hidden legacy alias |
| `--no-login` | Skip the startup authentication check |
| `--ca-bundle PEM`, `--insecure` | TLS verification controls |
| `--timeout N` | HTTP timeout |
| `--endpoint key=/path` | Override one logical endpoint |
| `-v/--verbose` | Same as `AAP_DEBUG=1` |
| `--version` | Print the version |

### Command reference

| Command | Purpose | Main options |
|---|---|---|
| `status` | Gateway status and ping | — |
| `whoami` | Current identity (`/me/`) | `--full` |
| `templates` | Job templates | `--workflow`, `--name`, `--exact`, `--fields`, `--only-ids` |
| `jobs` | Recent job runs | `--template`, `--status`, `--order`, `--only-ids` |
| `logs` | stdout of a job | `--job`, `--template`, `--url`, `--tail`, `--grep`, `--clean` |
| `api` | Arbitrary request or discovery | `-X/--method`, `-d/--body`, `--param k=v`, `--follow`, `--discover` |
| `endpoints` | View/override logical endpoints | `--set key=/path` |
| `users` | Gateway users | — |
| `organizations` | Gateway organizations | — |

### Reading job logs

Accepted job forms:

```bash
./aap_api_cli.py logs 42
./aap_api_cli.py logs --job 42
./aap_api_cli.py logs job:42
./aap_api_cli.py logs --template patch-linux       # most recent run of that template
```

Useful filters:

```bash
./aap_api_cli.py logs 42 --tail 50
./aap_api_cli.py logs 42 --grep failed
./aap_api_cli.py logs 42 --clean raw | less -R     # keep ANSI colours
```

`--tail 0` means “no line”; negative values are rejected. `--grep` is a regex,
case-insensitive. Filters apply before serialization, so they also affect
`json`/`yaml`/`csv` output.

### Output formats

`--output` is **universal**: every command accepts `table`, `json`, `csv`, or
`yaml`. The flag works before or after the command name.

| Format | Behaviour |
|---|---|
| `table` | Human view (default). For `api`, pretty-printed JSON is the human view. |
| `json` | Structured JSON, suitable for `jq` |
| `yaml` | YAML, produced by a small stdlib-only converter |
| `csv` | Tabular projection |

CSV details:

- `templates`, `jobs`, `users`, `organizations`: one row per record.
- `logs`: one row with `job_id,stdout` (multi-line field is quoted).
- `whoami`, `status`, `endpoints`: `key,value` rows.
- `api`: a top-level object becomes `key,value`; a list of objects becomes one
  row per object.

Two intentional exceptions:

- `--only-ids` always prints raw IDs, one per line, regardless of `--output`.
  It is meant for pipelines.
- `--fields` projects fields for `json`/`yaml` only; `table` and `csv` ignore it.

Examples:

```bash
./aap_api_cli.py templates --name patch --only-ids | xargs -n1 echo
./aap_api_cli.py jobs --limit 5 --output csv
./aap_api_cli.py whoami --output yaml
./aap_api_cli.py api /api/controller/v2/users/ --output csv
```

### Inspecting arbitrary API endpoints

`api` is the diagnostic escape hatch. It sends exactly what you ask for:

```bash
./aap_api_cli.py api /api/gateway/v1/users/
./aap_api_cli.py api /api/gateway/v1/users/ --param page=2 --param page_size=1
./aap_api_cli.py api /api/gateway/v1/ping/ -d '{"probe": 1}'
./aap_api_cli.py api /api/gateway/v1/ --output yaml
```

`-d/--body` switches the default `GET` to `POST`; use `-X` for another method.
Invalid JSON is a named error, not a traceback.

## Stay safe

- Business commands are **read-only**: they issue `GET` requests.
- `api` is deliberately unrestricted and **can mutate the server**. Read-only
  use is a matter of discipline, not enforcement.
- Secrets are never printed: `endpoints`, `AAP_DEBUG`, and the `Credentials`
  `repr` do not expose passwords or tokens. `whoami` masks sensitive fields
  unless `--full` is given.
- `ca_bundle` is the normal production path. `tls_insecure = true` warns on
  every run.
- Keep the config at `600`; the tool warns when it is readable by others.
- Prefer `--password-stdin`/`--password-env`/`--password-cmd` and the config file
  for tokens over command-line arguments.

## Developing and testing

The test suite is self-contained: it starts its own mock AAP server on a free
port, writes a temporary config, runs, then cleans up. No mock needs to be
started manually.

```bash
python3 tests/test_aap_api_cli.py           # comprehensive custom suite
python3 tests/test_aap_api_cli.py -v        # failure details + mock log tail
python3 tests/test_aap_api_cli.py --keep    # keep the temporary config and log
MOCK_PORT=18099 python3 tests/test_aap_api_cli.py   # force a port
```

For a quick check, `tests/test_smoke.py` is a basic standard-library `unittest`
suite (8 cases). It is also collected by pytest:

```bash
python3 tests/test_smoke.py
python3 -m unittest discover -s tests -v
```

## Environment variables

| Variable | Effect |
|---|---|
| `AAP_PROFILE` | Default profile when `-P` is absent |
| `AAP_DEBUG=1` | Trace HTTP requests and endpoint resolution on stderr |
| `AAP_PASSE` | Only used when passed via `--password-env AAP_PASSE` |

`RHAAP_PROFILE` and `RHAAP_DEBUG` remain accepted as legacy aliases for scripts
written before the rename to `AAP_*`.

## When something does not work

Enable tracing first:

```bash
AAP_DEBUG=1 ./aap_api_cli.py endpoints
```

- **401** — credentials rejected, or the auth method is not accepted. Try
  `--auth session` or a token.
- **403** — insufficient role, or a CSRF/Referer rejection in session mode.
- **404** — the component is not mounted on this vhost. Check `endpoints` and
  `api --discover`, then override with `--endpoint key=/path` or another
  profile.
- **503** — service or dependency unavailable. Check `ping` (`db_connected`,
  `proxy_connected`).
- **No HTTP response** — DNS, VPN, firewall, or TLS. The message distinguishes
  this from an authentication failure on purpose. It is intentionally short;
  run with `AAP_DEBUG=1` to see the probed bases.

## Current limitations

- `0.1`: the business commands still need revalidation against the real target.
- The test mock does not model a real `/api/controller/v2/` and only serves
  `text/plain` stdout; the `Accept`-negotiation fallback for logs is therefore
  not exercised by the suite.
- `jobs --template NAME` uses the server-side exact-name filter without a broad
  client-side fallback (`logs --template` does have one).
- `templates --name` without `--exact` filters after loading `--limit` items, so
  a match beyond the limit is not seen.
- `--fields` is ignored outside `json`/`yaml`.


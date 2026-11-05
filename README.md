# aap_api_cli

A single-file, dependency-free command-line client for the **Red Hat Ansible
Automation Platform (AAP) 2.5** REST API.

It is a diagnostic tool: it discovers which API bases the server actually
publishes, handles the gateway/controller split, and exposes job templates, job
runs, logs, users, and arbitrary API calls.

```
aap_api_cli.py templates --name patch
aap_api_cli.py jobs --limit 10 --output json | jq '.[0]'
aap_api_cli.py logs 42 --tail 50
aap_api_cli.py                 # interactive shell
```

> **Status:** `0.1`. Authentication and API discovery have been validated on a
> real AAP 2.5 installation. The business commands (`logs`, `whoami`, …) were
> fixed after that validation but have not yet been re-run against the target.
> See [Limitations](#limitations) before relying on it operationally.

> **Renamed in 0.1:** the script was `rhaap_api_cli.py`. The config and history
> files are now `~/.aap_api_cli.conf` and `~/.aap_api_cli_history`; there is no
> fallback to the old config path. `RHAAP_PROFILE` and `RHAAP_DEBUG` remain
> accepted as aliases for `AAP_PROFILE` and `AAP_DEBUG`.

---

## Requirements

- Python **3.8+** (developed and run on 3.14)
- Network access to the AAP gateway and, if separate, the controller
- No pip package, no virtualenv, no build step
- `readline` is optional; without it the one-shot CLI still works and the
  interactive shell loses history/Tab completion

## Installation

Copy the script somewhere on `PATH` and make it executable:

```bash
install -m 755 aap_api_cli.py ~/.local/bin/aap_api_cli
# or keep the original name:
# install -m 755 aap_api_cli.py ~/.local/bin/aap_api_cli.py
```

From the repository you can also run it directly:

```bash
./aap_api_cli.py --version
```

## Configuration

Create a config file with the built-in template:

```bash
./aap_api_cli.py --generate-config > ~/.aap_api_cli.conf
chmod 600 ~/.aap_api_cli.conf
$EDITOR ~/.aap_api_cli.conf
```

Minimal example:

```ini
[default]
host = aap.apps.example.internal
user = admin
pass = CHANGEME
tls_insecure = true
auth_method = basic
timeout = 30

# Prefer an internal CA over disabling verification:
# ca_bundle = ~/.internal-ca.pem
```

On the next run, a `CHANGEME` password triggers an interactive prompt instead of
producing an obscure `401`.

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
the tool use the only profile / `[default]`.

```bash
./aap_api_cli.py --list-profiles
./aap_api_cli.py -P controller status
```

### Supported keys

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

## Usage

One-shot commands:

```bash
./aap_api_cli.py status                 # gateway status + ping
./aap_api_cli.py whoami                 # current identity (sensitive fields masked)
./aap_api_cli.py whoami --full          # show every field, including the password hash
./aap_api_cli.py endpoints              # discovered bases and logical endpoints
./aap_api_cli.py api --discover         # raw map published by the server
```

Without a command, the tool opens an interactive shell with history and Tab
completion. `help`, `help <command>`, `history`, `exit`/`quit`/`Ctrl-D` work
there.

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

### Commands

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

### Job logs

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

### Arbitrary API calls

`api` is the diagnostic escape hatch. It sends exactly what you ask for:

```bash
./aap_api_cli.py api /api/gateway/v1/users/
./aap_api_cli.py api /api/gateway/v1/users/ --param page=2 --param page_size=1
./aap_api_cli.py api /api/gateway/v1/ping/ -d '{"probe": 1}'
./aap_api_cli.py api /api/gateway/v1/ --output yaml
```

`-d/--body` switches the default `GET` to `POST`; use `-X` for another method.
Invalid JSON is a named error, not a traceback.

## Security

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

## Tests

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

## Troubleshooting

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

## Limitations

- `0.1`: the business commands still need revalidation against the real target.
- The test mock does not model a real `/api/controller/v2/` and only serves
  `text/plain` stdout; the `Accept`-negotiation fallback for logs is therefore
  not exercised by the suite.
- `jobs --template NAME` uses the server-side exact-name filter without a broad
  client-side fallback (`logs --template` does have one).
- `templates --name` without `--exact` filters after loading `--limit` items, so
  a match beyond the limit is not seen.
- `--fields` is ignored outside `json`/`yaml`.

For architecture, discovery internals, and maintenance traps, read
[`AGENTS.md`](AGENTS.md).

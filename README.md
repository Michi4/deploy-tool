# deploy-tool

**In short:** Put files, users and permissions on many Linux servers from a
recipe - idempotent, traceable and with rollback.

**In detail:** A recipe describes *what* happens (paths, permissions,
validation, tests). A profile describes *where* it happens (hosts, access,
values). `deploy.py` first compares the actual state via SHA-256, writes only
differences, backs up the previous file and rolls back on a critical test
failure. Multiple hosts run in parallel; each gets its own summary. Pure Python
(>= 3.11, standard library), no dependencies, no agents on the servers.

## Two repos, one path

`deploy-tool` is the general deployment side. The `acme-target` recipe prepares
hosts for [acme-rollout](https://github.com/Michi4/acme-rollout): it creates the
`acmepush` user, the forced command and the sudoers rule that accept a
certificate bundle. `acme-rollout` issues the certificates and sends them
through exactly this path.

```
deploy-tool                          acme-rollout
recipes/acme-target                  acme.sh + deploy hook
        │                                     │
        │  sets up the receiver               │  issues and pushes
        └───────────────┐                     │
                        ▼                     ▼
                 ┌──────────────────────────────────┐
                 │ Target host                      │
                 │ acmepush -> acme-target          │
                 │ /etc/ssl/<name>.{crt,key,ca}     │
                 └──────────────────────────────────┘
```

Both repos work on their own. Together they form the complete path from
issuance to the file on the web server.

## Layers

```
.env                         only PROFILE: which profile applies without -p
profiles/<name>.env          targets, access and values (not in the repo)
recipes/<name>/recipe.toml   flow, data-free, only {{PLACEHOLDERS}}
scripts/                     payload
keys/                        public keys
```

Rule of thumb: a recipe contains no server names, a profile contains no logic.
Precedence for the same name: CLI > profile > `.env` > default.

## Quick start

```bash
cp .env.example .env
cp profiles/example.env profiles/local.env
$EDITOR .env profiles/local.env

./deploy.py selftest
./deploy.py plan   -p local
./deploy.py deploy -p local
```

`-p local` layers `profiles/local.env` over `.env`. Without `-p`, the `PROFILE`
from `.env` applies. `profiles/*.env` is excluded from Git; only files with
`example` in the name are templates. Before the first real run, adjust
`SSH_KEY`, `TARGETS` and the values in the profile.

## Commands

| Purpose | Call |
|---|---|
| Menu | `./deploy.py` |
| Show changes | `./deploy.py plan -p local` |
| Roll out | `./deploy.py deploy -p local` |
| Check drift | `./deploy.py status -p local` |
| Tests | `./deploy.py test -p local` |
| Check locally | `./deploy.py selftest` |
| Recipes / profiles | `./deploy.py recipes` / `./deploy.py profiles` |
| Single file | `./deploy.py push file --dest /path` |
| Remote command | `./deploy.py run -p local -- 'uptime'` |
| Variable reference | `./deploy.py help-env` |

`-j 1` to `-j 16` controls parallelism (default 4). `-t host1,host2` runs
against other targets without changing the profile. `-s NAME=VALUE` overrides
individual values.

## Run per host

1. Connection, program check, `[[preflight]]`
2. Target/actual comparison via SHA-256; identical files are skipped
3. Write to a temporary file, set permissions, `validate`, atomically via `mv`
4. Back up the previous file and record it in the registry
5. `[[tests]]`; a critical failure rolls back the changed files
6. Summary per host: `STATUS` (OK/FAIL) separate from `TEST` (PASS/WARN/FAIL/SKIP)

`plan` and `status` write nothing. `--force` forces an update even when the
content is identical. Repeated runs are safe.

## Profiles

`profiles/example.env` shows `site-rescue-audit`,
`profiles/example-acme.env` shows `acme-target`. The most important values:

| Value | Meaning |
|---|---|
| `RECIPE`, `TARGETS` | Recipe and target hosts |
| `SSH_USER`, `SSH_KEY`, `SSH_PORT` | Access to the target host |
| `SYSTEM_USER`, `SYSTEM_USER_GROUPS` | User that the recipe creates |
| `WHITELIST_IP`, `RESCUE_PUBKEY` | Allowed source IP and public key |
| `SUDOERS_NAME`, `SUDOERS_CONTENT` | Sudoers file and content |
| `ACME_USER`, `ACME_PUSH_PUBKEY`, `ACME_PUSH_IP` | Receiver for acme-rollout |
| `SCRIPTS_DIR`, `BIN_DIR` | Payload and destination directory |
| `ALLOW_EXISTING_USER`, `ROLLBACK_ON_FAIL` | Behaviour of the run |

## Recipes

| Recipe | Purpose |
|---|---|
| `acme-target` | Certificate receiver for acme-rollout: user, forced command, sudoers, acceptance under `/etc/ssl` |
| `site-rescue-audit` | `site-rescue` and `site-audit` including user, sudoers, whitelist and tests |
| `one-script` | Template: one file to a target path |
| `push` | Ad-hoc single file, values come from the command line |

```bash
./deploy.py plan   -r acme-target -p example-acme
./deploy.py deploy -r acme-target -p example-acme

./deploy.py plan   -p example
./deploy.py deploy -p example
./deploy.py test   -p example -s TEST_AUDIT=1 -t web1.example.com
```

`site-audit` checks a web server read-only (versions, updates, suspicious
processes, CMS and webshell patterns). `site-rescue` is the counterpart for
operations: `diag`, `block`, `unblock`, `audit`, `restart-pool` - reachable only
through the forced command of the `rescue` user.

## New server

Add it to `TARGETS` in the profile and run `deploy` again. The run is
idempotent: missing items are created, identical items stay untouched,
divergent items are backed up and replaced.

## New profile

```bash
cp profiles/example.env profiles/customer.env
$EDITOR profiles/customer.env
./deploy.py plan -p customer
```

Required depending on the recipe: `RECIPE`, `TARGETS`, `SSH_USER`, `SSH_KEY`.
`SCRIPTS_DIR` selects a different payload for the same recipe.

## New recipe

`recipes/one-script/` is the template for one file,
`recipes/site-rescue-audit/` the template for two related files. Copy it, name
the placeholders, put the scripts into `scripts/`, run `selftest`.

Important fields per `[[files]]`:

| Field | Meaning |
|---|---|
| `src`, `dest` | Source and destination, placeholders allowed |
| `mode`, `owner`, `group` | Permissions; sudoers files `0440` |
| `validate` | Check before writing (`bash -n {{FILE}}`, `visudo -cf {{FILE}}`) |
| `on_conflict` | `fail`, `keep` or `overwrite` |
| `unless_cmd` | Skip the step if the command reports success |
| `description` | Plain text in the output |

## Tests

`selftest` checks recipes, placeholders, sources, permissions and checksums
without touching a server. `selftest --ssh` adds a read-only connection test.
`test` runs the `[[tests]]` of a recipe; `critical = false` turns a failure into
a warning, `-s TEST_AUDIT=1` enables optional long-running tests.

## Security

- Profiles and `.env` contain credentials and are not in the repo.
- `keys/` contains only public keys; the private key stays local.
- Sudoers files are checked with `visudo -cf` before activation.
- Changes are backed up; critical test failures trigger a rollback.
- The `acme-target` receiver accepts only `status` and `install <name>` and
  writes exclusively to `/etc/ssl`.

## Layout

```
deploy.py                     CLI
.env.example                  template for PROFILE
profiles/example.env          example: site-rescue-audit
profiles/example-acme.env     example: acme-target
recipes/acme-target/          certificate receiver
recipes/site-rescue-audit/    site-rescue + site-audit
recipes/one-script/           template
recipes/push/                 single file
scripts/acme-target           receiver script
scripts/site-audit            read-only weekly check
scripts/site-rescue           operations helper
scripts/example.sh            placeholder
keys/example_key.pub          throwaway public key for the examples
```

#!/usr/bin/env python3
"""
deploy.py v2.0.0 - Universelles, rezept- und direktgesteuertes Linux-Deployment-Werkzeug.
Basiert ausschliesslich auf der Python-Standardbibliothek (Python >= 3.11).
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import difflib
import glob as globmod
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("deploy.py erfordert Python >= 3.11 (tomllib).")

VERSION = "2.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent

STD_DEFAULTS: dict[str, str] = {
    "RECIPE": "",
    "PROFILE": "",
    "TARGETS": "",
    "INVENTORY": "",
    "SSH_USER": "root",
    "SSH_PORT": "22",
    "SSH_KEY": "~/.ssh/id_ed25519",
    "SSH_OPTS": "",
    "SSH_STRICT": "accept-new",
    "SUDO": "auto",
    "JOBS": "4",
    "TIMEOUT": "120",
    "CONNECT_TIMEOUT": "10",
    "STATE_DIR": "/var/lib/deploy-tool",
    "KEEP_GOING": "0",
    "ROLLBACK_ON_FAIL": "0",
    "ASSUME_YES": "0",
    "ALLOW_EXISTING_USER": "0",
    "ON_CONFLICT": "ask",
    "TEST_HOST": "",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUDOERS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

AUTH_LINE_RE = re.compile(
    r'^(?:(?P<options>(?:[a-zA-Z0-9_\-]+(?:="[^"]*")?,?)+)\s+)?'
    r'(?P<type>ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+'
    r'(?P<key>[A-Za-z0-9+/=]+)'
    r'(?:\s+(?P<comment>.*))?$'
)

class ConfigError(Exception):
    """Fehler in Konfiguration, Parametern oder Syntax."""

class StepFailed(Exception):
    """Fehlschlag eines Ausfuehrungsschritts."""

class UI:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    stream = sys.stdout
    lock = threading.Lock()
    TAGS = {
        "OK": "32", "PASS": "32", "CHG": "33", "FAIL": "31;1",
        "WARN": "33", "INFO": "36", "SKIP": "90", "DRY": "35", "ASK": "33;1"
    }

    @classmethod
    def paint(cls, tag: str, text: str) -> str:
        if not cls.color:
            return text
        return f"\033[{cls.TAGS.get(tag, '0')}m{text}\033[0m"

    @classmethod
    def emit(cls, text: str) -> None:
        with cls.lock:
            try:
                print(text, file=cls.stream, flush=True)
            except BrokenPipeError:
                with contextlib.suppress(Exception):
                    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                os._exit(141)

def truthy(v: object) -> bool:
    return str(v).strip().lower() not in ("", "0", "false", "no", "off", "none", "nein")

def short(s: str, n: int = 400) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."

def q(s: str) -> str:
    return shlex.quote(s)

def b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def split_words(s: str) -> list[str]:
    return [x for x in re.split(r"[,\s]+", s.strip()) if x]

def norm_mode(m: object) -> str:
    s = str(m).strip()
    if not re.fullmatch(r"0?[0-7]{3,4}", s):
        raise ConfigError(f"mode muss oktal sein, z. B. '0755' (angegeben: {m!r})")
    return format(int(s, 8), "o").rjust(3, "0")

_VAR_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")

def _expand(value: str, known: dict[str, str]) -> str:
    def rep(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2) or ""
        return known.get(name) or os.environ.get(name) or default
    return _VAR_RE.sub(rep, value)

def parse_dotenv(path: Path, inherited: dict[str, str] | None = None) -> dict[str, str]:
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    res: dict[str, str] = {}
    known = dict(inherited or {})
    i = 0
    n = len(lines)

    while i < n:
        raw = lines[i].strip()
        i += 1
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].lstrip()
        if "=" not in raw:
            continue

        key, val = raw.split("=", 1)
        key = key.strip()
        val = val.lstrip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        if val and val[0] in ('"', "'"):
            quote_char = val[0]
            buf = [val[1:]]
            closed = False
            while not closed:
                cur = buf[-1]
                pos = 0
                found = -1
                while pos < len(cur):
                    if cur[pos] == '\\' and quote_char == '"':
                        pos += 2
                        continue
                    if cur[pos] == quote_char:
                        found = pos
                        break
                    pos += 1

                if found != -1:
                    buf[-1] = cur[:found]
                    closed = True
                else:
                    if i >= n:
                        break
                    buf.append(lines[i])
                    i += 1

            val_str = "".join(buf)
            if quote_char == '"':
                val_str = re.sub(
                    r'\\([\\abfnrtv"\'$])',
                    lambda m: {
                        'n': '\n', 't': '\t', 'r': '\r', '\\': '\\',
                        '"': '"', "'": "'", '$': '$'
                    }.get(m.group(1), m.group(1)),
                    val_str
                )
                val_str = _expand(val_str, known)
        else:
            val_str = re.split(r'\s+#', val, maxsplit=1)[0].rstrip()
            val_str = _expand(val_str, known)

        if val_str.startswith("file:"):
            fp = Path(os.path.expanduser(val_str[5:].strip()))
            if not fp.is_absolute():
                fp = path.parent / fp
            if fp.is_file():
                val_str = fp.read_text(encoding="utf-8").strip()

        res[key] = val_str
        known[key] = val_str

    return res

def resolve_key_line(raw_line: str, base_dir: Path) -> str:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return ""

    m = re.search(r"file:(\S+)", line)
    if m:
        f_path_str = m.group(1)
        f_path = Path(os.path.expanduser(f_path_str))
        if not f_path.is_absolute():
            cand = base_dir / f_path
            f_path = cand if cand.is_file() else Path.cwd() / f_path
        if not f_path.is_file():
            raise ConfigError(f"Public-Key-Datei nicht gefunden: {f_path}")
        pub_content = f_path.read_text(encoding="utf-8").strip()
        pub_key = ""
        for pl in pub_content.splitlines():
            pl = pl.strip()
            if pl and not pl.startswith("#"):
                pub_key = pl
                break
        if not pub_key:
            raise ConfigError(f"Kein gueltiger Public-Key in {f_path} gefunden.")
        line = line.replace(m.group(0), pub_key).strip()

    if not AUTH_LINE_RE.match(line):
        raise ConfigError(f"Ungueltige SSH-Key-Zeile: {short(line, 60)!r}")
    return line

_TPL = re.compile(r"\{\{(.*?)\}\}", re.S)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def render(text: str, lookup, where: str = "") -> str:
    if "{{" not in text:
        return text.replace("\\{{", "{{")
    text = text.replace("\\{{", "\x00LB\x00")
    missing: list[str] = []

    def rep(m: re.Match) -> str:
        inner = m.group(1).strip()
        quote = False
        if inner.endswith("|q"):
            quote, inner = True, inner[:-2].rstrip()
        default = None
        name = inner
        if ":-" in inner:
            name, default = inner.split(":-", 1)
            name = name.strip()
        if not _NAME.fullmatch(name):
            raise ConfigError(f"Ungueltiger Platzhalter {{{{{m.group(1)}}}}} in {where or 'Text'}")
        val = lookup(name)
        if val is None or val == "":
            if default is not None:
                val = default
            elif val is None:
                missing.append(name)
                return ""
        return shlex.quote(val) if quote else val

    out = _TPL.sub(rep, text)
    if missing:
        raise ConfigError(f"Unbekannte Variable(n) {', '.join(sorted(set(missing)))} in {where or 'Text'}")
    return out.replace("\x00LB\x00", "{{")

RECIPE_KEYS = {"name", "version", "description", "requires"}
VAR_KEYS = {"default", "required", "secret", "description"}
COND = {"if_var", "unless_var"}
PREFLIGHT_KEYS = {"name", "run", "level", "hint", "sudo"} | COND
USER_KEYS = {"name", "system", "home", "shell", "groups", "create_home", "lock_password", "comment",
             "remove_on_uninstall", "description"} | COND
FILE_KEYS = {"src", "content", "dest", "mode", "owner", "group", "template", "validate", "mkdir", "backup",
             "keep", "on_conflict", "description"} | COND
BLOCK_KEYS = {"dest", "src", "content", "id", "position", "comment", "owner", "group", "mode", "dir_mode",
              "validate", "description", "on_conflict"} | COND
AUTH_BLOB_RE = re.compile(r"(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-\S+|sk-\S+)\s+([A-Za-z0-9+/]{20,}=*)")

AUTH_KEYS = {"user", "keys", "key_files", "options", "command", "from", "id", "restrict", "ephemeral_test_key",
             "description"} | COND
REMOVE_KEYS = {"path", "on_conflict", "description"} | COND
STEP_KEYS = {"name", "run", "where", "phase", "if_cmd", "unless_cmd", "when_changed", "undo", "ignore_errors",
             "timeout", "sudo", "description"} | COND
TEST_KEYS = {"name", "kind", "run", "user", "key", "expect_rc", "expect", "expect_not", "expect_stderr", "timeout",
             "retries", "retry_delay", "sudo", "cleanup", "critical", "description"} | COND
TOP_KEYS = {"recipe", "vars", "preflight", "users", "files", "blocks", "authorized_keys", "remove", "steps",
            "cleanup", "uninstall", "tests"}
CONFLICT_VALUES = ("ask", "overwrite", "skip", "abort")

def _check_keys(d: dict, allowed: set[str], where: str) -> None:
    for k in d:
        if k not in allowed:
            hint = difflib.get_close_matches(k, sorted(allowed), n=1)
            raise ConfigError(f"{where}: Unbekannter Schluessel '{k}'" + (f" (meintest du '{hint[0]}'?)" if hint else ""))

@dataclass
class VarSpec:
    default: str | None = None
    required: bool = False
    secret: bool = False
    description: str = ""

@dataclass
class Recipe:
    path: Path
    dir: Path
    name: str
    version: str
    description: str
    requires: list[str]
    vars: dict[str, VarSpec]
    preflight: list[dict]
    users: list[dict]
    files: list[dict]
    blocks: list[dict]
    authorized_keys: list[dict]
    remove: list[dict]
    steps: list[dict]
    cleanup: list[dict]
    uninstall: list[dict]
    tests: list[dict]

def _section(raw: dict, name: str, path: Path) -> list[dict]:
    v = raw.get(name, [])
    if not isinstance(v, list) or any(not isinstance(x, dict) for x in v):
        raise ConfigError(f"{path}: [[{name}]] muss eine Liste von Tabellen sein")
    return v

def validate_recipe(path: Path, raw: dict) -> Recipe:
    _check_keys(raw, TOP_KEYS, str(path))
    r = raw.get("recipe", {})
    _check_keys(r, RECIPE_KEYS, f"{path} [recipe]")
    name = r.get("name") or path.parent.name
    if not NAME_RE.match(name):
        raise ConfigError(f"{path}: Ungueltiger Rezeptname {name!r}")

    vars_: dict[str, VarSpec] = {}
    for k, v in raw.get("vars", {}).items():
        if not _NAME.fullmatch(k):
            raise ConfigError(f"{path} [vars]: Ungueltiger Variablenname {k!r}")
        if isinstance(v, dict):
            _check_keys(v, VAR_KEYS, f"{path} [vars.{k}]")
            d = v.get("default")
            vars_[k] = VarSpec(None if d is None else str(d), bool(v.get("required", False)),
                               bool(v.get("secret", False)), str(v.get("description", "")))
        else:
            vars_[k] = VarSpec(str(v))

    sec = {n: _section(raw, n, path) for n in (
        "preflight", "users", "files", "blocks", "authorized_keys", "remove",
        "steps", "cleanup", "uninstall", "tests"
    )}

    for i, p in enumerate(sec["preflight"], 1):
        w = f"{path} [[preflight]] #{i}"
        _check_keys(p, PREFLIGHT_KEYS, w)
        if "run" not in p:
            raise ConfigError(f"{w}: 'run' fehlt")

    for i, u in enumerate(sec["users"], 1):
        w = f"{path} [[users]] #{i}"
        _check_keys(u, USER_KEYS, w)
        if not USER_RE.match(str(u.get("name", ""))) and "{{" not in str(u.get("name", "")):
            raise ConfigError(f"{w}: Ungueltiger Benutzername {u.get('name')!r}")

    for i, f in enumerate(sec["files"], 1):
        w = f"{path} [[files]] #{i}"
        _check_keys(f, FILE_KEYS, w)
        if "dest" not in f:
            raise ConfigError(f"{w}: 'dest' fehlt")
        if ("src" in f) == ("content" in f):
            raise ConfigError(f"{w} ({f['dest']}): Genau eines von 'src' oder 'content' angeben")
        norm_mode(f.get("mode", "0644")) if "{{" not in str(f.get("mode", "")) else None

    for i, b in enumerate(sec["blocks"], 1):
        w = f"{path} [[blocks]] #{i}"
        _check_keys(b, BLOCK_KEYS, w)
        if "dest" not in b or "id" not in b:
            raise ConfigError(f"{w}: 'dest' und 'id' sind Pflicht")

    for i, a in enumerate(sec["authorized_keys"], 1):
        w = f"{path} [[authorized_keys]] #{i}"
        _check_keys(a, AUTH_KEYS, w)
        if "user" not in a:
            raise ConfigError(f"{w}: 'user' fehlt")

    for i, x in enumerate(sec["remove"], 1):
        w = f"{path} [[remove]] #{i}"
        _check_keys(x, REMOVE_KEYS, w)
        if "path" not in x:
            raise ConfigError(f"{w}: 'path' fehlt")

    for sect in ("steps", "cleanup", "uninstall"):
        for i, s in enumerate(sec[sect], 1):
            w = f"{path} [[{sect}]] #{i}"
            _check_keys(s, STEP_KEYS, w)
            if "run" not in s:
                raise ConfigError(f"{w}: 'run' fehlt")

    for i, t in enumerate(sec["tests"], 1):
        w = f"{path} [[tests]] #{i}"
        _check_keys(t, TEST_KEYS, w)
        if "run" not in t or "name" not in t:
            raise ConfigError(f"{w}: 'name' und 'run' sind Pflicht")

    return Recipe(
        path, path.parent, name, str(r.get("version", "")), str(r.get("description", "")),
        list(r.get("requires", [])), vars_, sec["preflight"], sec["users"], sec["files"],
        sec["blocks"], sec["authorized_keys"], sec["remove"], sec["steps"], sec["cleanup"],
        sec["uninstall"], sec["tests"]
    )

def load_recipe(path: Path) -> Recipe:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML-Syntaxfehler: {e}") from None
    return validate_recipe(path, raw)

def find_recipe(spec: str) -> Path:
    cands: list[Path] = []
    p = Path(os.path.expanduser(spec))
    cands += [p, p / "recipe.toml"]
    for base in [Path.cwd() / "recipes", SCRIPT_DIR / "recipes"] + [
        Path(x) for x in os.environ.get("DEPLOY_RECIPE_PATH", "").split(":") if x
    ]:
        cands += [base / spec / "recipe.toml", base / spec]
    for c in cands:
        if c.is_file() and c.suffix == ".toml":
            return c.resolve()
    raise ConfigError(f"Rezept {spec!r} nicht gefunden.")

def push_overrides(args: argparse.Namespace, S: dict[str, str]) -> dict[str, str]:
    """Werte fuer recipes/push. Nur was auf der Kommandozeile steht - kein Profil-Default."""
    src_path_str = getattr(args, "file", None) or S.get("PUSH_SRC") or ""
    if not src_path_str:
        raise ConfigError("push: Keine Quelldatei angegeben (Datei als Argument oder PUSH_SRC)")
    f = Path(os.path.expanduser(src_path_str)).resolve()
    if not f.is_file():
        raise ConfigError(f"Quelldatei nicht gefunden: {f}")

    dest = getattr(args, "dest", None) or S.get("PUSH_DEST") or ("/usr/local/bin/" + re.sub(r"\.(sh|py|pl|rb)$", "", f.name))
    mode = getattr(args, "mode", None) or S.get("PUSH_MODE") or "0755"
    owner = getattr(args, "owner", None) or S.get("PUSH_OWNER") or "root"
    group = getattr(args, "group", None) or S.get("PUSH_GROUP") or "root"

    validate = getattr(args, "validate", None) or S.get("PUSH_VALIDATE")
    if validate is None:
        head = f.read_bytes()[:200].decode("utf-8", "replace").split("\n", 1)[0]
        if "bash" in head or f.suffix == ".sh":
            validate = "bash -n {{FILE}}"
        elif "python" in head or f.suffix == ".py":
            validate = "python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' {{FILE}}"
        else:
            validate = ""

    out = {
        "PUSH_SRC": str(f), "PUSH_DEST": dest, "PUSH_MODE": norm_mode(mode),
        "PUSH_OWNER": owner, "PUSH_GROUP": group, "PUSH_VALIDATE": validate,
    }

    user = getattr(args, "system_user", None)
    if user:
        if not USER_RE.match(user):
            raise ConfigError(f"Ungueltiger Systembenutzer: {user!r}")
        out["PUSH_USER"] = user
        out["PUSH_USER_GROUPS"] = " ".join(split_words(S.get("SYSTEM_USER_GROUPS") or "sshallow"))

    sudoers = getattr(args, "sudoers", None)
    if sudoers:
        s_name = getattr(args, "sudoers_name", None) or "push"
        if not SUDOERS_NAME_RE.match(s_name):
            raise ConfigError(f"SUDOERS_NAME {s_name!r} ungueltig (keine Punkte oder Sonderzeichen erlaubt).")
        out["PUSH_SUDOERS"] = sudoers.strip()
        out["PUSH_SUDOERS_NAME"] = s_name

    keys = getattr(args, "authorized_keys", None)
    if keys:
        if not user:
            raise ConfigError("push: --authorized-keys braucht --system-user")
        out["PUSH_KEYS"] = keys

    whitelist = getattr(args, "whitelist_ip", None)
    if whitelist:
        for part in split_words(whitelist):
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError:
                raise ConfigError(f"WHITELIST_IP {part!r} ist keine gueltige IP oder CIDR-Range.")
        out["PUSH_WHITELIST_IP"] = whitelist

    tests = getattr(args, "test", None) or []
    if tests and not getattr(args, "no_test", False):
        out["PUSH_TEST_CMD"] = "\n".join(tests)

    return out

@dataclass
class Target:
    name: str
    host: str
    user: str
    port: int
    key: str = ""
    local: bool = False
    vars: dict[str, str] = field(default_factory=dict)

_TGT_RE = re.compile(r"^(?:(?P<user>[^@\s]+)@)?(?P<host>\[[0-9a-fA-F:.]+\]|[^:\s]+)(?::(?P<port>\d+))?$")

def load_inventory(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        inv = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Inventar {path}: {e}") from None
    return inv

def resolve_targets(spec: str, S: dict[str, str], inv: dict) -> list[Target]:
    hosts: dict = inv.get("hosts", {})
    dflt: dict = inv.get("defaults", {})
    groups: dict = {k: list(v) for k, v in inv.get("groups", {}).items()}
    for alias, h in hosts.items():
        for g in h.get("groups", []):
            groups.setdefault(g, []).append(alias)
    if "all" not in groups and hosts:
        groups["all"] = list(hosts)

    def expand(token: str, seen: tuple[str, ...] = ()) -> list[str]:
        if token.startswith("@"):
            g = token[1:]
            if g not in groups:
                raise ConfigError(f"Gruppe @{g} nicht im Inventar vorhanden.")
            if g in seen:
                raise ConfigError(f"Gruppen-Schleife bei @{g}")
            out: list[str] = []
            for t in groups[g]:
                out += expand(t, seen + (g,))
            return out
        return [token]

    names: list[str] = []
    for tok in split_words(spec):
        for n in expand(tok):
            if n not in names:
                names.append(n)

    targets: list[Target] = []
    for n in names:
        if n == "local":
            targets.append(Target("local", "localhost", "", 0, local=True))
            continue
        if n in hosts:
            h = hosts[n]
            vs = {k: str(v) for k, v in dflt.get("vars", {}).items()}
            vs.update({k: str(v) for k, v in h.get("vars", {}).items()})
            targets.append(Target(
                n, h.get("host", n), h.get("user", dflt.get("user", S["SSH_USER"])),
                int(h.get("port", dflt.get("port", S["SSH_PORT"]))),
                os.path.expanduser(h.get("key", dflt.get("key", S["SSH_KEY"]))),
                vars=vs
            ))
            continue
        m = _TGT_RE.match(n)
        if not m:
            raise ConfigError(f"Ziel {n!r} ungueltig (Format: [user@]host[:port] oder @gruppe)")
        targets.append(Target(
            n, m.group("host"), m.group("user") or dflt.get("user", S["SSH_USER"]),
            int(m.group("port") or dflt.get("port", S["SSH_PORT"])),
            os.path.expanduser(dflt.get("key", S["SSH_KEY"])),
            vars={k: str(v) for k, v in dflt.get("vars", {}).items()}
        ))
    return targets

@dataclass
class Proc:
    rc: int
    out: str = ""
    err: str = ""
    timed_out: bool = False

PRELUDE = 'export LC_ALL=C LANG=C\nexport PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"\n'

def ssh_hint(err: str) -> str:
    e = err.lower()
    table = [
        ("permission denied", "SSH-Authentifizierung abgelehnt (Key, User, AllowUsers oder AllowGroups pruefen)."),
        ("timed out", "Verbindungs-Timeout (Host unerreichbar, Firewall oder falscher Port)."),
        ("connection refused", "Verbindung abgewiesen (sshd laeuft nicht oder Port falsch)."),
        ("could not resolve", "DNS-Aufloesung fuer Hostname fehlgeschlagen."),
        ("host key verification failed", "Host-Key stimmt nicht mit known_hosts ueberein."),
        ("remote host identification has changed", "Host-Key hat sich geaendert."),
        ("a password is required", "sudo verlangt Passwort (NOPASSWD einrichten)."),
        ("is not in the sudoers", "SSH-Benutzer besitzt keine sudo-Rechte.")
    ]
    for needle, text in table:
        if needle in e:
            return text
    return ""

def _run(argv: list[str], stdin: bytes | None, timeout: int, env: dict | None = None, cwd: str | None = None) -> Proc:
    kw: dict = {"capture_output": True, "timeout": timeout, "env": env, "cwd": cwd}
    kw["stdin"] = subprocess.DEVNULL if stdin is None else None
    kw["input"] = stdin
    try:
        p = subprocess.run(argv, **kw)
        return Proc(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired as e:
        return Proc(
            124,
            (e.stdout or b"").decode("utf-8", "replace"),
            ((e.stderr or b"").decode("utf-8", "replace") + f"\nTimeout nach {timeout}s"),
            timed_out=True
        )
    except FileNotFoundError as e:
        return Proc(127, "", f"Programm nicht gefunden: {e.filename}")

class Transport:
    needs_sudo = False
    def exec(self, script: str, *, sudo: bool = True, timeout: int = 120) -> Proc:
        raise NotImplementedError
    def exec_cmd(self, cmd: str, *, stdin: bytes | None = None, timeout: int = 120) -> Proc:
        raise NotImplementedError
    def close(self) -> None:
        pass

class LocalTransport(Transport):
    def __init__(self, S: dict[str, str]):
        mode = S["SUDO"]
        self.needs_sudo = mode == "always" or (mode == "auto" and os.geteuid() != 0)

    def exec(self, script, *, sudo=True, timeout=120):
        fd, path = tempfile.mkstemp(prefix=".dpl-sh.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(script)
            argv = ["bash", path]
            if sudo and self.needs_sudo:
                argv = ["sudo", "-n"] + argv
            return _run(argv, None, timeout)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    def exec_cmd(self, cmd, *, stdin=None, timeout=120):
        return _run(["bash", "-c", cmd], stdin, timeout)

class SSHTransport(Transport):
    def __init__(self, t: Target, S: dict[str, str], ctl_dir: str):
        self.t, self.S, self.ctl = t, S, ctl_dir
        mode = S["SUDO"]
        self.needs_sudo = mode == "always" or (mode == "auto" and t.user != "root")

    def base(self, *, user: str | None = None, key: str | None = None, mux: bool = True) -> list[str]:
        S = self.S
        a = [
            "ssh", "-T", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={S['CONNECT_TIMEOUT']}",
            "-o", f"StrictHostKeyChecking={S['SSH_STRICT']}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "LogLevel=ERROR", "-p", str(self.t.port)
        ]
        if mux:
            a += ["-o", "ControlMaster=auto", "-o", f"ControlPath={self.ctl}/%C", "-o", "ControlPersist=120"]
        else:
            a += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]

        k = key if key is not None else self.t.key
        if k:
            a += ["-i", os.path.expanduser(k), "-o", "IdentitiesOnly=yes"]
        if S["SSH_OPTS"]:
            a += shlex.split(S["SSH_OPTS"])
        a.append(f"{user or self.t.user}@{self.t.host}")
        return a

    def exec(self, script, *, sudo=True, timeout=120):
        run = 'sudo -n bash "$f"' if (sudo and self.needs_sudo) else 'bash "$f"'
        inner = f'f=$(mktemp /tmp/.dpl-sh.XXXXXXXX) || exit 97; cat > "$f"; {run} </dev/null; rc=$?; rm -f "$f"; exit $rc'
        return _run(self.base() + ["sh -c " + q(inner)], script.encode(), timeout)

    def exec_cmd(self, cmd, *, stdin=None, timeout=120):
        return _run(self.base() + [cmd], stdin, timeout)

    def raw(self, user: str, key: str, command: str, timeout: int) -> Proc:
        return _run(self.base(user=user, key=key, mux=False) + [command], None, timeout)

    def close(self):
        with contextlib.suppress(Exception):
            subprocess.run(self.base() + ["-O", "exit"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)

class Ephemeral:
    _lock = threading.Lock()
    _dir: str | None = None
    comment = ""

    @classmethod
    def ensure(cls, run_id: str) -> tuple[str, str]:
        with cls._lock:
            if cls._dir is None:
                d = tempfile.mkdtemp(prefix="dpl-key-")
                os.chmod(d, 0o700)
                cls.comment = f"deploy-test-ephemeral-{run_id}"
                p = subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", cls.comment, "-f", f"{d}/id"],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10
                )
                if p.returncode != 0:
                    shutil.rmtree(d, ignore_errors=True)
                    raise ConfigError(f"ssh-keygen fehlgeschlagen: {p.stderr.strip() or p.stdout.strip()}")
                cls._dir = d
            return f"{cls._dir}/id", Path(f"{cls._dir}/id.pub").read_text().strip()

    @classmethod
    def cleanup(cls):
        if cls._dir:
            shutil.rmtree(cls._dir, ignore_errors=True)
            cls._dir = None

class ProbeTransport(Transport):
    """Transport ohne Aktionen: baut Rezepte lokal auf, ohne einen Server zu beruehren."""
    needs_sudo = False

    def exec(self, script: str, *, sudo: bool = True, timeout: int = 120) -> Proc:
        return Proc(1, "", "probe")

    def exec_cmd(self, cmd: str, *, stdin: bytes | None = None, timeout: int = 120) -> Proc:
        return Proc(1, "", "probe")

@dataclass
class Cfg:
    S: dict[str, str]
    sources: dict[str, str]
    recipe: Recipe | None
    secrets: list[str]
    run_id: str
    workdir: str
    ctl_dir: str
    n_targets: int
    target_names: list[str]

class HostLog:
    def __init__(self, name: str, live: bool, verbose: bool, secrets: list[str]):
        self.name, self.live, self.verbose, self.secrets = name, live, verbose, secrets
        self.lines: list[str] = []
        self.events: list[dict] = []
        self.counts = {"chg": 0, "ok": 0, "fail": 0, "test": "SKIP"}
        self.changed_items: list[str] = []

    def mask(self, s: str) -> str:
        for sec in self.secrets:
            if sec:
                s = s.replace(sec, "***")
        return s

    def log(self, tag: str, msg: str, detail: str = "") -> None:
        msg = self.mask(msg)
        self.events.append({"tag": tag, "msg": msg})

        if tag in ("CHG", "DRY"):
            self.counts["chg"] += 1
            short_item = msg.split(":")[0].replace("neu angelegt", "").strip()
            if "/usr/local/bin/" in short_item:
                short_item = Path(short_item).name
            elif "/etc/sudoers.d/" in short_item:
                short_item = "sudoers:" + Path(short_item).name
            elif "authorized_keys" in short_item:
                short_item = "authorized_keys"
            elif "Benutzer" in short_item:
                short_item = "user:" + short_item.replace("Benutzer", "").strip()
            elif "Whitelist" in short_item:
                short_item = "whitelist"

            if short_item and short_item not in self.changed_items:
                self.changed_items.append(short_item)

        elif tag in ("OK", "PASS"):
            self.counts["ok"] += 1
        elif tag == "FAIL":
            self.counts["fail"] += 1

        if "Test:" in msg:
            self.counts["test"] = ("PASS" if tag == "PASS" else "SKIP" if tag == "SKIP"
                                  else "WARN" if tag == "WARN" else "FAIL")

        line = f"[{UI.paint(tag, f'{tag:<4}')}] {self.name:<24} | {msg}"
        if detail:
            for dl in self.mask(detail).strip().splitlines()[-10:]:
                line += f"\n        | {dl}"
        if self.live:
            UI.emit(line)
        else:
            self.lines.append(line)

    def flush(self) -> None:
        if self.lines:
            UI.emit("\n".join(self.lines))
            self.lines.clear()

@dataclass
class Item:
    kind: str
    idx: int
    label: str
    spec: dict
    status: str = "?"
    conflict: dict | None = None
    decision: str = ""
    data: bytes = b""
    sha: str = ""
    mode: str = "644"
    owner: str = "root"
    group: str = "root"
    remote_sha: str = ""
    remote_meta: str = ""
    reg_recipe: str = ""
    reg_sha: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def cid(self) -> str:
        return f"{self.kind}:{self.label}"

def reg_file(state_dir: str, dest: str) -> str:
    return f"{state_dir.rstrip('/')}/_registry/files/{hashlib.sha1(dest.encode()).hexdigest()[:16]}"

BLOCK_COMPOSE = r'''
dest=@DEST@; out=@OUT@; BEGIN=@BEGIN@; END=@END@; pos=@POS@
base="$out.base"; blk="$out.blk"
if [ -f "$dest" ]; then awk -v b="$BEGIN" -v e="$END" '$0==b{s=1;next} $0==e{s=0;next} !s' "$dest" > "$base"; else : > "$base"; fi
if [ -s "$base" ] && [ -n "$(tail -c1 "$base")" ]; then echo >> "$base"; fi
@MAKEBLK@
if [ "$pos" = top ]; then cat "$blk" "$base" > "$out"; else cat "$base" "$blk" > "$out"; fi
rm -f "$base" "$blk"
'''

def compose_script(dest: str, out: str, block_id: str, comment: str, position: str, content: str | None) -> str:
    begin = f"{comment} BEGIN deploy-tool:{block_id}"
    end = f"{comment} END deploy-tool:{block_id}"
    if content is None:
        make = ': > "$blk"'
    else:
        make = f'printf \'%s\\n%s\\n%s\\n\' "$BEGIN" "$(printf %s {q(b64(content))} | base64 -d)" "$END" > "$blk"'
    return (
        BLOCK_COMPOSE.replace("@DEST@", q(dest))
        .replace("@OUT@", out)
        .replace("@BEGIN@", q(begin))
        .replace("@END@", q(end))
        .replace("@POS@", q(position))
        .replace("@MAKEBLK@", make)
    )

def meta_script(dest: str, mode: str, owner: str, group: str) -> str:
    return (
        f'grp={q(group)}; [ -n "$grp" ] || grp=$(id -gn {q(owner)} 2>/dev/null || echo {q(owner)})\n'
        f'chmod {mode} {q(dest)}; chown {q(owner)}:"$grp" {q(dest)}\n'
    )

class HostRunner:
    def __init__(self, cfg: Cfg, target: Target, tr: Transport, log: HostLog, opts: argparse.Namespace):
        self.cfg, self.t, self.tr, self.log, self.opts = cfg, target, tr, log, opts
        self.recipe: Recipe = cfg.recipe  # type: ignore
        self.stage = ""
        self.changed_any = False
        self.ops: list[dict] = []
        self.run_dir = ""
        self.started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.timeout = int(cfg.S["TIMEOUT"])
        self.tests_enabled = not getattr(opts, "no_test", False)
        if self.recipe:
            missing = [k for k, spec in self.recipe.vars.items()
                       if spec.required and not truthy(self.lookup(k) or "")]
            if missing:
                raise ConfigError(
                    f"Pflichtwert(e) fehlen: {', '.join(missing)} "
                    f"(Profil, .env oder -s NAME=WERT)"
                )
        self.items: list[Item] = []
        self.homes: dict[str, str] = {}
        self.skipped: list[Item] = []
        self.skipped_users: set[str] = set()
        self.ok = False
        self.err = ""
        self.seconds = 0.0

    def builtins(self) -> dict[str, str]:
        S = self.cfg.S
        return {
            "TARGET": self.t.name, "TARGET_HOST": self.t.host, "TARGET_USER": self.t.user or "",
            "TARGET_PORT": str(self.t.port), "TARGET_COUNT": str(self.cfg.n_targets),
            "TARGET_NAMES": ",".join(self.cfg.target_names),
            "RUN_ID": self.cfg.run_id, "WORKDIR": self.cfg.workdir,
            "TESTS_ENABLED": "1" if self.tests_enabled else "0",
            "RECIPE_NAME": self.recipe.name if self.recipe else "",
            "RECIPE_DIR": str(self.recipe.dir) if self.recipe else "",
            "STATE_DIR": S["STATE_DIR"],
        }

    def lookup(self, name: str, extra: dict[str, str] | None = None) -> str | None:
        if extra and name in extra:
            return extra[name]
        b = self.builtins()
        if name in b:
            return b[name]
        if name in ("EPHEMERAL_KEY", "EPHEMERAL_PUBKEY"):
            priv, pub = Ephemeral.ensure(self.cfg.run_id)
            return priv if name == "EPHEMERAL_KEY" else pub
        if name in self.t.vars:
            return self.t.vars[name]
        if name in self.cfg.S:
            return self.cfg.S[name]
        spec = self.recipe.vars.get(name) if self.recipe else None
        return spec.default if spec is not None else None

    def R(self, text: str, where: str, extra: dict[str, str] | None = None) -> str:
        return render(text, lambda n: self.lookup(n, extra), where)

    def var_ok(self, spec: dict) -> bool:
        if "if_var" in spec and not truthy(self.lookup(spec["if_var"]) or ""):
            return False
        if "unless_var" in spec and truthy(self.lookup(spec["unless_var"]) or ""):
            return False
        return True

    @property
    def state(self) -> str:
        return self.cfg.S["STATE_DIR"].rstrip("/")

    @property
    def reg_dir(self) -> str:
        return f"{self.state}/_registry"

    def sh(self, script: str, *, sudo: bool = True, timeout: int | None = None, strict: bool = True) -> Proc:
        body = PRELUDE + ("set -eo pipefail\n" if strict else "") + script
        return self.tr.exec(body, sudo=sudo, timeout=timeout or self.timeout)

    def must(self, p: Proc, what: str) -> Proc:
        if p.rc != 0:
            hint = ssh_hint(p.err) if p.rc in (1, 124, 255) else ""
            detail = short(p.err or p.out, 600)
            raise StepFailed(
                f"{what} fehlgeschlagen (rc={p.rc})" + (f" - {hint}" if hint else "") +
                (f"\n{detail}" if detail else "")
            )
        return p

    def exec_local(self, script: str, timeout: int) -> Proc:
        env = dict(os.environ)
        env.update(self.builtins())
        for k in (self.recipe.vars if self.recipe else {}):
            v = self.lookup(k)
            if v is not None:
                env[k] = v
        return _run(["bash", "-c", "set -eo pipefail\n" + script], None, timeout, env=env, cwd=self.cfg.workdir)

    def connect(self) -> None:
        p = self.sh("echo DPL_OK; id -un; uname -sr", sudo=False, timeout=int(self.cfg.S["CONNECT_TIMEOUT"]) + 5)
        if p.rc != 0 or "DPL_OK" not in p.out:
            raise StepFailed(f"SSH-Verbindung fehlgeschlagen (rc={p.rc}) {ssh_hint(p.err)}\n{short(p.err, 300)}")
        lines = p.out.split("\n")
        who = lines[1].strip() if len(lines) > 1 else "?"
        sysinfo = lines[2].strip() if len(lines) > 2 else ""
        if self.tr.needs_sudo:
            qq = self.sh("id -u", sudo=True, timeout=10)
            if qq.rc != 0 or qq.out.strip() != "0":
                raise StepFailed("sudo -n funktioniert nicht (NOPASSWD noetig). " + ssh_hint(qq.err))
        self.log.log("PASS", f"Verbunden als {who} ({sysinfo})" + (" mit sudo" if self.tr.needs_sudo else ""))

    def preflight(self) -> bool:
        r = self.recipe
        ok = True
        if r.requires:
            p = self.sh(
                "m=''; for c in " + " ".join(q(c) for c in r.requires) +
                '; do command -v "$c" >/dev/null 2>&1 || m="$m $c"; done; echo "$m"',
                strict=False
            )
            miss = p.out.strip()
            if p.rc != 0 or miss:
                self.log.log("FAIL", f"Benoetigte Programme fehlen: {miss or '?'}")
                ok = False
            else:
                self.log.log("PASS", f"Programme verifiziert ({', '.join(r.requires)})")

        for pf in r.preflight:
            if not self.var_ok(pf):
                continue
            name = pf.get("name", pf["run"][:40])
            p = self.sh(self.R(pf["run"], f"preflight '{name}'"), sudo=pf.get("sudo", True), strict=False)
            hint = f" - {pf['hint']}" if pf.get("hint") else ""
            if p.rc == 0:
                self.log.log("PASS", f"Preflight: {name}")
            elif pf.get("level", "fatal") == "warn":
                self.log.log("WARN", f"Preflight: {name}{hint}", p.out + p.err)
            else:
                self.log.log("FAIL", f"Preflight: {name}{hint}", p.out + p.err)
                ok = False
        return ok

    def home_of(self, user: str) -> str:
        if user not in self.homes:
            p = self.sh(f"getent passwd -- {q(user)} | cut -d: -f6", strict=False)
            h = p.out.strip().splitlines()[0] if p.out.strip() else ""
            if not h:
                for u in self.recipe.users:
                    if self.R(u.get("name", ""), "user") == user and u.get("home"):
                        h = self.R(u["home"], "users.home")
                h = h or ("/root" if user == "root" else f"/home/{user}")
            self.homes[user] = h
        return self.homes[user]

    def key_lines(self, spec: dict, where: str) -> list[str]:
        lines: list[str] = []
        if spec.get("keys"):
            for raw_ln in self.R(spec["keys"], where + " keys").splitlines():
                resolved = resolve_key_line(raw_ln, self.recipe.dir)
                if resolved:
                    lines.append(resolved)

        kf = spec.get("key_files", [])
        for pat in ([kf] if isinstance(kf, str) else kf):
            pat = os.path.expanduser(self.R(pat, where + " key_files"))
            hits = sorted(globmod.glob(pat))
            if not hits:
                raise ConfigError(f"{where}: key_files {pat!r} findet keine Dateien.")
            for h in hits:
                for raw_ln in Path(h).read_text(encoding="utf-8").splitlines():
                    resolved = resolve_key_line(raw_ln, self.recipe.dir)
                    if resolved:
                        lines.append(resolved)

        seen: list[str] = []
        for ln in lines:
            if ln not in seen:
                seen.append(ln)
        return seen

    def auth_options(self, spec: dict, where: str, with_from: bool) -> str:
        opts: list[str] = []
        cmd = self.R(spec["command"], where + " command") if spec.get("command") else ""
        if cmd:
            opts.append('command="' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"')
        if spec.get("restrict", True) and cmd:
            opts += ["no-pty", "no-port-forwarding", "no-agent-forwarding", "no-X11-forwarding"]
        if spec.get("options"):
            extra = self.R(spec["options"], where + " options").strip()
            if extra:
                opts.append(extra)
        frm = self.R(spec["from"], where + " from").strip() if spec.get("from") else ""
        if frm and with_from:
            opts.append('from="' + frm.replace('"', "") + '"')
        return ",".join(opts)

    def auth_item(self, i: int, n: int, spec: dict, ephemeral: bool = False) -> Item | None:
        where = f"authorized_keys #{i + 1}"
        user = self.R(spec["user"], where + " user")
        if not USER_RE.match(user):
            raise ConfigError(f"{where}: Ungueltiger Benutzername {user!r}")
        bid = f"{self.recipe.name}:{spec.get('id', 'keys')}"

        if ephemeral:
            if not spec.get("ephemeral_test_key"):
                return None
            _, pub = Ephemeral.ensure(self.cfg.run_id)
            opts = self.auth_options(spec, where, with_from=False)
            lines = [(opts + " " if opts else "") + pub]
            bid += "-test"
        else:
            keys = self.key_lines(spec, where)
            if not keys:
                return None
            opts = self.auth_options(spec, where, with_from=True)
            lines = []
            for k in keys:
                if opts and not AUTH_LINE_RE.match(k).group("options"):
                    lines.append(opts + " " + k)
                else:
                    lines.append(k)

        dest = f"{self.home_of(user)}/.ssh/authorized_keys"
        it = Item("block", n, dest, spec)
        it.data = ("\n".join(lines)).encode()
        needle = ""
        if not ephemeral:
            for ln in lines:
                m = AUTH_BLOB_RE.search(ln)
                if m:
                    needle = m.group(1)
                    break
        it.extra = {
            "id": bid, "comment": "#", "position": "bottom", "owner": user, "group": "",
            "mode": "600", "dir_mode": "700", "enforce": True, "validate": "",
            "user": user, "auth": True, "key_needle": needle,
            "title": f"authorized_keys fuer {user} ({len(lines)} Key(s))"
        }
        return it

    def src_path(self, spec: str, where: str) -> Path:
        p = Path(os.path.expanduser(self.R(spec, where)))
        if p.is_absolute():
            return p
        cand = self.recipe.dir / p
        return cand if cand.exists() else Path.cwd() / p

    def build_items(self) -> list[Item]:
        r = self.recipe
        items: list[Item] = []
        n = 0
        for i, u in enumerate(r.users):
            if not self.var_ok(u):
                continue
            name = self.R(u["name"], f"users[{i}].name")
            if not USER_RE.match(name):
                raise ConfigError(f"users[{i}]: Ungueltiger Benutzername {name!r}")
            it = Item("user", n, name, u)
            it.extra = {
                "home": self.R(u.get("home", f"/home/{name}"), "users.home"),
                "shell": self.R(u.get("shell", "/bin/bash"), "users.shell"),
                "groups": [self.R(g, "users.groups") for g in u.get("groups", [])],
                "system": bool(u.get("system", True)),
                "create_home": bool(u.get("create_home", True)),
                "lock": bool(u.get("lock_password", True)),
                "comment": self.R(u.get("comment", f"deploy-tool-{r.name}"), "users.comment").replace(":", " ")
            }
            items.append(it)
            n += 1

        for i, f in enumerate(r.files):
            if not self.var_ok(f):
                continue
            dest = self.R(f["dest"], f"files[{i}].dest")
            if not dest.startswith("/"):
                raise ConfigError(f"files[{i}]: dest muss absolut sein: {dest}")
            if "src" in f:
                src = self.src_path(f["src"], f"files[{i}].src")
                if not src.is_file():
                    raise ConfigError(f"files[{i}]: Quelldatei fehlt: {src}")
                data = src.read_bytes()
            else:
                data = f["content"].encode()
            if f.get("template", False) or "content" in f:
                data = self.R(data.decode("utf-8"), f"files[{i}] ({dest})").encode()
            it = Item(
                "file", n, dest, f, data=data, sha=sha256(data),
                mode=norm_mode(self.R(str(f.get("mode", "0644")), f"files[{i}].mode")),
                owner=self.R(f.get("owner", "root"), "owner"),
                group=self.R(f.get("group", "root"), "group")
            )
            items.append(it)
            n += 1

        for i, b in enumerate(r.blocks):
            if not self.var_ok(b):
                continue
            dest = self.R(b["dest"], f"blocks[{i}].dest")
            if "src" in b:
                src = self.src_path(b["src"], f"blocks[{i}].src")
                text = src.read_text(encoding="utf-8")
            else:
                text = b["content"]
            text = self.R(text, f"blocks[{i}] ({dest})")
            owner = self.R(b["owner"], "owner") if b.get("owner") else ""
            it = Item("block", n, dest, b, data=text.rstrip("\n").encode())
            it.extra = {
                "id": f"{r.name}:{b['id']}", "comment": b.get("comment", "#"),
                "position": b.get("position", "bottom"), "owner": owner or "root",
                "group": self.R(b.get("group", ""), "group"),
                "mode": norm_mode(b.get("mode", "0644")),
                "dir_mode": norm_mode(b.get("dir_mode", "0755")),
                "enforce": bool(b.get("owner") or b.get("group") or b.get("mode")),
                "validate": b.get("validate", ""), "title": f"Block '{b['id']}' in {dest}"
            }
            items.append(it)
            n += 1

        for i, a in enumerate(r.authorized_keys):
            if not self.var_ok(a):
                continue
            it = self.auth_item(i, n, a)
            if it:
                items.append(it)
                n += 1

        for i, x in enumerate(r.remove):
            if not self.var_ok(x):
                continue
            p = self.R(x["path"], f"remove[{i}].path")
            items.append(Item("remove", n, p, x))
            n += 1

        return items

    def stat_items(self, items: list[Item]) -> None:
        if not items:
            return
        parts = []
        rdir = self.reg_dir
        for it in items:
            i = it.idx
            if it.kind in ("file", "remove"):
                rf = reg_file(self.state, it.label)
                parts.append(
                    f'f={q(it.label)}; r={q(rf)}; reg=""\n'
                    f'if [ -f "$r" ]; then reg="$(sed -n \'s/^recipe=//p\' "$r" | head -1) $(sed -n \'s/^sha=//p\' "$r" | head -1)"; fi\n'
                    f'if [ -L "$f" ]; then echo "{i} SYMLINK"\n'
                    f'elif [ -d "$f" ]; then echo "{i} DIR"\n'
                    f'elif [ -e "$f" ]; then echo "{i} FILE $(sha256sum "$f" | cut -d" " -f1) $(stat -c "%a %U %G" "$f") REG $reg"\n'
                    f'else echo "{i} MISSING REG $reg"; fi'
                )
            elif it.kind == "block":
                x = it.extra
                parts.append(
                    f'd={q(it.label)}; B={q(x["comment"] + " BEGIN deploy-tool:" + x["id"])}; E={q(x["comment"] + " END deploy-tool:" + x["id"])}; K={q(x.get("key_needle", ""))}\n'
                    f'if [ -L "$d" ]; then echo "{i} SYMLINK"\n'
                    f'elif [ -f "$d" ]; then has=0; grep -qxF -- "$B" "$d" && has=1; kp=0\n'
                    f'  if [ -n "$K" ] && grep -qF -- "$K" "$d"; then kp=1; fi\n'
                    f'  echo "{i} FILE $(stat -c "%a %U %G" "$d") $has $kp $(awk -v b="$B" -v e="$E" \'$0==b{{s=1;next}} $0==e{{s=0;next}} s\' "$d" | base64 -w0)"\n'
                    f'else echo "{i} MISSING"; fi'
                )
            elif it.kind == "user":
                nm = it.label
                parts.append(
                    f'if id -u -- {q(nm)} >/dev/null 2>&1; then e=EXISTS; else e=MISSING; fi\n'
                    f'cb=$(cat {q(rdir + "/users/" + nm + ".created_by")} 2>/dev/null || true)\n'
                    f'mine=0; [ -e {q(rdir + "/users/" + nm + ".used." + self.recipe.name)} ] && mine=1\n'
                    f'echo "{i} USER $e ${{cb:--}} $mine"'
                )

        pr = self.must(self.sh("\n".join(parts), strict=False), "Ist-Zustand erfassen")
        info: dict[int, list[str]] = {}
        for ln in pr.out.splitlines():
            tok = ln.split(" ")
            if tok and tok[0].isdigit():
                info[int(tok[0])] = tok[1:]

        force = getattr(self.opts, "force", False)
        for it in items:
            st = info.get(it.idx, ["MISSING"])
            if st[0] == "SYMLINK":
                raise StepFailed(f"{it.label} ist ein Symlink - Bearbeitung abgebrochen.")
            if st[0] == "DIR":
                raise StepFailed(f"{it.label} ist ein Verzeichnis, erwartet wird Datei.")
            getattr(self, f"stat_{it.kind}")(it, st, force)

    def stat_file(self, it: Item, st: list[str], force: bool) -> None:
        reg = st[st.index("REG") + 1:] if "REG" in st else []
        it.reg_recipe = reg[0] if len(reg) > 0 else ""
        it.reg_sha = reg[1] if len(reg) > 1 else ""
        me = self.recipe.name
        if st[0] == "MISSING":
            it.status = "create"
            return
        it.remote_sha = st[1]
        it.remote_meta = " ".join(st[2:5])
        mode, own, grp = st[2], st[3], st[4]
        meta_ok = int(mode, 8) == int(it.mode, 8) and own == it.owner and grp == it.group
        same = it.remote_sha == it.sha and not force
        if same:
            it.status = "ok" if meta_ok else "meta"
            if it.reg_recipe not in ("", me):
                it.conflict = {"kind": "other-recipe", "msg": f"Identische Datei gehoert zu Rezept '{it.reg_recipe}'."}
            return
        it.status = "update"
        if it.reg_recipe == me:
            if it.reg_sha and it.remote_sha != it.reg_sha:
                it.conflict = {"kind": "drift", "msg": "Datei wurde manuell geaendert (wird ueberschrieben, Backup angelegt)."}
        elif it.reg_recipe == "":
            it.conflict = {"kind": "unmanaged", "msg": "Existiert bereits, nicht von deploy.py angelegt (Backup angelegt)."}
        else:
            it.conflict = {"kind": "other-recipe", "msg": f"Gehoert zu Rezept '{it.reg_recipe}' (Backup angelegt)."}

    def stat_remove(self, it: Item, st: list[str], force: bool) -> None:
        reg = st[st.index("REG") + 1:] if "REG" in st else []
        it.reg_recipe = reg[0] if reg else ""
        if st[0] == "MISSING":
            it.status = "ok"
            return
        it.remote_sha = st[1]
        it.remote_meta = " ".join(st[2:5])
        it.status = "remove"
        if it.reg_recipe != self.recipe.name:
            it.conflict = {"kind": "remove-foreign", "msg": "Soll geloescht werden, stammt nicht von diesem Rezept (Backup angelegt)."}

    def stat_block(self, it: Item, st: list[str], force: bool) -> None:
        x = it.extra
        if st[0] == "MISSING":
            it.status = "create"
            it.extra["existed"] = False
            return
        mode, own, grp, has = st[1], st[2], st[3], st[4] == "1"
        already = len(st) > 5 and st[5] == "1"
        cur = base64.b64decode(st[6]).decode("utf-8", "replace").rstrip("\n") if len(st) > 6 and st[6] else ""
        it.extra.update({"existed": True, "had_block": has, "prev_b64": b64(cur) if has else None})
        it.remote_meta = f"{mode} {own} {grp}"
        if not has and already:
            # Schluessel steht schon in der Datei (handgepflegt): nicht doppelt eintragen.
            it.status = "ok"
            it.extra["adopted"] = True
        elif not has:
            it.status = "create"
        elif cur != it.data.decode("utf-8", "replace") or force:
            it.status = "update"
        else:
            it.status = "ok"
            if x["enforce"]:
                wgrp = x["group"]
                if int(mode, 8) != int(x["mode"], 8) or own != x["owner"] or (wgrp and grp != wgrp):
                    it.status = "meta"

    def stat_user(self, it: Item, st: list[str], force: bool) -> None:
        exists, created_by, mine = st[1] == "EXISTS", st[2], st[3] == "1"
        it.extra.update({"exists": exists, "created_by": "" if created_by == "-" else created_by, "mine": mine})
        if not exists:
            it.status = "create"
        elif mine:
            it.status = "ok"
        else:
            it.status = "exists"
            if not truthy(self.cfg.S.get("ALLOW_EXISTING_USER", "0")):
                who = f"angelegt von '{created_by}'" if created_by != "-" else "bereits auf dem System vorhanden"
                it.conflict = {"kind": "user-exists", "msg": f"Benutzer existiert ({who}). Wird nur mitbenutzt (keine Aenderung an Shell/Home)."}

    def analyze(self) -> bool:
        self.connect()
        if not self.preflight():
            return False
        self.items = self.build_items()
        self.stat_items(self.items)
        return True

    @property
    def conflicts(self) -> list[Item]:
        return [it for it in self.items if it.conflict]

    def stage_and_validate(self) -> None:
        todo = [
            i for i in self.items if i.kind in ("file", "block") and i.status in ("create", "update")
            and (not i.conflict or i.decision == "overwrite")
        ]
        if not todo:
            return

        p = self.must(self.tr.exec_cmd("mktemp -d /tmp/dpl.XXXXXXXX", timeout=30), "Tempdir erzeugen")
        self.stage = p.out.strip()
        s = q(self.stage)

        for it in todo:
            if it.kind == "file":
                up = self.must(
                    self.tr.exec_cmd(
                        f"umask 077; cat > {s}/u{it.idx} && sha256sum {s}/u{it.idx}",
                        stdin=it.data, timeout=self.timeout
                    ),
                    f"Upload {it.label}"
                )
                if up.out.split()[0] != it.sha:
                    raise StepFailed(f"Pruefsummenfehler beim Upload von {it.label}")

                script = f"install -m {it.mode} -o root -g root {s}/u{it.idx} {s}/v{it.idx}\n"
                if it.spec.get("validate"):
                    # Zweimal rendern: erst {{FILE}} einsetzen, dann Platzhalter aufloesen,
                    # die selbst erst durch eine Variable entstanden sind (z. B. PUSH_VALIDATE).
                    fpath = f"{self.stage}/v{it.idx}"
                    vcmd = self.R(it.spec["validate"], f"files[{it.idx}].validate", {"FILE": fpath})
                    script += self.R(vcmd, f"files[{it.idx}].validate", {"FILE": fpath}) + "\n"
                what = it.label
            else:
                x = it.extra
                script = compose_script(
                    it.label, f"{self.stage}/v{it.idx}", x["id"], x["comment"],
                    x["position"], it.data.decode("utf-8")
                ) + f'chmod 600 {s}/v{it.idx}\n'
                if x["validate"]:
                    script += self.R(x["validate"], f"block {x['id']} validate", {"FILE": f"{self.stage}/v{it.idx}"}) + "\n"
                what = x["title"]

            v = self.sh(script)
            if v.rc != 0:
                raise StepFailed(f"Validierung von {what} fehlgeschlagen (rc={v.rc}):\n{short(v.err or v.out, 600)}")
            self.log.log("PASS", f"Validiert: {what}" if it.spec.get("validate") or it.extra.get("validate") else f"Bereitgestellt: {what}")

    def runs_root(self) -> str:
        return f"{self.state}/{self.recipe.name}/runs"

    def init_run(self) -> None:
        self.run_dir = f"{self.runs_root()}/{self.cfg.run_id}"
        self.must(
            self.sh(f"mkdir -p -m 700 {q(self.run_dir)}/files {q(self.reg_dir)}/files {q(self.reg_dir)}/users"),
            "Laufverzeichnis initialisieren"
        )
        self.save_manifest("partial")

    @contextlib.contextmanager
    def lock(self):
        lk = f"{self.state}/{self.recipe.name}/lock"
        script = (
            f'mkdir -p {q(self.state + "/" + self.recipe.name)}\n'
            f'if ! mkdir {q(lk)} 2>/dev/null; then\n'
            f'  age=$(( $(date +%s) - $(stat -c %Y {q(lk)}) ))\n'
            f'  if [ "$age" -gt 3600 ]; then rm -rf {q(lk)}; mkdir {q(lk)}; '
            f'else echo "gesperrt: $(cat {q(lk)}/info 2>/dev/null)"; exit 3; fi\n'
            f'fi\necho "run={self.cfg.run_id} von={os.environ.get("USER", "?")}@{os.uname().nodename}" > {q(lk)}/info'
        )
        p = self.sh(script, strict=False)
        if p.rc != 0:
            raise StepFailed(f"Host ist durch einen anderen Lauf gesperrt: {short(p.out + p.err, 200)}")
        try:
            yield
        finally:
            self.sh(f"rm -rf {q(lk)}", strict=False, timeout=10)

    def save_manifest(self, status: str) -> None:
        man = {
            "recipe": self.recipe.name, "version": self.recipe.version, "run_id": self.cfg.run_id,
            "host": self.t.host, "started": self.started, "status": status, "ops": self.ops
        }
        js = b64(json.dumps(man, separators=(",", ":")))
        rd = q(self.run_dir)
        self.must(
            self.sh(f"printf %s {q(js)} | base64 -d > {rd}/manifest.json.new && mv -f {rd}/manifest.json.new {rd}/manifest.json"),
            "Manifest schreiben"
        )

    def record(self, op: dict) -> dict:
        self.ops.append(op)
        self.save_manifest("partial")
        return op

    def reg_write_script(self, it: Item) -> str:
        rf = reg_file(self.state, it.label)
        body = f"recipe={self.recipe.name}\nsha={it.sha}\nrun={self.cfg.run_id}\ndest={it.label}\n"
        return f'mkdir -p {q(self.reg_dir + "/files")}; printf %s {q(b64(body))} | base64 -d > {q(rf)}\n'

    def reg_before(self, it: Item) -> str:
        rf = reg_file(self.state, it.label)
        p = self.sh(f'[ -f {q(rf)} ] && base64 -w0 {q(rf)} || true', strict=False)
        return p.out.strip()

    def apply_user(self, it: Item) -> None:
        x = it.extra
        nm, rd = it.label, self.reg_dir
        used = f"{rd}/users/{nm}.used.{self.recipe.name}"
        if it.status == "create":
            cmd = ["useradd"]
            if x["system"]:
                cmd.append("--system")
            cmd += [
                "--create-home" if x["create_home"] else "--no-create-home",
                "--home-dir", x["home"], "--shell", x["shell"], "--comment", x["comment"]
            ]
            if x["groups"]:
                cmd += ["--groups", ",".join(x["groups"])]
            cmd += ["--", nm]
            script = " ".join(q(c) for c in cmd) + "\n"
            if x["lock"]:
                script += f"usermod -p '*' -- {q(nm)}\n"
            script += (
                f'mkdir -p {q(rd + "/users")}; printf %s {q(self.recipe.name)} > {q(rd + "/users/" + nm + ".created_by")}\n'
                f': > {q(used)}\n'
            )
            self.record({"type": "user", "name": nm, "created": True, "used": used, "created_by": f"{rd}/users/{nm}.created_by"})
            self.must(self.sh(script), f"Benutzer {nm} anlegen")
            self.log.log("CHG", f"Benutzer {nm} angelegt (Gruppen: {','.join(x['groups']) or 'keine'})")
        else:
            self.record({"type": "user", "name": nm, "created": False, "used": used})
            script = f'mkdir -p {q(rd + "/users")}; : > {q(used)}\n'
            if x["groups"]:
                script += f'for g in {" ".join(q(g) for g in x["groups"])}; do getent group "$g" >/dev/null && usermod -aG "$g" -- {q(nm)} || true; done\n'
            self.must(self.sh(script), f"Benutzer {nm} registrieren")
            self.log.log("OK" if it.status == "ok" else "WARN", f"Benutzer {nm}: Vorhanden (Gruppen synchronisiert)")
        self.changed_any = True

    def apply_file_like(self, it: Item) -> None:
        d, s, rd = q(it.label), q(self.stage), q(self.run_dir)
        is_block = it.kind == "block"
        x = it.extra
        backup = (it.spec.get("backup", True) if not is_block else True) and (
            it.status == "update" or (is_block and x.get("existed"))
        )
        op = {
            "type": it.kind, "dest": it.label, "action": it.status, "idx": it.idx,
            "backup": None, "meta_before": it.remote_meta,
            "existed": it.status != "create" or bool(x.get("existed"))
        }
        if backup:
            op["backup"] = f"files/{it.idx}.orig"
        if is_block:
            op.update({
                "id": x["id"], "comment": x["comment"], "position": x["position"],
                "prev_b64": x.get("prev_b64"), "enforce": x["enforce"], "owner": x["owner"],
                "group": x["group"], "mode": x["mode"]
            })
        else:
            op["reg_before"] = self.reg_before(it)
        self.record(op)

        if it.status == "meta":
            m_script = meta_script(it.label, x["mode"], x["owner"], x["group"]) if is_block else meta_script(it.label, it.mode, it.owner, it.group)
            self.must(self.sh(m_script), f"Rechte anpassen fuer {it.label}")
            return

        script = f'dest={d}\ndir=$(dirname "$dest")\n'
        if is_block:
            script += (
                f'if [ ! -d "$dir" ]; then mkdir -p "$dir"; grp={q(x["group"])}; [ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])}); '
                f'chown {q(x["owner"])}:"$grp" "$dir"; chmod {x["dir_mode"]} "$dir"; fi\n'
            )
        elif it.spec.get("mkdir", True):
            script += 'mkdir -p "$dir"\n'

        if backup:
            script += f'cp -a "$dest" {rd}/{op["backup"]}\n'

        script += 'tmp="$dir/.$(basename "$dest").dpl.$$"\n'
        if is_block:
            script += f'install -m 600 -o root -g root {s}/v{it.idx} "$tmp"\n'
            if x["enforce"]:
                script += f'grp={q(x["group"])}; [ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])});\nchown {q(x["owner"])}:"$grp" "$tmp"; chmod {x["mode"]} "$tmp"\n'
            else:
                script += (
                    f'if [ -e "$dest" ]; then chown --reference="$dest" "$tmp"; chmod --reference="$dest" "$tmp"; '
                    f'else chmod {x["mode"]} "$tmp"; fi\n'
                )
            script += 'mv -f "$tmp" "$dest"\n'
        else:
            script += (
                f'install -m {it.mode} -o {q(it.owner)} -g {q(it.group)} {s}/v{it.idx} "$tmp"\n'
                f'mv -f "$tmp" "$dest"\n' + self.reg_write_script(it) + 'sha256sum "$dest" | cut -d" " -f1\n'
            )

        p = self.must(self.sh(script), f"Aktivieren von {it.label}")
        if not is_block and p.out.strip().split("\n")[-1] != it.sha:
            raise StepFailed(f"{it.label}: Pruefsumme nach Schreiben nicht identisch.")
        self.changed_any = True

    def adopt_file(self, it: Item) -> None:
        self.record({"type": "reg", "dest": it.label, "reg_before": self.reg_before(it)})
        self.must(self.sh(self.reg_write_script(it)), f"Registriere {it.label}")

    def apply_remove(self, it: Item) -> None:
        d, rd = q(it.label), q(self.run_dir)
        rf = reg_file(self.state, it.label)
        op = {
            "type": "remove", "path": it.label, "backup": f"files/{it.idx}.removed",
            "meta_before": it.remote_meta, "reg_before": self.reg_before(it)
        }
        self.record(op)
        self.must(self.sh(f'cp -a {d} {rd}/{op["backup"]} && rm -f {d} {q(rf)}'), f"Entferne {it.label}")
        self.changed_any = True

    def apply_items(self) -> None:
        for it in self.items:
            if it.kind == "block" and it.extra.get("user") in self.skipped_users:
                self.log.log("SKIP", f"{it.extra['title']} (Benutzer uebersprungen)")
                continue
            if it.conflict and it.decision != "overwrite":
                self.log.log("SKIP", f"{it.label}: Uebersprungen laut Richtlinie")
                self.skipped.append(it)
                if it.kind == "user":
                    self.skipped_users.add(it.label)
                continue

            if it.kind == "user":
                self.apply_user(it)
            elif it.kind == "remove":
                if it.status == "ok":
                    self.log.log("OK", f"{it.label}: Bereits entfernt")
                else:
                    self.apply_remove(it)
                    self.log.log("CHG", f"Entfernt: {it.label}")
            else:
                title = it.extra.get("title", it.label)
                if it.status == "ok":
                    if it.kind == "file" and it.reg_recipe == "":
                        self.adopt_file(it)
                        self.log.log("OK", f"{it.label}: Unveraendert (Besitz registriert)")
                    else:
                        self.log.log("OK", f"{title}: Unveraendert")
                    continue
                self.apply_file_like(it)
                w = {"create": "neu angelegt", "update": "aktualisiert", "meta": "Rechte angepasst"}[it.status]
                self.log.log("CHG", f"{title}: {w}")

    def step_wanted(self, st: dict, name: str) -> tuple[bool, str]:
        if not self.var_ok(st):
            return False, "Variable"
        if st.get("when_changed") and not self.changed_any:
            return False, "keine Aenderung"
        for key, want in (("if_cmd", True), ("unless_cmd", False)):
            if key in st:
                p = self.sh(self.R(st[key], f"step '{name}' {key}"), sudo=st.get("sudo", True), strict=False, timeout=60)
                if (p.rc == 0) != want:
                    return False, "bereits erledigt" if key == "unless_cmd" else f"{key} nicht erfuellt"
        return True, ""

    def run_step(self, st: dict, idx: int, record_undo: bool = True) -> None:
        name = st.get("name", f"step {idx}")
        wanted, why = self.step_wanted(st, name)
        if not wanted:
            self.log.log("SKIP", f"{name} ({why})")
            return
        script = self.R(st["run"], f"step '{name}'")
        t = int(st.get("timeout", self.timeout))
        p = self.exec_local(script, t) if st.get("where", "remote") == "local" else self.sh(script, sudo=st.get("sudo", True), timeout=t)
        if p.rc != 0 and not st.get("ignore_errors", False):
            raise StepFailed(f"Schritt '{name}' fehlgeschlagen (rc={p.rc})\n{short(p.err or p.out, 800)}")
        self.log.log("WARN" if p.rc != 0 else "CHG", f"{name}" + (f" (ignoriert, rc={p.rc})" if p.rc != 0 else ""))
        if st.get("when_changed") is None:
            self.changed_any = True
        if record_undo and st.get("undo") and p.rc == 0:
            self.record({"type": "step", "name": name, "undo": self.R(st["undo"], f"step '{name}' undo"), "where": st.get("where", "remote")})

    def needs_ephemeral(self) -> bool:
        if not self.tests_enabled:
            return False
        for t in self.recipe.tests:
            if t.get("kind") == "ssh" and self.var_ok(t) and "ephemeral" in split_words(t.get("key", "ephemeral")):
                return True
        return False

    @contextlib.contextmanager
    def ephemeral_auth(self):
        added: list[Item] = []
        if self.needs_ephemeral():
            for i, a in enumerate(self.recipe.authorized_keys):
                if not self.var_ok(a):
                    continue
                it = self.auth_item(i, 900 + i, a, ephemeral=True)
                if it:
                    added.append(it)
        try:
            for it in added:
                self.stat_items([it])
                self.stage = self.stage or self.must(self.tr.exec_cmd("mktemp -d /tmp/dpl.XXXXXXXX", timeout=30), "tmp").out.strip()
                x = it.extra
                comp = compose_script(it.label, f"{self.stage}/e{it.idx}", x["id"], x["comment"], x["position"], it.data.decode())
                install_part = (
                    f'dir=$(dirname {q(it.label)}); tmp="$dir/.dpl.$$"\n'
                    f'if [ ! -d "$dir" ]; then mkdir -p "$dir"; grp={q(x["group"])}; '
                    f'[ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])}); '
                    f'chown {q(x["owner"])}:"$grp" "$dir"; chmod {x["dir_mode"]} "$dir"; fi\n'
                    f'install -m 600 -o root -g root {q(self.stage + "/e" + str(it.idx))} "$tmp"\n'
                    f'grp={q(x["group"])}; [ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])});\n'
                    f'chown {q(x["owner"])}:"$grp" "$tmp"; chmod {x["mode"]} "$tmp"; mv -f "$tmp" {q(it.label)}\n'
                )
                self.must(self.sh(comp + install_part), "Temporaeren Test-Schluessel einrichten")
            yield
        finally:
            for it in added:
                x = it.extra
                with contextlib.suppress(Exception):
                    out = f"{self.stage}/e{it.idx}"
                    comp = compose_script(it.label, out, x["id"], x["comment"], x["position"], None)
                    revert_part = (
                        f'dir=$(dirname {q(it.label)}); tmp="$dir/.dpl.$$"\n'
                        f'if [ ! -d "$dir" ]; then mkdir -p "$dir"; grp={q(x["group"])}; '
                        f'[ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])}); '
                        f'chown {q(x["owner"])}:"$grp" "$dir"; chmod {x["dir_mode"]} "$dir"; fi\n'
                        f'install -m 600 -o root -g root {q(out)} "$tmp"\n'
                        f'grp={q(x["group"])}; [ -n "$grp" ] || grp=$(id -gn {q(x["owner"])} 2>/dev/null || echo {q(x["owner"])});\n'
                        f'chown {q(x["owner"])}:"$grp" "$tmp"; chmod {x["mode"]} "$tmp"; mv -f "$tmp" {q(it.label)}\n'
                    )
                    self.sh(comp + revert_part, strict=False)
            if added:
                self.log.log("INFO", "Temporaerer Test-Schluessel entfernt")

    def run_tests(self, only: list[str] | None = None) -> bool:
        target_filter = self.cfg.S.get("TEST_HOST", "").strip() or self.cfg.S.get("TEST_TARGETS", "").strip()
        if target_filter:
            allowed = split_words(target_filter)
            if self.t.name not in allowed and self.t.host not in allowed:
                self.log.log("SKIP", f"Tests uebersprungen (nur fuer {target_filter} konfiguriert)")
                return True

        all_ok = True
        n_run = 0
        for t in self.recipe.tests:
            if only and not any(o.lower() in t["name"].lower() for o in only):
                continue
            if not self.var_ok(t):
                continue
            for label, ok, detail in self.one_test(t):
                n_run += 1
                nm = t["name"] + (f" [{label}]" if label else "")
                if ok:
                    self.log.log("PASS", f"Test: {nm}", detail if self.log.verbose else "")
                else:
                    crit = t.get("critical", True)
                    self.log.log("FAIL" if crit else "WARN", f"Test: {nm}", detail)
                    all_ok = all_ok and not crit
        if n_run == 0 and self.recipe.tests:
            self.log.log("WARN", "Keine Tests ausgefuehrt")
        return all_ok

    def one_test(self, t: dict) -> list[tuple[str, bool, str]]:
        name = t["name"]
        kind = t.get("kind", "remote")
        cmd = self.R(t["run"], f"test '{name}'")
        timeout = int(t.get("timeout", 30))
        tries = int(t.get("retries", 0)) + 1
        runs: list[tuple[str, str | None]] = [("", None)]

        if kind == "ssh":
            if not isinstance(self.tr, SSHTransport):
                return [("", False, "kind=ssh erfordert SSH-Transport (nicht local)")]
            raw_keys = split_words(self.R(t.get("key", "ephemeral"), f"test '{t['name']}' key"))
            runs = []
            for tok in raw_keys:
                if tok == "ephemeral":
                    runs.append(("ephemeral", Ephemeral.ensure(self.cfg.run_id)[0]))
                else:
                    pth = os.path.expanduser(tok)
                    runs.append((os.path.basename(pth), pth))

        out: list[tuple[str, bool, str]] = []
        for label, key in runs:
            proc = Proc(1, "", "nicht ausgefuehrt")
            ok, problems = False, ["nicht ausgefuehrt"]
            for attempt in range(tries):
                if kind == "ssh":
                    assert isinstance(self.tr, SSHTransport) and key
                    if not os.path.exists(key):
                        proc = Proc(1, "", f"Key-Datei fehlt: {key}")
                    else:
                        proc = self.tr.raw(self.R(t["user"], f"test '{name}' user"), key, cmd, timeout)
                elif kind == "local":
                    proc = self.exec_local(cmd, timeout)
                else:
                    proc = self.sh(cmd, sudo=t.get("sudo", True), timeout=timeout, strict=False)

                problems = []
                erc = t.get("expect_rc", 0)
                if erc != "any":
                    erc_list = [erc] if isinstance(erc, int) else list(erc)
                    if proc.rc not in erc_list:
                        problems.append(f"rc={proc.rc}, erwartet {erc_list}")
                for pat in ([t["expect"]] if isinstance(t.get("expect"), str) else t.get("expect", [])):
                    if not re.search(pat, proc.out, re.M):
                        problems.append(f"Erwartetes Muster nicht gefunden: /{pat}/")
                for pat in ([t["expect_not"]] if isinstance(t.get("expect_not"), str) else t.get("expect_not", [])):
                    if re.search(pat, proc.out + "\n" + proc.err, re.M):
                        problems.append(f"Unerwartetes Muster gefunden: /{pat}/")

                ok = len(problems) == 0
                if ok:
                    break
                if attempt + 1 < tries:
                    time.sleep(float(t.get("retry_delay", 2)))

            if t.get("cleanup"):
                self.sh(self.R(t["cleanup"], f"test '{name}' cleanup"), strict=False, timeout=timeout)
            out_str = f"{short(proc.out, 300)}\n{short(proc.err, 300)}".strip()
            out.append((label, ok, out_str if ok else "; ".join(problems) + ("\n" + out_str if out_str else "")))
        return out

    def diff_text(self, it: Item) -> str:
        cur = self.sh(f"head -c 300000 {q(it.label)}", strict=False).out
        diff = difflib.unified_diff(
            cur.splitlines(), it.data.decode("utf-8", "replace").splitlines(),
            "Vorhanden", "Neu", lineterm="", n=2
        )
        return "\n".join(list(diff)[:80]) or "(Keine Unterschiede)"

    def apply(self) -> bool:
        try:
            with self.lock():
                try:
                    self.stage_and_validate()
                    self.init_run()
                    try:
                        for i, st in enumerate(s for s in self.recipe.steps if s.get("phase", "post") == "pre"):
                            self.run_step(st, i + 1)
                        self.apply_items()
                        for i, st in enumerate(s for s in self.recipe.steps if s.get("phase", "post") == "post"):
                            self.run_step(st, i + 1)
                        self.save_manifest("applied")
                    except StepFailed:
                        self.log.log("WARN", "Fehler waehrend Installation - Fuehre Rollback aus...")
                        self.rollback_ops(self.ops)
                        self.save_manifest("rolled-back")
                        raise

                    tests_ok = True
                    if self.tests_enabled and self.recipe.tests:
                        with self.ephemeral_auth():
                            tests_ok = self.run_tests()

                    for i, st in enumerate(self.recipe.cleanup):
                        try:
                            self.run_step(st, i + 1, record_undo=False)
                        except StepFailed as e:
                            self.log.log("WARN", f"Cleanup fehlgeschlagen: {e}")

                    if not tests_ok and (getattr(self.opts, "rollback_on_fail", False) or truthy(self.cfg.S["ROLLBACK_ON_FAIL"])):
                        self.log.log("WARN", "Tests fehlgeschlagen - Rollback (--rollback-on-fail aktiv)")
                        self.rollback_ops(self.ops)
                        self.save_manifest("rolled-back")
                    return tests_ok
                finally:
                    if self.stage and self.stage.startswith("/tmp/dpl."):
                        self.sh(f"rm -rf {q(self.stage)}", strict=False, timeout=15)
                        self.stage = ""
        finally:
            pass

    def rollback_ops(self, ops: list[dict]) -> None:
        rd = q(self.run_dir)
        for op in reversed(ops):
            try:
                t = op["type"]
                if t == "step":
                    if op.get("where") == "local":
                        self.exec_local(op["undo"], self.timeout)
                    else:
                        self.sh(op["undo"], strict=False)
                    self.log.log("CHG", f"Undo: {op['name']}")
                    continue
                if t == "user":
                    nm = op["name"]
                    s = f"rm -f {q(op['used'])}\n"
                    if op.get("created"):
                        s += f"userdel -- {q(nm)} || true\nrm -f {q(op['created_by'])}\n"
                    self.must(self.sh(s, strict=False), f"Benutzer {nm} zuruecksetzen")
                    self.log.log("CHG", f"Zurueckgesetzt: Benutzer {nm}")
                    continue
                if t == "remove":
                    d = q(op["path"])
                    rf = reg_file(self.state, op["path"])
                    self.must(self.sh(f'cp -a {rd}/{op["backup"]} {d} && rm -f {q(rf)}'), f"Wiederherstellen {op['path']}")
                    self.log.log("CHG", f"Wiederhergestellt: {op['path']}")
                    continue
                if t == "block":
                    d = q(op["dest"])
                    prev = base64.b64decode(op["prev_b64"]).decode() if op.get("prev_b64") is not None else None
                    script = "out=$(mktemp /tmp/dpl.rb.XXXXXX)\n" + compose_script(op["dest"], '"$out"', op["id"], op["comment"], op["position"], prev)
                    script += (
                        f'if [ ! -s "$out" ] && [ {"1" if not op.get("existed") else "0"} = 1 ]; then rm -f {d}\n'
                        f'else tmp="$(dirname {d})/.dpl.rb.$$"; install -m 600 -o root -g root "$out" "$tmp"\n'
                        f'  if [ -e {d} ]; then chown --reference={d} "$tmp"; chmod --reference={d} "$tmp"; fi; mv -f "$tmp" {d}; fi\n'
                        f'rm -f "$out"\n'
                    )
                    self.must(self.sh(script), f"Block {op['id']} zuruecksetzen")
                    self.log.log("CHG", f"Block zurueckgesetzt: {op['id']} in {op['dest']}")
                    continue

                d = q(op["dest"])
                rf = reg_file(self.state, op["dest"])
                if op["action"] == "create":
                    self.must(self.sh(f"rm -f {d} {q(rf)}"), f"Entferne {op['dest']}")
                elif op.get("backup"):
                    self.must(self.sh(f'cp -a {rd}/{op["backup"]} {d} && rm -f {q(rf)}'), f"Wiederherstellen {op['dest']}")
                self.log.log("CHG", f"Zurueckgesetzt: {op['dest']}")
            except StepFailed as e:
                self.log.log("FAIL", f"Rollback-Schritt fehlgeschlagen: {e}")

    def cmd_test(self) -> bool:
        if not self.analyze():
            return False
        files = [it for it in self.items if it.kind in ("file", "block")]
        missing = [it.label for it in files if it.status == "create"]
        if missing:
            names = ", ".join(Path(m).name if m.startswith("/usr/local/bin/") else m for m in missing)
            self.log.log(
                "SKIP",
                f"Noch nicht ausgerollt ({len(missing)}/{len(files)} fehlen: {names}) "
                f"- zuerst ./deploy.py deploy"
            )
            return True
        try:
            with self.ephemeral_auth():
                ok = self.run_tests()
        finally:
            if self.stage and self.stage.startswith("/tmp/dpl."):
                self.sh(f"rm -rf {q(self.stage)}", strict=False, timeout=15)
                self.stage = ""
        if not ok:
            self.err = "Tests fehlgeschlagen"
        return ok

    def cmd_plan(self) -> bool:
        if not self.analyze():
            return False
        for it in self.items:
            name = it.extra.get("title", it.label)
            if it.status == "ok":
                self.log.log("OK", f"{name}: Unveraendert")
            elif it.status == "exists":
                self.log.log("OK", f"{name}: Vorhanden (bleibt bestehen)")
            else:
                w = {"create": "wuerde angelegt", "update": "wuerde aktualisiert", "meta": "wuerde Rechte anpassen", "remove": "wuerde entfernt"}.get(it.status, it.status)
                self.log.log("DRY", f"{name}: {w}")
            if it.conflict:
                pol = it.spec.get("on_conflict", "ask")
                pol = self.cfg.S["ON_CONFLICT"] if pol == "ask" else pol
                self.log.log("ASK" if pol == "ask" else "INFO", f"Konflikt ({pol}): {it.label} -> {it.conflict['msg']}")
            if it.status == "update" and it.kind == "file" and getattr(self.opts, "diff", False):
                self.log.log("INFO", "Diff", self.diff_text(it))
        return True

    def cmd_status(self) -> bool:
        if not self.analyze():
            return False
        all_ok = True
        drift = 0
        for it in self.items:
            name = it.extra.get("title", it.label)
            if it.status == "ok":
                self.log.log("OK", f"{name}: Entspricht Soll-Zustand")
            else:
                self.log.log("WARN", f"{name}: Weicht ab (Status: {it.status})")
                all_ok = False
                drift += 1
        if drift:
            self.err = f"{drift} Abweichung(en)"
        return all_ok

class Decider:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.policy = cfg.S["ON_CONFLICT"]
        self.assume_yes = truthy(cfg.S.get("ASSUME_YES", "0"))
        self.memo: dict[str, str] = {}
        self.blanket = ""

    def decide_all(self, runners: list[HostRunner]) -> None:
        pending = [(r, it) for r in runners for it in r.conflicts]
        if not pending:
            return
        aborted: list[str] = []
        for r, it in pending:
            pol = it.spec.get("on_conflict", "ask")
            pol = self.policy if pol == "ask" else pol

            if self.assume_yes and pol == "ask":
                it.decision = "overwrite"
                continue
            if it.kind == "user" and truthy(self.cfg.S.get("ALLOW_EXISTING_USER", "0")):
                it.decision = "overwrite"
                continue

            if pol == "abort":
                aborted.append(f"{r.t.name}: {it.label} ({it.conflict['msg']})")
            elif pol in ("overwrite", "skip"):
                it.decision = pol
            else:
                it.decision = self.ask(r, it)
        if aborted:
            raise ConfigError("Abbruch durch Konflikt-Richtlinie 'abort':\n  " + "\n  ".join(aborted))

    def ask(self, r: HostRunner, it: Item) -> str:
        if it.cid in self.memo:
            return self.memo[it.cid]
        if self.blanket:
            return self.blanket
        if not sys.stdin.isatty():
            raise ConfigError(f"Konflikt auf {r.t.name} ({it.label}), aber kein TTY verfuegbar. Parameter --overwrite, --skip-conflicts oder -y verwenden.")

        action = "ueberschreiben" if it.kind != "user" else "mitbenutzen"
        while True:
            UI.emit(f"\n{UI.paint('ASK', 'KONFLIKT')} auf {r.t.name} ({it.conflict['kind']}):\n  Ziel: {it.label}\n  Info: {it.conflict['msg']}")
            ans = input(
                f"  [y] {action}              [n] Ueberspringen\n"
                f"  [Y] {action} auf ALLEN    [N] Ueberspringen auf ALLEN\n"
                f"  [d] Diff anzeigen         [q] Abbrechen > "
            ).strip()
            if ans == "y":
                return "overwrite"
            if ans == "n":
                return "skip"
            if ans == "Y":
                self.blanket = "overwrite"
                return "overwrite"
            if ans == "N":
                self.blanket = "skip"
                return "skip"
            if ans == "d":
                UI.emit(r.diff_text(it) if it.kind == "file" else "(Diff nur fuer Dateien verfuegbar)")
                continue
            if ans == "q":
                raise ConfigError("Deployment durch Benutzer abgebrochen.")

def run_phase(cfg: Cfg, runners: list[HostRunner], fn, verbose: bool, keep_going: bool = False) -> None:
    jobs = int(cfg.S["JOBS"])
    keep_going = keep_going or truthy(cfg.S["KEEP_GOING"])
    abort = threading.Event()

    def work(r: HostRunner) -> None:
        if abort.is_set():
            r.ok, r.err = False, "Uebersprungen wegen Fehlern auf anderem Host"
            return
        t0 = time.time()
        r.err = ""
        r.ok = False
        try:
            r.ok = bool(fn(r))
        except StepFailed as e:
            r.err = str(e)
            r.log.log("FAIL", r.err.split("\n")[0], "\n".join(r.err.split("\n")[1:]))
        except Exception as e:
            r.err = f"{type(e).__name__}: {e}"
            r.log.log("FAIL", f"Unerwarteter Fehler: {r.err}", traceback.format_exc() if verbose else "")
        r.seconds += time.time() - t0
        if not r.ok and not keep_going:
            abort.set()
        r.log.flush()

    if jobs == 1:
        for r in runners:
            work(r)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            list(ex.map(work, runners))

def print_run_header(cfg: Cfg, verb: str) -> None:
    r = cfg.recipe
    if not r:
        return
    ver = f" v{r.version}" if r.version else ""
    UI.emit(f"{UI.paint('INFO', verb)}: Rezept {r.name}{ver} - {len(r.files)} Datei(en), "
            f"{len(r.tests)} Test(s), {cfg.n_targets} Ziel(e)")

def print_summary_table(runners: list[HostRunner]) -> None:
    UI.emit("\n" + "=" * 90)
    UI.emit(f"{'HOST':<28} {'DAUER':<8} {'STATUS':<8} {'TEST':<8} GEAENDERTE ELEMENTE / DETAILS")
    UI.emit("-" * 90)
    for r in runners:
        tag = "OK" if r.ok else "FAIL"
        c = r.log.counts
        test_str = c["test"]
        test_tag = ("PASS" if test_str == "PASS" else "SKIP" if test_str == "SKIP"
                    else "WARN" if test_str == "WARN" else "FAIL")
        test_colored = UI.paint(test_tag, f"{test_str:<6}")

        if not r.ok:
            details = UI.paint("FAIL", r.err.splitlines()[0] if r.err else "Fehlgeschlagen")
        elif r.log.changed_items:
            details = UI.paint("CHG", ", ".join(r.log.changed_items))
        else:
            details = "Keine Aenderungen (Soll = Ist)"

        UI.emit(
            f"{r.t.name:<28} {r.seconds:5.1f}s   [{UI.paint(tag, f'{tag:<4}')}] "
            f"{test_colored}  {details}"
        )
    UI.emit("=" * 90)

def deploy_flow(cfg: Cfg, runners: list[HostRunner], args: argparse.Namespace) -> int:
    verbose = bool(getattr(args, "verbose", False))
    keep_going = truthy(cfg.S["KEEP_GOING"])
    print_run_header(cfg, "DEPLOY")

    run_phase(cfg, runners, lambda r: r.analyze(), verbose)
    failed = [r for r in runners if not r.ok]
    if failed and not keep_going:
        UI.emit("\nAbbruch: Vorab-Pruefung fehlgeschlagen. Es wurden keine Aenderungen durchgefuehrt.")
        print_summary_table(runners)
        return 1

    good = [r for r in runners if r.ok]
    Decider(cfg).decide_all(good)
    run_phase(cfg, good, lambda r: r.apply(), verbose)

    print_summary_table(runners)
    return 1 if any(not r.ok for r in runners) else 0

def find_profile(spec: str) -> Path:
    p = Path(os.path.expanduser(spec))
    cands: list[Path] = [p, p.with_name(p.name + ".env")]
    for base in profile_dirs():
        cands += [base / spec, base / f"{spec}.env"]
    for c in cands:
        if c.is_file():
            return c.resolve()
    raise ConfigError(f"Profil {spec!r} nicht gefunden (erwartet: profiles/<name>.env).")

def profile_dirs() -> list[Path]:
    dirs = [Path.cwd() / "profiles", SCRIPT_DIR / "profiles"]
    return dirs + [Path(x) for x in os.environ.get("DEPLOY_PROFILE_PATH", "").split(":") if x]

def discover_profiles() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for base in profile_dirs():
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.env")):
            out.setdefault(p.stem, p)
    return out

def list_profiles() -> int:
    profs = discover_profiles()
    if not profs:
        UI.emit(UI.paint("WARN", "Keine Profile gefunden.") + " Erwartet: profiles/<name>.env")
        return 1
    base = parse_dotenv(Path.cwd() / ".env") if (Path.cwd() / ".env").is_file() else {}
    UI.emit(f"\n{'PROFIL':<20} {'REZEPT':<24} {'ZIELE':<7} ZUGANG")
    UI.emit("-" * 90)
    for name in sorted(profs):
        cfg = dict(base)
        cfg.update(parse_dotenv(profs[name], base))
        key = os.path.expanduser(cfg.get("SSH_KEY", "")) if cfg.get("SSH_KEY") else ""
        zugang = f"{cfg.get('SSH_USER') or 'root'}@{cfg.get('SSH_PORT') or '22'}"
        zugang += f", {Path(key).name}" if key else ", kein Key"
        if key and not Path(key).is_file():
            zugang += " (FEHLT)"
        UI.emit(f"{name:<20} {cfg.get('RECIPE') or '-':<24} {len(split_words(cfg.get('TARGETS', ''))):<7} {zugang}")
    UI.emit("-" * 90)
    UI.emit("Ausfuehren: ./deploy.py plan -p <profil>   |   ./deploy.py deploy -p <profil>")
    return 0

def discover_recipes() -> tuple[dict[str, tuple[Recipe, Path]], list[str]]:
    bases: list[Path] = [Path.cwd() / "recipes", SCRIPT_DIR / "recipes"]
    bases += [Path(x) for x in os.environ.get("DEPLOY_RECIPE_PATH", "").split(":") if x]

    found: dict[str, tuple[Recipe, Path]] = {}
    errors: list[str] = []
    for base in bases:
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*/recipe.toml")) + sorted(base.glob("*.toml")):
            if not p.is_file():
                continue
            try:
                rec = load_recipe(p)
            except (ConfigError, OSError) as e:
                errors.append(f"{p}: {e}")
                continue
            found.setdefault(rec.name, (rec, p))
    return found, errors

def pick_profile(env_cfg: dict[str, str]) -> tuple[str, dict[str, str]]:
    profs = discover_profiles()
    if not profs:
        return "", env_cfg
    names = sorted(profs)
    dflt = env_cfg.get("PROFILE", "")
    UI.emit("\nProfil (Server, Zugang, Rezept):")
    for i, n in enumerate(names, 1):
        cfg = dict(env_cfg)
        cfg.update(parse_dotenv(profs[n], env_cfg))
        mark = "  (Standard)" if n == dflt else ""
        UI.emit(f"  [{i}] {n:<18} {cfg.get('RECIPE') or '-':<24} "
                f"{len(split_words(cfg.get('TARGETS', '')))} Ziele{mark}")
    d = str(names.index(dflt) + 1) if dflt in names else "1"
    ans = input(f"\nWahl [1-{len(names)}, Standard: {d}]: ").strip() or d
    if ans.isdigit() and 1 <= int(ans) <= len(names):
        name = names[int(ans) - 1]
    elif ans in profs:
        name = ans
    else:
        name = names[int(d) - 1]
    cfg = dict(env_cfg)
    cfg.update(parse_dotenv(profs[name], env_cfg))
    cfg["PROFILE"] = name
    return name, cfg

def pick_recipe(default: str, recipes: dict[str, tuple[Recipe, Path]] | None = None) -> str:
    if recipes is None:
        recipes = discover_recipes()[0]
    names = sorted(recipes)
    if not names:
        UI.emit(UI.paint("FAIL", "Keine Rezepte gefunden") + " - erwartet: recipes/<name>/recipe.toml")
        return default
    UI.emit("\nRezepte:")
    for i, n in enumerate(names, 1):
        rec = recipes[n][0]
        mark = "  (Standard)" if n == default else ""
        UI.emit(f"  [{i}] {n:<22} {rec.version or '-':<11} {len(rec.files)} Dateien  {rec.description or ''}{mark}")
    dflt = str(names.index(default) + 1) if default in names else "1"
    ans = input(f"Wahl [1-{len(names)}, Standard: {dflt}]: ").strip() or dflt
    if ans.isdigit() and 1 <= int(ans) <= len(names):
        return names[int(ans) - 1]
    return ans if ans in recipes else names[int(dflt) - 1]

def list_recipes() -> int:
    found, errors = discover_recipes()
    if not found:
        UI.emit(UI.paint("WARN", "Keine Rezepte gefunden.") + " Erwartet: recipes/<name>/recipe.toml")
    else:
        UI.emit(f"\n{'REZEPT':<24} {'VERSION':<12} {'DATEIEN':<8} BESCHREIBUNG")
        UI.emit("-" * 100)
        for name in sorted(found):
            rec, path = found[name]
            UI.emit(f"{name:<24} {(rec.version or '-'):<12} {len(rec.files):<8} {rec.description or '-'}")
            UI.emit(f"{'':<24} {path}")
        UI.emit("-" * 100)
        UI.emit("Ausfuehren: ./deploy.py deploy -r <rezept>  (Standard aus RECIPE in .env)")

    for e in errors:
        UI.emit(UI.paint("FAIL", "FEHLER") + f" {e}")
    return 0 if found else 1

def cmd_selftest(S: dict[str, str], do_ssh: bool) -> int:
    rows: list[tuple[str, str, str]] = []
    state = {"ok": True}

    def add(name: str, good: bool, detail: str = "") -> None:
        state["ok"] = state["ok"] and good
        rows.append((name, "PASS" if good else "FAIL", detail))

    add("Python >= 3.11 (tomllib)", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")

    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        targets = split_words(S.get("TARGETS", ""))
        key = os.path.expanduser(S["SSH_KEY"]) if S.get("SSH_KEY") else ""
        detail = f"{len(targets)} Ziel(e), SSH-User {S.get('SSH_USER') or '?'}"
        if S.get("RECIPE"):
            detail += f", Standard-Rezept {S['RECIPE']}"
        add(".env im Arbeitsverzeichnis", bool(targets), detail if targets else "TARGETS ist leer")
        add("SSH-Key lesbar", bool(key) and Path(key).is_file(), key or "SSH_KEY ist leer")
    else:
        add(".env im Arbeitsverzeichnis", False, f"{env_path} fehlt")

    recipes, errors = discover_recipes()
    for e in errors:
        add("Rezept ladbar", False, e)
    add("Rezepte gefunden", bool(recipes),
        ", ".join(sorted(recipes)) or "erwartet: recipes/<name>/recipe.toml")

    for name in sorted(recipes):
        rec, path = recipes[name]
        try:
            cfg = Cfg(S, {}, rec, [], "selftest", "/tmp/dpl-selftest", "/tmp/dpl-selftest-ctl", 1, ["selftest"])
            runner = HostRunner(
                cfg, Target("selftest", "127.0.0.1", "root", 22), ProbeTransport(),
                HostLog("selftest", False, False, []),
                argparse.Namespace(no_test=False, force=False, diff=False)
            )
            items = runner.build_items()
            files = [i for i in items if i.kind == "file"]
            detail = f"{len(files)} Datei(en), {len(rec.tests)} Test(s) - {path}"
            for i in files:
                detail += f"\n{i.label}  {i.mode} {i.owner}:{i.group}  {len(i.data)} B  sha={i.sha[:12]}"
            add(f"Rezept {name} (Platzhalter, Quellen, Rechte)", True, detail)
        except Exception as e:
            add(f"Rezept {name} (Platzhalter, Quellen, Rechte)", False, str(e))

    profs = discover_profiles()
    if profs:
        for pname in sorted(profs):
            try:
                pcfg = dict(S)
                pcfg.update(parse_dotenv(profs[pname], S))
                prec = pcfg.get("RECIPE", "")
                if prec and prec not in recipes:
                    raise ConfigError(f"Rezept {prec!r} fehlt (./deploy.py recipes)")
                n = len(split_words(pcfg.get("TARGETS", "")))
                add(f"Profil {pname}", bool(n) and bool(prec), f"{prec or '-'} - {n} Ziel(e) - {profs[pname]}")
            except Exception as e:
                add(f"Profil {pname}", False, str(e))

    if S.get("RECIPE") and S["RECIPE"] not in recipes:
        add(f"Standard-Rezept {S['RECIPE']}", False, "nicht gefunden - siehe ./deploy.py recipes")

    if not do_ssh:
        rows.append(("SSH-Erreichbarkeit", "SKIP", "uebersprungen (mit --ssh pruefen)"))
    else:
        inv_path = Path(os.path.expanduser(S["INVENTORY"])) if S.get("INVENTORY") else (
            Path.cwd() / "inventory.toml" if (Path.cwd() / "inventory.toml").is_file() else None)
        ctl = tempfile.mkdtemp(prefix="dpl-ctl-")
        try:
            try:
                targets = resolve_targets(S.get("TARGETS", ""), S, load_inventory(inv_path))
            except ConfigError as e:
                add("SSH-Erreichbarkeit", False, str(e))
                targets = []
            for t in targets:
                tr = LocalTransport(S) if t.local else SSHTransport(t, S, ctl)
                cfg = Cfg(S, {}, None, [], "selftest", ctl, ctl, 1, [t.name])
                runner = HostRunner(cfg, t, tr, HostLog(t.name, False, False, []),
                                    argparse.Namespace(no_test=False, force=False, diff=False))
                try:
                    runner.connect()
                    add(f"SSH {t.name}", True, f"erreichbar als {t.user or S.get('SSH_USER')}")
                except Exception as e:
                    add(f"SSH {t.name}", False, str(e).splitlines()[0])
                finally:
                    tr.close()
        finally:
            shutil.rmtree(ctl, ignore_errors=True)

    UI.emit("")
    for name, status, detail in rows:
        UI.emit(f"[{UI.paint(status, f'{status:<4}')}] {name}")
        for line in detail.splitlines():
            if line:
                UI.emit(f"       | {line}")
    good = state["ok"]
    UI.emit("\n" + (UI.paint("PASS", "Selbstpruefung bestanden.") if good
                    else UI.paint("FAIL", "Selbstpruefung fehlgeschlagen.")))
    return 0 if good else 1

def print_help_env():
    help_text = """
=== deploy.py: Vollstaendige .env Konfigurations-Referenz ===

[REZEPTE]
  RECIPE                Standard-Rezept fuer deploy/plan/status/test
                        (z. B. 'site-rescue-audit'). Rezepte liegen als
                        recipes/<name>/recipe.toml neben deploy.py.
                        Uebersicht: ./deploy.py recipes
  Hinweis               'push' ist kein eigener Mechanismus, sondern ein zur Laufzeit
                        erzeugtes Ein-Datei-Rezept aus den PAYLOAD-Werten unten. Es
                        laeuft durch dieselbe Pruef-, Sicherungs- und Rollback-Logik
                        und ist nur fuer ad-hoc einzelne Dateien gedacht.

[VERBINDUNG]
  TARGETS               Zielserver (Kommasepariert, z. B. 'web1.example.com,web2.example.com')
  SSH_USER              Admin-Benutzer fuer Ausrollung (Standard: 'root')
  SSH_KEY               Lokaler Private-Key fuer Admin-Login (Standard: '~/.ssh/id_ed25519')
  SSH_PORT              SSH-Port (Standard: '22')
  JOBS                  Parallele Verbindungen (Standard: '4'; '1' = nacheinander)

[EINZELDATEI (push)]
  push ist ein normales Rezept (recipes/push). Seine Werte kommen aus der
  Kommandozeile, nicht aus .env: ./deploy.py push <datei> --dest <pfad>
  Zusaetze: --mode --owner --group --system-user --sudoers --sudoers-name
            --authorized-keys --whitelist-ip --test
  Fuer alles Weitere ein eigenes Rezept unter recipes/<name>/ anlegen.

[BENUTZER]
  SYSTEM_USER           Systembenutzer, der angelegt/genutzt wird (z. B. 'rescue')
  SYSTEM_USER_GROUPS    Sekundaere Gruppen (Standard: 'sshallow' fuer OpenSSH-Zugang)
  SYSTEM_USER_SHELL     Shell des Benutzers (Standard: '/bin/bash')
  ALLOW_EXISTING_USER   '1' = Vorhandenen Benutzer ohne Bestaetigungs-Prompt mitbenutzen

[RECHTE & SUDOERS]
  SUDOERS_NAME          Dateiname in /etc/sudoers.d/ (keine Punkte!, z. B. 'site-rescue')
  SUDOERS_CONTENT       Sudoers-Regel (wird vor Aktivierung zwingend mit visudo -cf validiert)

[SSH-ZUGANG & KEYS]
  AUTHORIZED_KEYS       Schluessel fuer ~/.ssh/authorized_keys des SYSTEM_USER.
                        Unterstuetzt OpenSSH-Optionen und inline 'file:~/.ssh/key.pub'!
  WHITELIST_IP          IP-Adresse oder Range fuer Fail2ban / Firewall-Whitelist

[POST-DEPLOY TESTS]
  TEST_COMMAND          Befehl fuer E2E-Funktionstest (z. B. 'diag example.com')
  TEST_USER             SSH-Benutzer fuer den Test (z. B. 'rescue')
  TEST_KEY              Lokaler Private-Key fuer den Test (z. B. '~/.ssh/id_ed25519')
  TEST_EXPECT           Erwartetes Regex-Muster in stdout des Tests
  TEST_HOST             Filter: Fuehrt den Test NUR auf diesem Host aus (z. B. 'web1.example.com')

[KONFLIKT-BEHANDLUNG]
  ON_CONFLICT           Verhalten bei abweichenden Dateien: ask (Standard), overwrite, skip, abort
  ROLLBACK_ON_FAIL      '1' = Automatischer Rollback, wenn der Test nach Deploy fehlschlaegt
"""
    print(help_text.strip())

def interactive_wizard() -> list[str]:
    UI.emit("\n" + "=" * 60)
    UI.emit(f"=== {UI.paint('INFO', 'deploy.py v' + VERSION + ' - Interaktiver Setup-Wizard')} ===")
    UI.emit("=" * 60)

    cwd_env = Path.cwd() / ".env"
    env_cfg = parse_dotenv(cwd_env) if cwd_env.is_file() else {}

    UI.emit("\nWas moechtest du tun?")
    UI.emit("  [1] Probelauf ohne Aenderungen (plan)")
    UI.emit("  [2] Rezept ausrollen (deploy)")
    UI.emit("  [3] Soll-Zustand pruefen (status)")
    UI.emit("  [4] Tests ausfuehren (test)")
    UI.emit("  [5] Einzelne Datei ausrollen (push)")
    UI.emit("  [6] Befehl remote ausfuehren (run)")
    UI.emit("  [7] Selbstpruefung + Rezeptliste (selftest)")
    UI.emit("  [8] .env Referenz anzeigen (help-env)")
    UI.emit("  [q] Beenden")

    choice = input("\nWahl [1-8, Standard: 1]: ").strip() or "1"
    if choice == "q":
        sys.exit(0)
    if choice == "8":
        print_help_env()
        sys.exit(0)

    cmd_map = {"1": "plan", "2": "deploy", "3": "status", "4": "test",
               "5": "push", "6": "run", "7": "selftest"}
    cmd = cmd_map.get(choice, "plan")

    if cmd == "selftest":
        cli_args = ["selftest"]
        if input("\nSSH-Erreichbarkeit mitpruefen (nur lesend)? [y/N]: ").strip().lower() == "y":
            cli_args.append("--ssh")
        return cli_args

    profile, env_cfg = pick_profile(env_cfg)

    dflt_targets = env_cfg.get("TARGETS", "")
    targets = input(f"\nZielserver ({f'Standard: {dflt_targets}' if dflt_targets else 'z. B. srv1,srv2'}): ").strip() or dflt_targets
    if not targets:
        sys.exit("Fehler: Keine Zielserver angegeben.")

    cli_args = [cmd, "-t", targets]
    if profile:
        cli_args += ["-p", profile]

    jobs = input("\nParallele Verbindungen [Standard: 4]: ").strip() or "4"
    if not jobs.isdigit() or int(jobs) < 1:
        UI.emit("  Ungueltig - nehme 4.")
        jobs = "4"
    cli_args += ["-j", jobs]

    if cmd in ("deploy", "plan", "status", "test"):
        if not profile:
            name = pick_recipe(env_cfg.get("RECIPE", ""))
            cli_args += ["-r", name]
            rec = discover_recipes()[0].get(name)
            if rec:
                UI.emit(f"\nRezept {UI.paint('INFO', name)}: {rec[0].description or '-'} "
                        f"({len(rec[0].files)} Dateien, {len(rec[0].tests)} Tests)")
        if cmd == "deploy":
            if input("\nProbelauf vorher anzeigen? [Y/n]: ").strip().lower() != "n":
                diff = input("  Diffs der Aenderungen anzeigen? [y/N]: ").strip().lower() == "y"
                plan_args = (["plan", "-t", targets, "-p", profile] if profile
                             else ["plan", "-r", name, "-t", targets]) + (["--diff"] if diff else [])
                UI.emit(f"\n{UI.paint('INFO', 'Probelauf')}: ./deploy.py " + " ".join(shlex.quote(a) for a in plan_args))
                main(plan_args)
                if input("\nJetzt ausrollen? [Y/n]: ").strip().lower() == "n":
                    sys.exit(0)
            if "," in targets and input("Bei Fehlern auf anderen Hosts weitermachen? [Y/n]: ").strip().lower() != "n":
                cli_args.append("--keep-going")
            if input("Auch unveraenderte Dateien neu schreiben (--force)? [y/N]: ").strip().lower() == "y":
                cli_args.append("--force")

    if cmd == "push":
        dflt_src = env_cfg.get("PUSH_SRC", "./scripts/example.sh")
        src = input(f"Lokale Datei/Skript [Standard: {dflt_src}]: ").strip() or dflt_src
        cli_args.append(src)

        dflt_dest = env_cfg.get("PUSH_DEST", f"/usr/local/bin/{Path(src).name}")
        dest = input(f"Zielpfad auf Server [Standard: {dflt_dest}]: ").strip() or dflt_dest
        cli_args += ["--dest", dest]

        do_overwrite = input("Bestehende Dateien ueberschreiben? [Y/n]: ").strip().lower() != "n"
        if do_overwrite:
            cli_args.append("--overwrite")

        skip_test = input("Tests ueberspringen? [y/N]: ").strip().lower() == "y"
        if skip_test:
            cli_args.append("--no-test")

    elif cmd == "plan":
        show_diff = input("Diffs geaenderter Dateien anzeigen? [y/N]: ").strip().lower() == "y"
        if show_diff:
            cli_args.append("--diff")

    elif cmd == "run":
        remote_cmd = input("Befehl, der remote ausgefuehrt werden soll: ").strip()
        if not remote_cmd:
            sys.exit("Fehler: Kein Befehl angegeben.")
        cli_args += ["--", remote_cmd]

    UI.emit(f"\nGenerierter Aufruf: {UI.paint('PASS', './deploy.py ' + ' '.join(shlex.quote(a) for a in cli_args))}")
    if cmd != "deploy" and input("Jetzt ausfuehren? [Y/n]: ").strip().lower() == "n":
        sys.exit(0)

    return cli_args

def print_completion(shell: str, install: bool):
    subcommands = "help-env recipes profiles selftest push deploy plan status test run wizard completion"
    flags = (
        "-t --targets -i --inventory -u --user -P --port -k --key -j --jobs -e --env "
        "-s --set -y --yes --overwrite --skip-conflicts --rollback-on-fail --keep-going -v --verbose"
    )

    bash_code = f"""# deploy.py bash completion
_deploy_py() {{
    local cur prev words cword
    _init_completion || return

    local cmds="{subcommands}"
    local common_flags="{flags}"

    if [[ ${{cword}} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${{cmds}} -h --help --version" -- "${{cur}}") )
        return 0
    fi

    case "${{prev}}" in
        -t|--targets)
            return 0
            ;;
        -r|--recipe)
            COMPREPLY=( $(compgen -d -- "${{cur}}") )
            return 0
            ;;
        -k|--key|-e|--env|--script)
            COMPREPLY=( $(compgen -f -- "${{cur}}") )
            return 0
            ;;
        -j|--jobs|-P|--port|-u|--user)
            return 0
            ;;
    esac

    COMPREPLY=( $(compgen -W "${{common_flags}} --dest --mode --owner --group --system-user --sudoers --sudoers-name --authorized-keys --whitelist-ip --test --test-user --test-key --no-test --diff --force" -- "${{cur}}") )
}}
complete -F _deploy_py deploy.py ./deploy.py
"""

    if install:
        home = Path.home()
        target_rc = home / (".bashrc" if "bash" in shell else ".zshrc")
        tag = "# deploy.py autocompletion"
        content = target_rc.read_text(encoding="utf-8") if target_rc.is_file() else ""
        if tag in content:
            UI.emit(f"Autovervollstaendigung ist in {target_rc} bereits registriert.")
            return

        with open(target_rc, "a", encoding="utf-8") as f:
            f.write(f"\n{tag}\n{bash_code}\n")
        UI.emit(f"{UI.paint('PASS', 'Erfolgreich installiert in ' + str(target_rc))}")
        UI.emit("Fuer sofortige Aktivierung im Terminal ausfuehren:")
        UI.emit(f"  source {target_rc}")
    else:
        print(bash_code)

def make_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("Ziele & Verbindung")
    S_ = argparse.SUPPRESS
    g.add_argument("-t", "--targets", default=S_, help="Zielserver (kommasepariert, IP, User@Host:Port oder @gruppe)")
    g.add_argument("-i", "--inventory", default=S_, help="TOML-Inventardatei")
    g.add_argument("-u", "--user", default=S_, help="SSH-Admin-User (Standard: root)")
    g.add_argument("-P", "--port", default=S_, help="SSH-Port")
    g.add_argument("-k", "--key", default=S_, help="SSH-Private-Key fuer Admin-Login")
    g.add_argument("-j", "--jobs", default=S_, help="Parallele Verbindungen (Standard: 4, 1 = nacheinander)")
    g.add_argument("-e", "--env", action="append", default=S_, help="Zusaetzliche .env-Datei")
    g.add_argument("-p", "--profile", default=S_,
                   help="Profil aus profiles/<name>.env (Server, Zugang, Rezept, Werte)")
    g.add_argument("-s", "--set", action="append", default=S_, metavar="K=V", help="Variable setzen")
    g.add_argument("-y", "--yes", action="store_true", default=S_, help="Bestaetigungen ueberspringen")
    g.add_argument("--overwrite", action="store_true", default=S_, help="Bestehende Fremddateien ueberschreiben")
    g.add_argument("--skip-conflicts", action="store_true", default=S_, help="Konflikte automatisch ueberspringen")
    g.add_argument("--rollback-on-fail", action="store_true", default=S_, help="Automatischer Rollback wenn Tests fehlschlagen")
    g.add_argument("--keep-going", action="store_true", default=S_, help="Bei Fehlern auf anderen Hosts weitermachen")
    g.add_argument("-v", "--verbose", action="store_true", default=S_, help="Ausfuehrliche Log-Ausgabe")

    ap = argparse.ArgumentParser(
        prog="deploy.py",
        description=f"deploy.py v{VERSION} - Universelles Server-Deployment-Werkzeug",
        epilog="Hilfe: ./deploy.py help <befehl> - Rezeptuebersicht: ./deploy.py recipes"
    )
    ap.add_argument("--version", action="version", version=f"deploy.py {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=False)

    subparsers = {}

    subparsers["help-env"] = sub.add_parser("help-env", help="Vollstaendige Dokumentation aller .env Variablen anzeigen")
    subparsers["wizard"] = sub.add_parser("wizard", help="Interaktiver CLI-Setup-Assistent")

    comp_parser = sub.add_parser("completion", help="Bash/Zsh Autovervollstaendigung anzeigen oder installieren")
    comp_parser.add_argument("--shell", choices=["bash", "zsh"], default="bash")
    comp_parser.add_argument("--install", action="store_true", help="Direkt in ~/.bashrc bzw. ~/.zshrc eintragen")
    subparsers["completion"] = comp_parser

    subparsers["recipes"] = sub.add_parser(
        "recipes",
        help="Verfuegbare Rezepte auflisten",
        description="Zeigt alle Rezepte aus ./recipes, $DEPLOY_RECIPE_PATH und dem Verzeichnis von deploy.py."
    )

    subparsers["profiles"] = sub.add_parser(
        "profiles",
        help="Profile auflisten (Server, Rezept, Zugang)",
        description="Zeigt alle Profile aus ./profiles, $DEPLOY_PROFILE_PATH und dem Verzeichnis von deploy.py."
    )

    selftest_parser = sub.add_parser(
        "selftest",
        help="Lokale Selbstpruefung ohne Aenderungen",
        description="Prueft .env, SSH-Key, alle Rezepte (Quellen, Rechte, Platzhalter) und optional die SSH-Erreichbarkeit."
    )
    selftest_parser.add_argument("--ssh", action="store_true", default=False,
                                 help="Zusaetzlich alle Ziele verbinden (nur lesend)")
    subparsers["selftest"] = selftest_parser

    pu = sub.add_parser(
        "push", parents=[common],
        help="Einzelnes Skript/Datei deployen",
        description="Laedt eine Datei oder Skript atomar auf die Server hoch, erstellt User, Sudoers und SSH-Keys.",
        epilog="Beispiel: ./deploy.py push ./site-rescue --dest /usr/local/bin/site-rescue -t web1.example.com --overwrite"
    )
    pu.add_argument("file", nargs="?", default=None, help="Lokaler Pfad zum Skript")
    pu.add_argument("--dest", default=None, help="Zielpfad auf Server (z. B. /usr/local/bin/skript)")
    pu.add_argument("--mode", default=None, help="Oktale Dateirechte (Standard: 0755)")
    pu.add_argument("--owner", default=None, help="Dateieigentuemer auf Server (Standard: root)")
    pu.add_argument("--group", default=None, help="Dateigruppe auf Server (Standard: root)")
    pu.add_argument("--system-user", default=None, help="Systembenutzer erstellen/zuordnen")
    pu.add_argument("--sudoers", default=None, help="Sudoers-Regeltext (wird per visudo validiert)")
    pu.add_argument("--sudoers-name", default=None, help="Dateiname in /etc/sudoers.d/")
    pu.add_argument("--authorized-keys", default=None, help="SSH-Keys fuer Systembenutzer (direkt oder file:...)")
    pu.add_argument("--whitelist-ip", default=None, help="IP fuer Fail2ban-Whitelist")
    pu.add_argument("--test", action="append", default=None, help="Post-Deploy Testbefehl")
    pu.add_argument("--test-user", default=None, help="SSH-User fuer Test")
    pu.add_argument("--test-key", default=None, help="SSH-Key fuer Test")
    pu.add_argument("--no-test", action="store_true", default=False, help="Tests ueberspringen")
    subparsers["push"] = pu

    d = sub.add_parser("deploy", parents=[common], help="Rezept ausfuehren (recipe.toml)")
    d.add_argument("-r", "--recipe", default=S_, help="Rezeptname oder Pfad zu recipe.toml")
    d.add_argument("--no-test", action="store_true", default=False)
    d.add_argument("--force", action="store_true", default=False,
                   help="Auch unveraenderte Dateien neu schreiben (uebernimmt die Registrierung vom alten Rezept)")
    subparsers["deploy"] = d

    pl = sub.add_parser("plan", parents=[common], help="Dry-Run (zeigt geplante Aenderungen ohne Modifikation)")
    pl.add_argument("-r", "--recipe", default=S_)
    pl.add_argument("--diff", action="store_true", default=False, help="Diffs geaenderter Dateien anzeigen")
    subparsers["plan"] = pl

    st = sub.add_parser("status", parents=[common], help="Drift-Pruefung gegen Soll-Zustand")
    st.add_argument("-r", "--recipe", default=S_)
    subparsers["status"] = st

    te = sub.add_parser("test", parents=[common], help="Nur Tests ausfuehren")
    te.add_argument("-r", "--recipe", default=S_)
    subparsers["test"] = te

    ru = sub.add_parser("run", parents=[common], help="Befehl oder Skript remote ausfuehren")
    ru.add_argument("--script", default=None, help="Lokale Skriptdatei uebergeben")
    ru.add_argument("--no-sudo", action="store_true", default=False)
    ru.add_argument("command", nargs=argparse.REMAINDER, help="Befehl nach --")
    subparsers["run"] = ru

    return ap, subparsers

def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if not raw_args and sys.stdin.isatty():
        raw_args = interactive_wizard()

    ap, subparsers = make_parser()

    if raw_args and raw_args[0] in ("help", "-h", "--help"):
        if len(raw_args) > 1 and raw_args[1] in subparsers:
            subparsers[raw_args[1]].print_help()
            return 0
        if len(raw_args) == 1:
            ap.print_help()
            return 0

    args = ap.parse_args(raw_args)

    if not args.cmd or args.cmd == "wizard":
        if sys.stdin.isatty():
            return main(interactive_wizard())
        ap.print_help()
        return 0

    if args.cmd == "help-env":
        print_help_env()
        return 0

    if args.cmd == "completion":
        print_completion(args.shell, args.install)
        return 0

    if args.cmd == "recipes":
        return list_recipes()

    if args.cmd == "profiles":
        return list_profiles()

    layers = [Path.cwd() / ".env"]
    for e in getattr(args, "env", None) or []:
        layers.append(Path(os.path.expanduser(e)).resolve())

    env_cfg: dict[str, str] = {}
    for p in layers:
        env_cfg.update(parse_dotenv(p, env_cfg))

    S = dict(STD_DEFAULTS)
    S.update(env_cfg)

    prof_arg = getattr(args, "profile", None) or S.get("PROFILE", "")
    if prof_arg:
        prof_path = find_profile(str(prof_arg))
        S.update(parse_dotenv(prof_path, S))
        S["PROFILE"] = prof_path.stem

    for opt, key in (
        ("targets", "TARGETS"), ("inventory", "INVENTORY"), ("user", "SSH_USER"),
        ("port", "SSH_PORT"), ("key", "SSH_KEY"), ("jobs", "JOBS"),
        ("yes", "ASSUME_YES"), ("keep_going", "KEEP_GOING"),
        ("rollback_on_fail", "ROLLBACK_ON_FAIL")
    ):
        v = getattr(args, opt, None)
        if v not in (None, False, ""):
            S[key] = "1" if v is True else str(v)

    if getattr(args, "overwrite", False):
        S["ON_CONFLICT"] = "overwrite"
    if getattr(args, "skip_conflicts", False):
        S["ON_CONFLICT"] = "skip"

    for item in getattr(args, "set", None) or []:
        if "=" in item:
            k, v = item.split("=", 1)
            S[k.strip()] = v.strip()

    if args.cmd == "selftest":
        return cmd_selftest(S, bool(getattr(args, "ssh", False)))

    recipe = None
    if args.cmd == "push":
        for k, v in push_overrides(args, S).items():
            S.setdefault(k, v)
        recipe = load_recipe(find_recipe("push"))
    elif getattr(args, "recipe", None) or S.get("RECIPE"):
        rec_spec = getattr(args, "recipe", None) or S["RECIPE"]
        recipe = load_recipe(find_recipe(rec_spec))
    elif args.cmd not in ("run", "wizard", "help-env", "completion"):
        sys.exit("Fehler: Weder Rezept (-r) noch DEPLOY_SRC in .env angegeben.")

    inv_path = Path(os.path.expanduser(S["INVENTORY"])) if S["INVENTORY"] else (Path.cwd() / "inventory.toml" if (Path.cwd() / "inventory.toml").is_file() else None)
    inv = load_inventory(inv_path)

    if not S["TARGETS"]:
        sys.exit("Fehler: Keine Zielserver definiert (-t oder TARGETS in .env).")

    targets = resolve_targets(S["TARGETS"], S, inv)
    cfg = Cfg(
        S, {}, recipe, [], dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S"),
        tempfile.mkdtemp(prefix="dpl-work-"), tempfile.mkdtemp(prefix="dpl-ctl-"),
        len(targets), [t.name for t in targets]
    )

    runners = []
    try:
        jobs = int(S["JOBS"])
    except (TypeError, ValueError):
        raise ConfigError(f"JOBS {S['JOBS']!r} ist keine Zahl")
    if jobs < 1:
        raise ConfigError("JOBS muss mindestens 1 sein (-j 1 = nacheinander, -j 4 = Standard)")
    S["JOBS"] = str(jobs)
    live = jobs == 1
    verbose = bool(getattr(args, "verbose", False))

    for t in targets:
        tr: Transport = LocalTransport(S) if t.local else SSHTransport(t, S, cfg.ctl_dir)
        runners.append(HostRunner(cfg, t, tr, HostLog(t.name, live, verbose, []), args))

    try:
        if args.cmd in ("plan", "status", "test"):
            print_run_header(cfg, args.cmd.upper())

        if args.cmd == "run":
            cmd = list(args.command)
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            script_text = Path(os.path.expanduser(args.script)).read_text(encoding="utf-8") if args.script else None

            def run_fn(r: HostRunner) -> bool:
                r.connect()
                if script_text:
                    body = PRELUDE + script_text
                    p = r.tr.exec(body, sudo=not args.no_sudo, timeout=r.timeout)
                else:
                    p = r.sh(" ".join(q(c) for c in cmd), sudo=not args.no_sudo, strict=False)
                r.log.log("PASS" if p.rc == 0 else "FAIL", f"rc={p.rc}", p.out + p.err)
                return p.rc == 0

            run_phase(cfg, runners, run_fn, verbose)
            print_summary_table(runners)
            return 0 if all(r.ok for r in runners) else 1

        if args.cmd in ("deploy", "push"):
            return deploy_flow(cfg, runners, args)

        if args.cmd == "plan":
            run_phase(cfg, runners, lambda r: r.cmd_plan(), verbose, keep_going=True)
            print_summary_table(runners)
            return 0 if all(r.ok for r in runners) else 1

        if args.cmd == "status":
            run_phase(cfg, runners, lambda r: r.cmd_status(), verbose, keep_going=True)
            print_summary_table(runners)
            return 0 if all(r.ok for r in runners) else 1

        if args.cmd == "test":
            run_phase(cfg, runners, lambda r: r.cmd_test(), verbose, keep_going=True)
            print_summary_table(runners)
            return 0 if all(r.ok for r in runners) else 1

    finally:
        for r in runners:
            r.tr.close()
        shutil.rmtree(cfg.workdir, ignore_errors=True)
        shutil.rmtree(cfg.ctl_dir, ignore_errors=True)
        Ephemeral.cleanup()

    return 0

if __name__ == "__main__":
    try:
        rc = main()
    except ConfigError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        rc = 2
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        rc = 130
    sys.exit(rc)
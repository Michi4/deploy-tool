# deploy-tool

**Kurz:** Dateien, Benutzer und Rechte per Rezept auf viele Linux-Server
bringen - idempotent, nachvollziehbar und mit Rollback.

**Im Detail:** Ein Rezept beschreibt, *was* passiert (Pfade, Rechte,
Validierung, Tests). Ein Profil beschreibt, *wo* es passiert (Hosts, Zugang,
Werte). `deploy.py` vergleicht zuerst den Ist-Zustand per SHA-256, schreibt nur
Abweichungen, sichert die Vorgaengerdatei und rollt bei einem kritischen
Testfehler zurueck. Mehrere Hosts laufen parallel; jeder bekommt eine eigene
Zusammenfassung. Reines Python (>= 3.11, Standardbibliothek), keine
Abhaengigkeiten, keine Agents auf den Servern.

## Zwei Repos, ein Weg

`deploy-tool` ist die allgemeine Deployment-Seite. Das Rezept `acme-target`
bereitet Hosts fuer [acme-rollout](https://github.com/Michi4/acme-rollout) vor:
Es legt den Benutzer `acmepush`, den Forced Command und die Sudoers-Regel an,
die ein Zertifikats-Bundle annehmen. `acme-rollout` stellt die Zertifikate aus
und schickt sie ueber genau diesen Weg.

```
deploy-tool                          acme-rollout
recipes/acme-target                  acme.sh + deploy hook
        │                                     │
        │  richtet Empfaenger ein             │  stellt aus und pusht
        └───────────────┐                     │
                        ▼                     ▼
                 ┌──────────────────────────────────┐
                 │ Zielhost                         │
                 │ acmepush -> acme-target          │
                 │ /etc/ssl/<name>.{crt,key,ca}     │
                 └──────────────────────────────────┘
```

Beide Repos funktionieren fuer sich. Zusammen ergeben sie den vollstaendigen
Weg von der Ausstellung bis zur Datei auf dem Webserver.

## Ebenen

```
.env                         nur PROFILE: welches Profil ohne -p gilt
profiles/<name>.env          Ziele, Zugang und Werte (nicht im Repo)
recipes/<name>/recipe.toml   Ablauf, datenlos, nur {{PLATZHALTER}}
scripts/                     Nutzlast
keys/                        Public Keys
```

Merksatz: Ein Rezept enthaelt keine Servernamen, ein Profil keine Logik.
Vorrang bei gleichem Namen: CLI > Profil > `.env` > Standard.

## Schnellstart

```bash
cp .env.example .env
cp profiles/example.env profiles/local.env
$EDITOR .env profiles/local.env

./deploy.py selftest
./deploy.py plan   -p local
./deploy.py deploy -p local
```

`-p local` legt `profiles/local.env` ueber `.env`. Ohne `-p` gilt `PROFILE` aus
`.env`. `profiles/*.env` ist von Git ausgeschlossen; nur die Dateien mit
`example` sind Vorlagen. Vor dem ersten echten Lauf `SSH_KEY`, `TARGETS` und
die Werte im Profil anpassen.

## Befehle

| Zweck | Aufruf |
|---|---|
| Menue | `./deploy.py` |
| Aenderungen zeigen | `./deploy.py plan -p local` |
| Ausrollen | `./deploy.py deploy -p local` |
| Drift pruefen | `./deploy.py status -p local` |
| Tests | `./deploy.py test -p local` |
| Lokal pruefen | `./deploy.py selftest` |
| Rezepte / Profile | `./deploy.py recipes` / `./deploy.py profiles` |
| Eine Datei | `./deploy.py push datei --dest /pfad` |
| Befehl remote | `./deploy.py run -p local -- 'uptime'` |
| Variablenhilfe | `./deploy.py help-env` |

`-j 1` bis `-j 16` steuert die Parallelitaet (Standard 4). `-t host1,host2`
zeigt einen Lauf auf andere Ziele, ohne das Profil zu aendern. `-s NAME=WERT`
ueberschreibt einzelne Werte.

## Ablauf pro Host

1. Verbindung, Programmpruefung, `[[preflight]]`
2. Soll-Ist-Vergleich per SHA-256; identische Dateien werden uebersprungen
3. Schreiben in eine temporaere Datei, Rechte setzen, `validate`, atomar per `mv`
4. Backup der Vorgaengerdatei und Eintrag im Register
5. `[[tests]]`; bei einem kritischen Fehlschlag Rollback der geaenderten Dateien
6. Zusammenfassung je Host: `STATUS` (OK/FAIL) getrennt von `TEST` (PASS/WARN/FAIL/SKIP)

`plan` und `status` schreiben nichts. `--force` erzwingt ein Update auch bei
identischem Inhalt. Wiederholte Laeufe sind gefahrlos.

## Profile

`profiles/example.env` zeigt `site-rescue-audit`, `profiles/example-acme.env`
zeigt `acme-target`. Die wichtigsten Werte:

| Wert | Bedeutung |
|---|---|
| `RECIPE`, `TARGETS` | Rezept und Zielhosts |
| `SSH_USER`, `SSH_KEY`, `SSH_PORT` | Zugang zum Zielhost |
| `SYSTEM_USER`, `SYSTEM_USER_GROUPS` | Benutzer, den das Rezept anlegt |
| `WHITELIST_IP`, `RESCUE_PUBKEY` | erlaubte Quell-IP und Public Key |
| `SUDOERS_NAME`, `SUDOERS_CONTENT` | Sudoers-Datei und Inhalt |
| `ACME_USER`, `ACME_PUSH_PUBKEY`, `ACME_PUSH_IP` | Empfaenger fuer acme-rollout |
| `SCRIPTS_DIR`, `BIN_DIR` | Nutzlast und Zielverzeichnis |
| `ALLOW_EXISTING_USER`, `ROLLBACK_ON_FAIL` | Verhalten des Laufs |

## Rezepte

| Rezept | Zweck |
|---|---|
| `acme-target` | Zertifikatsempfaenger fuer acme-rollout: Benutzer, Forced Command, Sudoers, Annahme unter `/etc/ssl` |
| `site-rescue-audit` | `site-rescue` und `site-audit` inkl. Benutzer, Sudoers, Whitelist und Tests |
| `one-script` | Vorlage: eine Datei an einen Zielpfad |
| `push` | Ad-hoc-Einzeldatei, Werte kommen von der Kommandozeile |

```bash
./deploy.py plan   -r acme-target -p example-acme
./deploy.py deploy -r acme-target -p example-acme

./deploy.py plan   -p example
./deploy.py deploy -p example
./deploy.py test   -p example -s TEST_AUDIT=1 -t web1.example.com
```

`site-audit` prueft einen Webserver nur lesend (Versionen, Updates, verdaechtige
Prozesse, CMS- und Webshell-Muster). `site-rescue` ist der Gegenpart fuer den
Betrieb: `diag`, `block`, `unblock`, `audit`, `restart-pool` - erreichbar nur
ueber den Forced Command des Benutzers `rescue`.

## Neuer Server

In `TARGETS` des Profils aufnehmen und `deploy` erneut ausfuehren. Der Lauf ist
idempotent: Fehlendes wird angelegt, Gleiches bleibt unberuehrt, Abweichendes
wird gesichert und ersetzt.

## Neues Profil

```bash
cp profiles/example.env profiles/kunde.env
$EDITOR profiles/kunde.env
./deploy.py plan -p kunde
```

Pflicht je nach Rezept: `RECIPE`, `TARGETS`, `SSH_USER`, `SSH_KEY`.
`SCRIPTS_DIR` waehlt eine andere Nutzlast fuer dasselbe Rezept.

## Neues Rezept

`recipes/one-script/` ist die Vorlage fuer eine Datei,
`recipes/site-rescue-audit/` die fuer zwei zusammenhaengende Dateien. Kopieren,
Platzhalter benennen, Skripte in `scripts/` legen, `selftest` laufen lassen.

Wichtige Felder pro `[[files]]`:

| Feld | Bedeutung |
|---|---|
| `src`, `dest` | Quelle und Ziel, Platzhalter erlaubt |
| `mode`, `owner`, `group` | Rechte; Sudoers-Dateien `0440` |
| `validate` | Pruefung vor dem Schreiben (`bash -n {{FILE}}`, `visudo -cf {{FILE}}`) |
| `on_conflict` | `fail`, `keep` oder `overwrite` |
| `unless_cmd` | Schritt ueberspringen, wenn der Befehl Erfolg meldet |
| `description` | Klartext in der Ausgabe |

## Tests

`selftest` prueft Rezepte, Platzhalter, Quellen, Rechte und Checksummen, ohne
einen Server zu beruehren. `selftest --ssh` ergaenzt einen lesenden
Verbindungstest. `test` fuehrt die `[[tests]]` eines Rezepts aus;
`critical = false` macht einen Fehlschlag zur Warnung, `-s TEST_AUDIT=1`
aktiviert optionale Langlauftests.

## Sicherheit

- Profile und `.env` enthalten Zugangsdaten und sind nicht im Repo.
- `keys/` enthaelt nur Public Keys; der private Key bleibt lokal.
- Sudoers-Dateien werden vor dem Aktivieren mit `visudo -cf` geprueft.
- Aenderungen werden gesichert; kritische Testfehler loesen einen Rollback aus.
- Der Empfaenger `acme-target` akzeptiert nur `status` und `install <name>` und
  schreibt ausschliesslich nach `/etc/ssl`.

## Layout

```
deploy.py                     CLI
.env.example                  Vorlage fuer PROFILE
profiles/example.env          Beispiel: site-rescue-audit
profiles/example-acme.env     Beispiel: acme-target
recipes/acme-target/          Zertifikatsempfaenger
recipes/site-rescue-audit/    site-rescue + site-audit
recipes/one-script/           Vorlage
recipes/push/                 Einzeldatei
scripts/acme-target           Empfaenger-Skript
scripts/site-audit            lesende Wochenpruefung
scripts/site-rescue           Betriebshilfe
scripts/example.sh            Platzhalter
keys/example_key.pub          Wegwerf-Public-Key fuer die Beispiele
```

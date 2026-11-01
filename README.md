# FlashTool

Tool de reprogramare ECU prin UDS (ISO 14229-1) pe CAN, CAN FD și DoIP, gândit pentru interfețe Vector VN.
Arhitectura e descrisă în [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Stadiu: fazele F1–F6 implementate, cu excepția ZenZefi și a job-urilor OEM `.jar`, care depind de informații încă lipsă (vezi capitolul de la final). Tot ce e descris mai jos rulează pe macOS, Linux și Windows, fără hardware, folosind ECU-ul simulat inclus.

## Instalare (macOS / Linux)

```bash
python3.12 -m venv .venv            # sau: uv venv -p 3.12 .venv
.venv/bin/pip install -e ".[gui,dev]"
```

## Încercare rapidă pe Mac

```bash
# 5 ECU-uri simulate: bcm + door pe același bus CAN (door din PDX), seat pe LIN (din VBF),
# gateway pe CAN FD, adas pe DoIP cu certificat (0x29)
.venv/bin/flashtool flash examples/demo/demo.ftproj

# tratarea erorilor: ECU-ul simulat răspunde cu NRC 0x72 la al 5-lea TransferData, blocul se reia de la erase
.venv/bin/flashtool flash examples/demo/faults.ftproj -v

# interfața grafică
.venv/bin/flashtool gui examples/demo/demo.ftproj
```

Fiecare rulare scrie în `examples/demo/out/<target>/<dată>/`:

| Fișier | Conținut |
|---|---|
| `report.html` / `report.json` | rezultat, fișiere + SHA-256, blocuri + CRC32, pași executați cu durate și erori |
| `trace.txt` | toate request-urile și răspunsurile UDS, cu timestamp |
| `bus.blf` | trafic CAN brut, dacă `can.bus_log` e setat (se deschide în CANalyzer/CANoe) |

Fișierul `out/audit.jsonl` e un audit log append-only, cu lanț de hash-uri. Îl verifici cu `flashtool audit-verify examples/demo/out/audit.jsonl`.

> Vitezele afișate pe bus-ul virtual nu sunt realiste: bus-ul simulat nu are limită de bitrate. Pe CAN 500k, maximul teoretic e de aproximativ 40–50 KB/s.

## Comenzi

| Comandă | Ce face |
|---|---|
| `flashtool flash PROIECT [-t ID] [-v] [--trace] [--json]` | rulează target-urile (implicit toate, în paralel) |
| `flashtool sequence PROIECT [-t ID]` | afișează secvența rezolvată (după `extends` + `patch`) |
| `flashtool inspect FIȘIER… [--base 0x…]` | segmente, CRC32 și verificări pentru hex/s19/bin |
| `flashtool hw` | canalele CAN detectate (Vector XL pe Windows, SocketCAN pe Linux) |
| `flashtool sim-doip [--port 13400]` | pornește un ECU DoIP simulat, de folosit cu alt tool |
| `flashtool sim-pki DIR` | creează un CA de test + certificat de tester, pentru ECU-ul simulat |
| `flashtool audit-verify FIȘIER` | verifică lanțul de hash-uri din audit log |
| `flashtool pack PROIECT -o X.ftpack [--sealed]` | exportă proiectul cu tot ce folosește într-un pachet |
| `flashtool unpack X.ftpack DEST` / `verify-pack X.ftpack` | despachetează (după verificare) / verifică un pachet |
| `flashtool gui [PROIECT]` | interfața grafică |

## Interfața grafică

Navigarea se face din bara laterală, iar tema light/dark se schimbă din butonul de jos, sau urmează automat setarea sistemului.

| Pagină | Ce faci acolo |
|---|---|
| **Flash** | Câte un card pentru fiecare ECU: bus, fișierele de firmware, stare, progres, viteză, timp rămas, pasul curent. **Flash all** sau **Flash** per ECU. Un release nou se trage din Finder / Explorer direct pe card (sau **Change release…**). |
| **Configure** | Lista de ECU-uri (**Add ECU** pentru CAN, CAN FD, LIN sau DoIP), apoi taburile **ECU settings** (bus, ID-uri, STmin, timpi, Seed&Key, certificate, mesaje CAN ciclice, custom actions), **Firmware** (fișiere, sesiune ODX-F, parametri 0x34 și blocurile rezultate), **Sequence** și **Sequence YAML**. **Sequence editor…** deschide editorul vizual de secvențe. |
| **Trace & log** | Request-urile și răspunsurile UDS pentru ECU-ul ales, plus log-ul tuturor job-urilor. |
| **Reports** | Toate rulările, cu rezultat, durată și eroare; pentru rularea selectată, unde s-a dus timpul (graficul duratelor pe pași) și throughput-ul transferului. |
| **Hardware** | Interfețele CAN detectate. |

În secțiunea Project din bara laterală:
- **New**, **Open** și **Save** (Ctrl+S) pentru proiect;
- **Export package…** și **Import package…** pentru pachetele `.ftpack`.

Scurtături: Ctrl+L (release nou pentru ECU-ul selectat), Ctrl+E (editorul de secvențe), Ctrl+F (flash pentru ECU-ul selectat), Ctrl+Shift+F (flash all), Esc (stop).

## Funcții ca în vFlash

Comparația completă cu Vector vFlash este în [docs/VFLASH_COMPARISON.md](docs/VFLASH_COMPARISON.md).

| Funcție | Cum |
|---|---|
| **VBF** (Volvo / Geely / JLR) | Fișierul `.vbf` se adaugă ca orice firmware. Part number-ul, zonele de erase, DFI-ul și semnătura se citesc din header. CRC16 pe fiecare bloc și `file_checksum` sunt verificate. |
| **Pachete** `.ftpack` (ca `.vflashpack`) | **Export package…** sau `flashtool pack PROIECT -o x.ftpack [--sealed]`. Pachetul conține proiectul, firmware-ul, secvențele, cataloagele, Seed&Key și certificatele, plus un manifest cu SHA-256. Un pachet **sigilat** se deschide read-only: se poate face flash, dar nu se poate edita. Cheile private nu sunt incluse decât cu `--include-private-keys`. |
| **Custom actions** (ca Custom Actions VDS) | Pasul „Custom action (Python)” din editorul de secvențe apelează `run(ctx)` dintr-un fișier `.py`. `ctx` oferă `request`, `read_did`, `write_did`, `vars`, `log`, `sleep` și `args`. Pasul rulează doar dacă ECU-ul are **Allow custom actions** bifat. |
| **Mesaje CAN ciclice** | În ECU settings, câmpul Cyclic messages, de exemplu `0x500=00 01 @100`: mesajul se trimite la fiecare 100 ms cât durează flash-ul (network management, keep-alive). |
| **API Python** | `from flashtool import api; api.flash("proiect.ftproj", on_event=print)`, pentru CI/CD și bancuri de test. |

## Proiect (`*.ftproj`)

Vezi [examples/demo/demo.ftproj](examples/demo/demo.ftproj). Pentru fiecare target configurezi:

- **transport**: `can`, `canfd`, `lin` (NAD, `n_cs_ms`, `poll_interval_ms`) sau `doip`, cu ID-uri, padding, `tx_dl`, `stmin_rx`, `block_size` și opțional `stmin_tx_override_ms` (abatere de la ISO 15765-2, marcată în raport);
- **firmware**: hex/s19/bin, `.vbf` sau `.pdx`/`.odx-f` (cu `session:` când are mai multe sesiuni ODX-F); per fișier, `dfi`, `alfid`, `memory_address`, `memory_size` și `erase` pentru fișierele deja comprimate/criptate;
- **sequence**: `uds_standard` sau un template propriu care face `extends` și `patch` (vezi [examples/demo/oem_demo.yaml](examples/demo/oem_demo.yaml));
- **seed_key**: provider `python`, `library` (DLL/.so cu `GenerateKeyEx`/`GenerateKeyExOpt`) sau `bridge` (DLL pe 32 de biți încărcat de un interpretor de 32 de biți).

- **certificates**: autentificare 0x29 (APCE). Provider `file` (dezvoltare) sau `python` (adaptor către un serviciu de certificate, de exemplu ZenZefi, când avem API-ul). Cheile rămân în procesul broker-ului de credențiale; job-urile primesc doar semnături;
- **catalog**: servicii numite din ODX-D/PDX sau dintr-un YAML, folosite în template-uri ca `service: Fingerprint_Write`.

Target-urile care au același `can.backend` + `can.channel` formează un **grup de bus**: rulează în același proces, iar pașii marcați `group: once` (10 03, 85 02, 28 03 și cei de final) se trimit o singură dată pentru tot bus-ul, după ce toate ECU-urile au ajuns la ei.

STmin se poate schimba și per pas, de exemplu doar pentru download:

```yaml
patch:
  - update: download
    with: { transport: { stmin_rx: 0xF5 } }   # 500 µs
```

## Hardware real

Pe Windows, cu driverele Vector instalate (aplicația `FlashTool` configurată în Vector Hardware Config):

```yaml
simulate: false
can: { backend: vector, channel: 0, app_name: FlashTool, bitrate: 500000, tx_id: 0x7E0, rx_id: 0x7E8 }
```

Pe Linux: `backend: socketcan, channel: can0`. Pentru DoIP: `simulate: false` și `doip: { ecu_ip: 192.168.0.10, ... }`.

## Teste și trasabilitate

```bash
.venv/bin/python -m pytest --trace-json=out/trace.json      # unit + integrare cu ECU-urile simulate
.venv/bin/python tools/traceability.py out/trace.json -o out/traceability.md   # matrice cerințe -> teste
.venv/bin/lint-imports                                       # regulile dintre straturi (docs/ARCHITECTURE.md §3)
```

Cerințele sunt în [docs/requirements.yaml](docs/requirements.yaml), iar draftul Tool Safety Manual e în [docs/TOOL_SAFETY_MANUAL.md](docs/TOOL_SAFETY_MANUAL.md).

## Aplicația stand-alone

```bash
.venv/bin/python packaging/build.py     # dist/flashtool/ (fără Python instalat), dist/sbom.cdx.json, dist/SHA256SUMS
./dist/flashtool/flashtool              # fără argumente deschide GUI-ul
```

Build-ul se face pe fiecare sistem țintă (Windows, Linux, macOS). Pe Windows, [packaging/windows/flashtool.iss](packaging/windows/flashtool.iss) produce installer-ul (Inno Setup, cu semnare de cod).

## Ce nu e încă implementat

| Funcție | De ce |
|---|---|
| Adaptorul ZenZefi | Lipsește documentația API-ului ZenZefi (Q-10). Punctul de integrare există: provider `python` pentru certificate. |
| Job-uri OEM `.jar` | E nevoie de un exemplu real ca să știm ce API MCD-3D folosesc (Q-11). |
| Import `.cdd` | Formatul CANdela nu e public; parserul trebuie validat pe fișiere reale (Q-08). Până atunci: ODX-D sau catalog YAML. |
| Validare pe hardware Vector | Calea Vector XL (CAN și LIN) nu a putut fi testată fără VN și ECU (REQ-05). |

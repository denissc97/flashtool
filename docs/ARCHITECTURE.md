# FlashTool — Arhitectură (Faza 0, spre revizuire)

> Status: **DRAFT v2 pentru review** — include răspunsurile din runda 1 (§15).
> Fiecare decizie marcată **[D-xx]** poate fi contestată; fiecare întrebare **[Q-xx]** deschisă are nevoie
> de un răspuns înainte de faza în care e folosită.

---

## 1. Scop

Aplicație desktop **de sine stătătoare** (Windows + Linux, fără instalare de Python) pentru reprogramarea
ECU-urilor, generică (fără logică specifică unui OEM în nucleu), similară funcțional cu Vector vFlash.

| ID | Cerință | Detaliu |
|---|---|---|
| REQ-01 | Fișiere de firmware | `.bin` (cu adresă de bază), `.hex`, `.s19/.srec`, `.pdx` / `.odx-f`; inclusiv fișiere **deja comprimate/criptate** |
| REQ-02 | Bus-uri | CAN clasic, CAN FD (ISO 15765-2), LIN (ISO 17987 / LIN-TP), Ethernet (DoIP, ISO 13400-2) |
| REQ-03 | Protocol | UDS (ISO 14229-1), secvența standard de programare + secvențe custom |
| REQ-04 | Personalizare | Template-uri de secvență, nume simbolice din `.cdd` / ODX-D, **flash job-uri OEM `.jar`** |
| REQ-05 | Hardware | Toată familia Vector VN (CAN/CAN FD/LIN/Ethernet), detectată automat |
| REQ-06 | Securitate ECU | Seed&Key (DLL 32/64-bit, `.so`, Python) **și** autentificare pe certificate (UDS 0x29) cu certificate din **ZenZefi** |
| REQ-07 | Paralelism | Flashing simultan pe mai multe ECU-uri, prin mai multe canale/interfețe |
| REQ-08 | Timing | STmin, BS, P2/P2\*, S3 configurabile, inclusiv per pas |
| REQ-09 | UI | GUI complet + CLI pentru automatizare |
| REQ-10 | Reglementare | Dezvoltat și documentat conform cerințelor automotive (§13) |

### În afara scopului (deocamdată)
- KWP2000 / K-Line, XCP, J2534.
- CAN XL — vezi [Q-14].
- Editor grafic de ODX.

---

## 2. Decizii tehnologice

**[D-01] Python 3.12 + PySide6 (Qt 6)** — ✅ confirmat.
Se livrează ca aplicație stand-alone (PyInstaller: interpretor + biblioteci incluse, JRE inclus pentru job-urile `.jar`).

**[D-02] Biblioteci externe** (toate sunt SOUP — se fixează versiunile și se evaluează conform §13)

| Bibliotecă | Rol | Licență |
|---|---|---|
| `python-can` 4.x | CAN/CAN FD: Vector XL (Windows), SocketCAN (Linux), `virtual` (teste), loguri BLF/ASC | LGPL-3.0 |
| `can-isotp` 2.x | ISO 15765-2 (segmentare, flow control, CAN FD) | MIT |
| `doipclient` | DoIP: vehicle identification, routing activation, diagnostic messages | MIT |
| `bincopy` | Parsare/îmbinare bin/hex/s19 | MIT |
| `odxtools` | Codare/decodare ODX-D (necesar pentru catalog și pentru job-urile Java) | MIT |
| `cryptography` | X.509, verificare semnături, TLS (DoIP securizat) | Apache-2.0 / BSD |
| `JPype1` | Rularea job-urilor Java (`.jar`) într-un JVM inclus | Apache-2.0 |
| `PySide6` | GUI | LGPL-3.0 |
| `PyYAML`, `lxml` | Proiecte, template-uri, CDD/ODX | MIT / BSD |

**[D-03] UDS client propriu** (nu `udsoncan`): control complet asupra request-urilor brute, P2/P2\*, NRC 0x78,
adresare funcțională/fizică, suprimarea răspunsului pozitiv și serviciul 0x29.

**[D-04] Strat HAL propriu peste Vector XL Driver Library (`vxlapi`).** Verificat în `python-can` 4.6.1:
backend-ul Vector folosește `ctypes.windll` → **doar Windows** și **fără LIN**. Deci:
- CAN/CAN FD pe Windows → `python-can` (backend `vector`).
- LIN → wrapper `ctypes` propriu peste `vxlapi`.
- Enumerarea și capabilitățile canalelor (CAN FD, LIN, Ethernet) → wrapper propriu (`xlGetDriverConfig`).
- Linux + VN → vezi R-02.

**[D-05] Fiecare flash job rulează într-un proces separat** (nou, din REQ-07).
Motive: (1) paralelismul real ocolește GIL-ul Python, deci timing-ul ISO-TP al unui ECU nu e afectat de celelalte;
(2) un DLL Seed&Key sau un job Java care se blochează ori crapă nu oprește GUI-ul și nici celelalte job-uri;
(3) același mecanism servește bridge-ul pentru DLL-uri 32-bit.

---

## 3. Vedere de ansamblu

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ Prezentare      GUI (PySide6)                          CLI (flashtool ...)     │
├──────────────────────────── IPC (evenimente + comenzi) ───────────────────────┤
│ Aplicație       JobManager ── planifică N FlashJob-uri, grupuri de bus,        │
│ (proces GUI)    alocă canale HW, colectează rapoarte, audit log                │
│                 CredentialBroker ── ZenZefi / PKI / Seed&Key (serializat)      │
├───────────────────────────────────────────────────────────────────────────────┤
│ FlashJob        Sequence Engine ◄── Template YAML (extends/patch)              │
│ (proces worker  │   ▲  ◄── Java Job Runtime (.jar, JVM inclus)                 │
│  per ECU)       │   └── Diag catalog (CDD / ODX-D)                             │
│                 ▼                                                              │
│                 UDS Client ── SecurityAccess (0x27) · Authentication (0x29)    │
│                 │                                                              │
│                 Firmware Image ◄── BIN · HEX · S19 · PDX/ODX-F                 │
├───────────────────────────────────────────────────────────────────────────────┤
│ Transport       ITransport:  IsoTp(CAN/CAN FD) · LinTp · DoIP(+TLS)            │
├───────────────────────────────────────────────────────────────────────────────┤
│ HAL             Vector XL (CAN, CAN FD, LIN, enumerare) · SocketCAN ·          │
│                 Virtual · NIC sistem (inclusiv VN5xxx ca adaptor de rețea)     │
└───────────────────────────────────────────────────────────────────────────────┘
```

Reguli de dependență (verificate automat cu `import-linter` în CI):
1. Un strat depinde **doar** de straturile de sub el.
2. `domain/` nu importă nimic din `gui/` sau din biblioteci hardware.
3. GUI și CLI sunt clienți egali ai stratului de aplicație — tot ce poate GUI-ul poate și CLI-ul.
   (În contractul `import-linter`, CLI-ul stă deasupra GUI-ului doar pentru că `flashtool gui` îl lansează.)

---

## 4. Module

### 4.1 Transport (`flashtool/transport/`)

```python
class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, payload: bytes, functional: bool = False) -> None: ...
    def recv(self, timeout: float) -> bytes | None: ...    # un PDU UDS complet
    def set_params(self, **overrides) -> None: ...          # ex. STmin per pas
    @property
    def max_payload(self) -> int: ...
```

| Implementare | Parametri configurabili |
|---|---|
| `IsoTpCanTransport` | bitrate nominal / data (FD), sample point, ID-uri fizice/funcționale (11/29 bit), adresare normal/extended/mixed, padding, `tx_dl` (8…64), BS, STmin, N_As/N_Bs/N_Cr, `wftmax` |
| `LinTpTransport` | baudrate, NAD, ID 0x3C/0x3D, checksum classic/enhanced, perioada de polling 0x3D, P2/P2\* LIN |
| `DoIpTransport` | IP / vehicle discovery, logical address tester/ECU/funcțională, activation type, TLS (port 3496), versiune protocol |

**STmin (REQ-08)** — două valori distincte, ambele configurabile:

| Parametru | Sens | Configurabil la nivel de |
|---|---|---|
| `stmin_rx` | STmin trimis **de tester** în propriile Flow Control (cât de repede ne trimite ECU-ul) | proiect, pas |
| `stmin_tx_override` | Ignoră STmin primit de la ECU și folosește valoarea dată (**abatere de la ISO 15765-2**; dezactivat implicit, apare ca avertisment în raport) | proiect, pas |

Valori acceptate: 0–127 ms și 100–900 µs (0xF1–0xF9). Precizia sub 1 ms pe Windows, din Python, nu e garantată (risc R-01).

### 4.2 UDS Client (`flashtool/uds/`)

- Timing: P2client, P2\*client, S3 (Tester Present), reîncercări — toate configurabile.
- NRC `0x78 ResponsePending` → reîncarcă P2\* (cu un număr maxim de reîncercări).
- Gestionare explicită a `suppressPosRspMsgIndicationBit` și a adresării fizice/funcționale.
- Tester Present pe timer separat, suspendat cât timp un request e în curs.
- Servicii: 0x10, 0x11, 0x14, 0x19, 0x22, 0x23/0x35 (citire înapoi), 0x27, **0x29**, 0x28, 0x2E, 0x31, 0x34, 0x36, 0x37, 0x3E, 0x85.
- Fiecare request/response trece prin `TraceSink`.

### 4.3 Firmware Image (`flashtool/firmware/`)

```python
@dataclass
class Segment:      address: int; data: bytes
@dataclass
class LogicalBlock:                          # = o unitate de erase/download
    name: str
    segments: list[Segment]
    erase_ranges: list[tuple[int, int]]
    data_format_identifier: int = 0x00       # DFI din 0x34 (nibble compresie | nibble criptare)
    memory_size: int | None = None           # memorySize din 0x34 dacă diferă de lungimea datelor
    signature: bytes | None = None
    checksum: bytes | None = None
    metadata: dict
    type: str = "APP"                        # BOOT / APP / CAL / DATA / ...: alege block sequence-ul
    index: int | None = None                 # logical block index (erase după număr de bloc)
@dataclass
class FirmwareImage:  blocks: list[LogicalBlock]; source: str; sha256: str
```

**Mai multe logical blocks per ECU (REQ-15).** Fiecare fișier din proiect dă unul sau mai multe blocuri, flash-uite în
ordinea din proiect. Tipul unui bloc vine, în ordinea priorității, din `type` al intrării din proiect, din fișier
(ODX-F `DATABLOCK TYPE`, VBF `sw_part_type`) sau e `APP`. Aliasuri: `CODE`/`EXE` → APP, `FBL`/`BOOTLOADER` → BOOT,
`CALIBRATION` → CAL; celelalte tipuri (SBL, CARCFG...) rămân cum sunt scrise. `block_index` vine din proiect sau din
ODX-F `LOGICAL-BLOCK-INDEX`; două blocuri cu același index sunt eroare de validare.

**Fișiere deja comprimate/criptate (REQ-01).** Tool-ul **nu** comprimă și **nu** criptează. Trimite octeții exact
cum sunt în fișier și setează parametrii din 0x34 din configurație:

| Parametru per bloc | Implicit | Sursă alternativă |
|---|---|---|
| `data_format_identifier` (DFI) | `0x00` | proiect / template / ODX-F `ENCRYPT-COMPRESS-METHOD` |
| `address_and_length_format` (ALFID) | `0x44` | proiect / template |
| `memory_address` | adresa din fișier | override explicit (fișierele comprimate pot avea altă adresă decât ținta) |
| `memory_size` | lungimea datelor transferate | override explicit / ODX-F `UNCOMPRESSED-SIZE` — vezi [Q-12] |
| `erase_ranges` | acoperirea segmentelor | override explicit (la date comprimate, zona de șters ≠ lungimea fișierului) |

| Loader | Note |
|---|---|
| BIN | Necesită adresă de bază; opțional împărțire după harta de memorie |
| HEX / S19 | `bincopy`; segmentele discontinue rămân separate; opțiune „fill gaps” |
| PDX | ZIP → `index.xml` → `.odx-f`/`.odx-d` + date + eventual `.jar` (§4.6). ODX-F: `ECU-MEM/MEM/SESSIONS` → `DATABLOCKS/SEGMENTS` → `FLASHDATAS` (intern/extern, `DATAFORMAT`, `ENCRYPT-COMPRESS-METHOD`) + `SECURITYS` |

Validări înainte de comunicare: suprapuneri, harta de memorie, aliniere, hash-uri.

### 4.4 Diag catalog (`flashtool/diagdb/`)

Construiește pași **după nume** și validează parametrii.

| Sursă | Abordare |
|---|---|
| `.cdd` (Vector CANdela) | Parser XML *best-effort* (formatul nu e public) — validare pe fișiere reale |
| ODX-D (din PDX) | `odxtools` (format standard ASAM) |
| Manual | Extensii direct în template |

### 4.5 Sequence Engine (`flashtool/sequence/`)

Secvența este **date**, nu cod. Tipuri de pași:

| Pas | Ce face |
|---|---|
| `request` | Request UDS după nume din catalog sau ca bytes brute |
| `session` | 0x10 + `wait_ecu` opțional |
| `security` | 0x27 seed → provider → key |
| `authenticate` | 0x29 cu certificate (§4.7) |
| `download` | 0x34 (DFI, ALFID, memorySize) → 0x36 × N (BSC rulant) → 0x37 |
| `program_block` | Rulează block sequence-ul tipului blocului (`block_sequences[block.type]`, altfel `default`) |
| `java_job` | Rulează un job `.jar` OEM (§4.6) |
| `foreach`, `if`, `call` | Control al fluxului |
| `tester_present`, `delay`, `wait_ecu`, `read_into`, `prompt` | Utilitare |
| `barrier` | Sincronizare între job-uri paralele de pe același bus (§4.8) |

Fiecare pas are: `expect`, `timeout`, `transport` (override STmin/BS etc.), `on_fail: abort | continue | retry(n) | goto(id)` și `store`.
Expresiile sunt evaluate restricționat (fără `eval` Python).

**Moștenire de template-uri:**

```yaml
extends: uds_standard
catalog: oem_x_ecu.cdd
variables: { security_level: 0x11, check_memory_rid: 0x0202 }
patch:
  - replace: security
    with: { authenticate: { method: apce_unidirectional, provider: zenzefi } }
  - insert_after: security
    steps:
      - request: "Fingerprint_Write"
        params: { date: "${today}", tester_id: 0x1234 }
  - remove: dtc_off
transport_overrides:
  download: { stmin_rx: 0, block_size: 0 }     # STmin diferit doar în timpul transferului
```

**Block sequences (REQ-15).** Pașii de programare a unui bloc stau în `block_sequences`, câte o listă per tip de bloc.
`uds_standard` definește doar `default` (erase după adresă → download → check memory), iar bucla `foreach block`
conține un singur pas `program_block`. Un template OEM adaugă listele care diferă:

```yaml
extends: uds_standard
block_sequences:
  BOOT: [ ... activare bootloader, reset, 10 03, 10 02, 27 ... ]   # bootloader update
  CAL:  [ ... erase cu ${u8(block.index)} ... ]                   # erase după număr de bloc
  DATA: null                                                       # null: șterge lista moștenită
```

Reguli:
- id-urile de pași sunt unice în toată secvența, deci și între block sequences;
- `patch` caută și în block sequences;
- un block sequence nu poate conține `program_block` sau `group: once`.

Înainte de conectare, `prepare()` oprește job-ul dacă un bloc are un tip fără listă proprie și fără `default`. Tot atunci
avertizează la liste goale și la block sequences pe care niciun `program_block` nu le folosește. În trace și în raport,
calea pasului arată lista folosită, de exemplu `blocks[calibration]/program[CAL]/cal_erase`.

### 4.6 Flash job-uri OEM `.jar` (`flashtool/javajobs/`) — nou

Ipoteză de lucru, de confirmat ([Q-11]): `.jar`-urile sunt **job-uri Java ODX/ASAM MCD-3D** (`SINGLE-ECU-JOB` cu
`PROG-CODE` de tip JAR), scrise pentru a rula într-un **D-Server MCD-3D**. Asta înseamnă că job-ul apelează un API
Java standard (logical link, diag service, request/response cu parametri decodați ODX), iar tool-ul trebuie să ofere
acel API.

| Opțiune | Pro | Contra |
|---|---|---|
| **A. Runtime propriu**: JVM inclus (JPype) + implementare a subsetului de API MCD-3D folosit de job-uri, conectată la clientul nostru UDS și la `odxtools` | Fără dependențe comerciale; se integrează cu secvențele, paralelismul și trace-ul nostru | Efort mare; necesită definițiile API-ului ASAM MCD-3D (standard cu licență); conformitatea trebuie testată pe job-uri reale |
| B. Integrare cu un D-Server comercial existent | Compatibilitate maximă | Licență per stație, dependență de furnizor, aplicația nu mai e cu adevărat stand-alone |
| C. Conversie manuală a job-ului în template YAML | Simplu | Nu e generic; trebuie refăcut la fiecare versiune a job-ului |

**Recomandare:** A, dar abia după un **spike de fezabilitate** (2–3 săptămâni): analizăm 2–3 `.jar`-uri reale,
inventariem interfețele folosite și estimăm subsetul de API necesar. Până atunci, arhitectura păstrează
`java_job` ca tip de pas izolat, așa că restul tool-ului nu depinde de rezultatul spike-ului.

Rulare izolată: JVM-ul pornește în procesul FlashJob (D-05), cu timeout și fără acces la rețea sau fișiere în afara directorului proiectului.

### 4.7 Securitate ECU (`flashtool/security/`)

#### 4.7.1 Seed & Key (0x27)

| Provider | Platformă | Note |
|---|---|---|
| DLL 64-bit (`GenerateKeyEx` / `GenerateKeyExOpt`) | Windows | Încărcat în procesul job-ului |
| DLL 32-bit | Windows | Proces helper 32-bit, comunicare prin pipe; tipul DLL-ului e detectat automat din headerul PE |
| `.so` (aceeași semnătură C) | Linux | Încărcat în procesul job-ului |
| Plugin Python | Ambele | `generate_key(seed, level, variant) -> bytes` |

#### 4.7.2 Autentificare pe certificate (0x29) + ZenZefi

UDS 0x29 (ISO 14229-1:2020) face schimb de certificate X.509 și dovedește posesia cheii private prin semnarea unui
challenge primit de la ECU. Fluxul tipic (APCE, unidirecțional):

```
Tester                         CredentialBroker / ZenZefi                    ECU
  │ 29 08 authenticationConfiguration ─────────────────────────────────────►│
  │◄───────────────────────────────────────────────────── config (APCE) ─────│
  │── cere certificat pentru ECU/rol ──►│                                     │
  │◄─────────── certificat (+ lanț) ────│                                     │
  │ 29 01 verifyCertificateUnidirectional(cert, challengeClient) ──────────►│
  │◄──────────────────────────────────────────────── challengeServer ────────│
  │── sign(challengeServer) ───────────►│  cheia privată NU părăsește        │
  │◄──────────────── proofOfOwnership ──│  ZenZefi / HSM                     │
  │ 29 03 proofOfOwnership(pop) ───────────────────────────────────────────►│
  │◄───────────────────────────────────────── sessionKeyInfo / OK ───────────│
```

```python
class CertificateProvider(Protocol):
    def get_certificate_chain(self, target: EcuIdentity, role: str) -> list[x509.Certificate]: ...
    def sign_challenge(self, target: EcuIdentity, challenge: bytes) -> bytes: ...   # semnătura, nu cheia
    def status(self) -> ProviderStatus: ...                                          # conectat, valabilitate, roluri
```

| Provider | Note |
|---|---|
| `ZenZefiProvider` | Conectare la clientul ZenZefi local (API-ul exact trebuie confirmat din documentația ZenZefi — [Q-10]); listare certificate disponibile, valabilitate, semnare challenge |
| `Pkcs11Provider` | Token / smartcard / HSM |
| `FileProvider` | Certificat + cheie din fișier (doar dezvoltare/simulator; dezactivabil prin politică) |

**Principiu de design:** cheile private nu sunt niciodată citite de tool; providerul semnează. Toate operațiile
cu certificate trec prin `CredentialBroker`, în procesul principal: serializează accesul la ZenZefi pentru
job-urile paralele, păstrează cache-ul de certificate și scrie fiecare utilizare în audit log.

### 4.8 Flashing paralel (`flashtool/app/jobs.py`) — nou

```
JobManager
 ├─ BusGroup "CAN1 @ VN1640#1 ch1"      ← ECU-uri pe același bus fizic
 │    ├─ faza funcțională comună (10 03, 85 02, 28 03) — o singură dată
 │    ├─ FlashJob ECU_A (proces) ┐
 │    ├─ FlashJob ECU_B (proces) ┼─ faza fizică în paralel
 │    └─ barrier → faza funcțională de final (28 00, 85 01, 10 01) — o singură dată
 ├─ BusGroup "CAN FD2 @ VN1640#1 ch2" ...
 └─ BusGroup "DoIP @ eth0" ...
```

- Un `BusGroup` grupează ECU-urile de pe același bus fizic. Request-urile **funcționale** se trimit o singură dată
  pe grup (altfel `28 03` de la un job ar perturba celelalte), iar fazele fizice rulează în paralel.
- Canalele hardware sunt alocate exclusiv de `JobManager` (un canal = un proces proprietar). Pentru ECU-uri multiple
  pe același canal CAN, procesul de bus face multiplexare după ID-uri.
- Limite configurabile: număr maxim de job-uri simultane, prioritate și politica de eroare (un ECU eșuat nu
  oprește celelalte, cu excepția fazei funcționale comune).

### 4.9 Proiect de flash (`*.ftproj`)

```yaml
name: BCM_FrontLeft_SW_1.4.2
targets:
  - id: bcm_fl
    transport: canfd
    hardware: { backend: vector, serial: 12345, channel: 1 }
    can: { bitrate: 500000, data_bitrate: 2000000, tx_dl: 64,
           tx_id: 0x7E0, rx_id: 0x7E8, func_id: 0x7DF, stmin_rx: 0, block_size: 0, padding: 0xCC }
    uds: { p2_ms: 50, p2_star_ms: 5000, tester_present_s: 2.0 }
    firmware:
      - { file: app_compressed.hex, dfi: 0x11, memory_size: 0x00060000 }
    sequence: templates/oem_x.yaml
    security: { seed_key: { provider: dll, path: seedkey/oem_x.dll },
                certificates: { provider: zenzefi, role: supplier_flash } }
  - id: door_fr
    ...                                     # al doilea ECU, rulat în paralel
```

Proiectele de producție pot fi **semnate și blocate** (§13): un operator le poate rula, dar nu le poate modifica.

### 4.10 Aplicație: evenimente, rapoarte, audit

- Comunicare GUI ↔ job-uri prin IPC (`multiprocessing` + mesaje tipizate): `StepStarted`, `StepFinished`,
  `Progress`, `UdsTraffic`, `Log`, `JobFinished`.
- Cancel în puncte sigure, cu cleanup garantat.
- Artefacte per rulare: `trace.log`, `bus.blf` (opțional), `report.json` + `report.html` (secvența rezolvată,
  parametri efectivi, hash-uri, versiuni SW citite înainte/după, certificate folosite).
- **Audit log** append-only cu lanț de hash-uri (orice modificare ulterioară se detectează): cine, ce proiect,
  ce ECU (VIN/serial), ce fișiere (SHA-256), ce rezultat.

### 4.11 GUI (`flashtool/gui/`)

| Ecran | Conținut |
|---|---|
| Project | Țintele (ECU-urile), fișierele firmware cu blocurile și adresele, parametrii 0x34 per bloc |
| Sequence | Arborele secvenței rezolvate, editare pași, catalog CDD/ODX cu drag & drop |
| Hardware | Toate canalele VN detectate, cu capabilități (CAN FD / LIN / Ethernet); maparea țintă ↔ canal |
| Security | Status ZenZefi (conectat, certificate, valabilitate), providere Seed&Key |
| Flash | Grilă cu N ECU-uri în paralel: progres per bloc, KB/s, ETA, stare |
| Trace | Log UDS live per ECU, cu filtre și NRC decodat |
| Users | Roluri (operator / inginer / admin), dacă politica le cere ([Q-16]) |

---

## 5. Secvența UDS standard (template `uds_standard.yaml`)

RID-urile marcate cu * sunt variabile (diferă între OEM-uri).

| # | Fază | Request | Adresare | Notă |
|---|---|---|---|---|
| 1 | Pre | `3E 80` periodic | funcțional | Tester Present ON |
| 2 | Pre | `10 03` | funcțional | Extended session |
| 3 | Pre | `31 01 02 03`* | fizic | Check programming preconditions (opțional) |
| 4 | Pre | `85 02` | funcțional | DTC setting OFF |
| 5 | Pre | `28 03 01` | funcțional | Disable Rx/Tx normal msgs |
| 6 | Prog | `10 02` | fizic | Programming session → `wait_ecu` |
| 7 | Prog | `27 xx/xx+1` **sau** `29 ...` | fizic | Security access sau autentificare pe certificate |
| 8 | Prog | `2E F1 5A`* | fizic | Fingerprint (opțional) |
| 9 | Prog | `program_block` per bloc | | Pașii 9a–9e sunt `block_sequences.default`; tipurile cu listă proprie (BOOT, CAL...) o rulează pe a lor |
| 9a | Prog | `31 01 FF 00 <ALFID> <addr> <len>` | fizic | Erase memory |
| 9b | | `34 <DFI> <ALFID> <addr> <memorySize>` | fizic | Request download |
| 9c | | `36 <BSC> <data>` × N | fizic | Transfer data |
| 9d | | `37` | fizic | Request transfer exit |
| 9e | | `31 01 02 02 <sig/CRC>`* | fizic | Check memory |
| 10 | Prog | `31 01 FF 01` | fizic | Check programming dependencies |
| 11 | Prog | `11 01` | fizic | Hard reset → `wait_ecu` |
| 12–16 | Post | `10 03`, `28 00 01`, `85 01`, `14 FF FF FF`, `10 01` | funcțional | Rulează și ca cleanup la eșec |

---

## 6. Flux de date — exemplu CAN FD

```
GUI ──► JobManager ──spawn──► FlashJob proces (ECU_A)
                                 │ Project → FirmwareImage (validat)
                                 │ Template + extends + CDD → Sequence (rezolvată)
                                 ▼
                              Engine ──► UdsClient ──► IsoTpCanTransport ──► python-can (vector)
                                 │            ▲                                 └► VN16xx ch1 ──► ECU_A
                                 │            └─ 0x29: CredentialBroker (IPC) ──► ZenZefi
                                 └──► evenimente (IPC) ──► GUI / CLI / raport / audit
```

---

## 7. Structura repository-ului

```
Flash_Tool/
├─ flashtool/
│  ├─ hal/            vector_xl.py · enumerate.py · backends.py
│  ├─ transport/      base.py · isotp_can.py · lin_tp.py · doip.py · factory.py
│  ├─ uds/            client.py · services.py · auth29.py · nrc.py · tester_present.py · trace.py
│  ├─ firmware/       model.py · loaders.py · pdx.py · odx_flash.py · validate.py
│  ├─ diagdb/         catalog.py · cdd.py · odx_d.py
│  ├─ security/       seedkey/ (dll, bridge32, so, python) · certs/ (broker, zenzefi, pkcs11, file)
│  ├─ javajobs/       runtime.py · mcd_api/ (subsetul MCD-3D, după spike)
│  ├─ sequence/       model.py · loader.py · expr.py · engine.py · steps/
│  ├─ app/            project.py · jobs.py · busgroup.py · ipc.py · events.py · report.py · audit.py
│  ├─ gui/            main_window.py · views/ · models/
│  └─ cli.py
├─ templates/  sim/  tests/  docs/ (ARCHITECTURE · SRS · TOOL_SAFETY_MANUAL · adr/)  packaging/
```

---

## 8. Concurență și timing

| Proces / thread | Rol |
|---|---|
| Proces GUI (main thread) | Doar UI |
| Proces GUI (thread-uri) | JobManager, CredentialBroker, IPC |
| Proces FlashJob (1 per ECU sau per bus) | Engine + UDS + transport; thread-uri proprii pentru Tester Present și Notifier/ISO-TP |
| Proces helper 32-bit | Doar pentru DLL-uri Seed&Key pe 32 de biți |

---

## 9. Tratarea erorilor și siguranță

- Validare completă înainte de start (fișiere, secvență, credențiale, valabilitatea certificatelor, hardware).
- Excepții tipizate: `TransportError`, `NegativeResponse(nrc)`, `AuthenticationError`, `SequenceError`, `ValidationError`, `JavaJobError`.
- Reluare per bloc, începând de la erase; cleanup garantat; raport scris și la eșec.
- **Dry-run** contra simulatorului intern.

---

## 10. Hardware Vector — toată familia VN

Tool-ul **nu are cod per model**: interoghează driverul (`xlGetDriverConfig`) și folosește capabilitățile raportate
de fiecare canal (CAN FD, LIN, Ethernet). Matricea de test include câte un reprezentant pe categorie:

| Categorie | Exemple | Bus-uri folosite |
|---|---|---|
| VN16xx | VN1610/1630/1640/1670 | CAN, CAN FD, LIN (prin piggyback) |
| VN56xx | VN5610A/5620/5640 | Ethernet (DoIP prin adaptorul de rețea virtual Vector) |
| VN7xxx / VN8xxx | VN7640, VN8900 | CAN FD, LIN, Ethernet, după configurație |

Ce suportă exact fiecare model se confirmă din documentația Vector, în timpul F1 (R-02).

---

## 11. Strategie de verificare

| Nivel | Ce | Cum |
|---|---|---|
| Unit | Loaders, ODX-F/CDD, expresii, moștenire template, 0x34/0x36, 0x29, STmin | `pytest`, trasabil la REQ-xx |
| Integrare | Flash end-to-end, inclusiv paralel | **ECU simulat** configurabil (bootloader UDS pe CAN virtual, DoIP localhost, 0x27/0x29 cu PKI de test, injectare erori) |
| HIL | Hardware real | Câte un ECU de referință per tip de bus × câte un VN per categorie; teste `@hil` |
| Arhitectură | Regulile din §3 | `import-linter` |
| Performanță | Throughput | ≥ 80% din maximul teoretic pe CAN 500k și CAN FD 2M, 1 ECU și 4 ECU în paralel |

„Generic” (răspunsul la Q-01) înseamnă că nucleul nu conține logică OEM. Pentru dovezi de validare (§13) e totuși
nevoie de ECU-uri de referință reale, iar simulatorul se configurează să emuleze variantele întâlnite.

---

## 12. Packaging (aplicație stand-alone)

- PyInstaller one-folder (interpretor + biblioteci + JRE + helper 32-bit) → Windows: installer semnat
  (Authenticode); Linux: AppImage + `.deb`.
- Driverele Vector nu sunt incluse; aplicația le detectează și dă un mesaj clar dacă lipsesc.
- DLL-urile Seed&Key și certificatele OEM nu sunt niciodată incluse.
- SBOM (CycloneDX) generat la fiecare build.

---

## 13. Conformitate automotive — nou

Un tool nu se „certifică” singur: conformitatea vine din procesul de dezvoltare și din dovezile produse.
Arhitectura e gândită ca aceste dovezi să rezulte natural.

| Standard / regulament | Relevanță pentru tool | Ce face arhitectura |
|---|---|---|
| **ISO 26262-8 §11** (calificarea tool-urilor) | Un flash greșit poate introduce o eroare în ECU (TI2) | Țintim **TD1** (detecție ridicată) → TCL1: check memory în ECU (CRC/semnătură), citire înapoi opțională (0x23/0x35), comparare de versiuni după flash, hash-uri ale fișierelor de intrare. Livrabile: Tool Safety Manual, use cases, known issues, suită de validare |
| **ISO/SAE 21434** (cybersecurity) | Tool-ul manipulează certificate și chei și scrie software în vehicul | TARA a tool-ului, chei private niciodată expuse (§4.7.2), proiecte semnate, audit log, SBOM, gestionarea vulnerabilităților SOUP, build reproductibil și semnat |
| **UNECE R155 / R156** (CSMS / SUMS) | OEM-ul trebuie să poată dovedi ce software s-a scris, unde și de către cine | Raport + audit log: VIN/serial, versiuni SW înainte/după (DID F189/F195 etc.), hash-uri, certificate și operator |
| **Automotive SPICE** (SWE.1–6, SUP.8/9/10) | Procesul de dezvoltare | SRS cu ID-uri REQ, ADR-uri pentru decizii, teste trasabile la cerințe (markeri pytest), review-uri, configuration management (git + tag-uri), change requests |
| ISO 14229 / 15765 / 13400 / 17987, ASAM MCD-2D (ODX) | Conformitate de protocol | Teste de conformitate pe simulator; abaterile (ex. `stmin_tx_override`) sunt opt-in și apar în raport |

Practici de dezvoltare: `mypy --strict`, `ruff`, `bandit`, acoperire de teste raportată, dependențe fixate cu
hash, evaluarea fiecărei componente SOUP (inclusiv interpretorul Python).

Ținta exactă (TCL, nivel ASPICE, utilizare în producție sau doar în dezvoltare) → [Q-13].

---

## 14. Riscuri

| ID | Risc | Mitigare |
|---|---|---|
| R-01 | Jitter de timing în Python la STmin mic / sub 1 ms, mai ales cu multe job-uri paralele | Proces per job (D-05), `NotifierBasedCanStack`, măsurători din F1; transport înlocuibil cu o implementare nativă (C/Rust) fără schimbări în straturile de sus |
| R-02 | Vector VN pe Linux: `python-can` suportă Vector doar pe Windows | Clarificare cu Vector, per model, a driverelor Linux (XL library sau SocketCAN); DoIP merge pe orice NIC |
| R-03 | DLL Seed&Key 32/64-bit; DLL-uri Windows inutilizabile pe Linux | Bridge 32-bit; `.so` și plugin Python pe Linux |
| R-04 | Format CDD nepublic | Parser best-effort + fișiere reale; ODX-D ca alternativă |
| R-05 | Variații ODX-F între OEM-uri | Parser tolerant + `flashtool inspect` |
| R-06 | LIN neverificabil fără hardware | Simulator LIN-TP + HIL |
| **R-07** | **Job-urile `.jar` cer un subset al API-ului ASAM MCD-3D** — efort mare, standard cu licență, risc de incompatibilitate | Spike de fezabilitate înainte de angajament (§4.6); pasul `java_job` e izolat |
| **R-08** | **API-ul ZenZefi** nu e public; acces condiționat de OEM | Documentație și acces de test de la OEM ([Q-10]); `CertificateProvider` abstract, deci se poate testa cu PKI proprie pe simulator |
| R-09 | Cerințele de reglementare cresc costul (documentație, dovezi) | Dovezile se generează automat (trasabilitate, rapoarte); ținta trebuie fixată devreme ([Q-13]) |

---

## 15. Roadmap

| Fază | Conținut | Criteriu de ieșire |
|---|---|---|
| **F0** | Arhitectură + SRS (cerințe cu ID-uri) + plan de calificare | Aprobate |
| **F1** | HAL Vector + enumerare, CAN + CAN FD (ISO-TP cu STmin/BS configurabile), UDS client, firmware bin/hex/s19 cu DFI/memorySize configurabile, engine + `uds_standard`, Seed&Key (DLL 64, bridge 32, Python), CLI, simulator, proces per job | Flash pe simulator în CI (Win + Linux) + pe ECU real cu VN16xx |
| **F2** | DoIP (+TLS), PDX/ODX-F, 0x29 cu `FileProvider`/PKCS#11 + simulator PKI | Flash DoIP pe simulator și pe HIL |
| **F3** | GUI complet + flashing paralel (BusGroup, JobManager) | 4 ECU-uri în paralel din GUI, cu rapoarte |
| **F4** | ZenZefi provider, catalog CDD/ODX-D, moștenire template-uri, editor de secvențe | Flash cu autentificare ZenZefi pe ECU real |
| **F4b** | Spike `.jar` → decizie → runtime Java (dacă e fezabil) | Un job OEM real rulează end-to-end |
| **F5** | LIN (Vector XL LIN, LIN-TP), Linux + VN | Flash LIN pe HIL |
| **F6** | Packaging semnat, SBOM, Tool Safety Manual, raport de validare | Release candidate |

### Stare la 2026-11-01

| Fază | Stare |
|---|---|
| F1 | Gata (testat pe ECU simulat) |
| F2 | Gata: DoIP, PDX/ODX-F (odxtools), 0x29 APCE cu provider `file`/`python` și broker de credențiale. PKCS#11: neînceput |
| F3 | Gata: GUI, procese per bus, grupuri de bus cu pași `group: once` |
| F4 | Parțial: catalog ODX-D + YAML, editor de template-uri în GUI. ZenZefi blocat de Q-10, `.cdd` de Q-08 |
| F4b | Blocat: job-uri `.jar` (Q-11) |
| F5 | Gata în simulare: LIN-TP + backend Vector XL LIN (netestat pe hardware). Linux + VN: R-02 deschis |
| F6 | Gata: build stand-alone, SBOM, SHA256SUMS, template installer Windows, trasabilitate, draft Tool Safety Manual |
| Extensii | Gata în simulare: block sequences per tip de bloc (BOOT/APP/CAL/DATA), erase după logical block index |

Toate fazele „gata” sunt validate doar pe simulator. Validarea HIL cu VN și ECU-uri reale (REQ-05) rămâne de făcut.

---

## 16. Decizii și întrebări

### Răspunsuri primite (runda 1)

| ID | Întrebare | Răspuns | Impact |
|---|---|---|---|
| Q-01 | ECU/OEM de validare | Generic, toate | Nucleul fără logică OEM; ECU-uri de referință pentru HIL (§11) |
| Q-02 | Modele VN | Toate | Detectarea capabilităților, fără cod per model (§10) |
| Q-03 | Seed&Key 32/64 | Ambele | Bridge 32-bit + încărcare directă 64-bit (§4.7.1) |
| Q-04 | Python + PySide6 | Da | D-01 confirmat |
| Q-05 | CAN FD în F1 | Da, toate bus-urile | CAN FD în F1; DoIP F2; LIN F5 (ordinea poate fi schimbată) |
| Q-06 | Flashing paralel | Da, pe mai multe interfețe | D-05, §4.8 |
| Q-07 | Compresie/criptare | Fișierele vin deja procesate; DFI configurabil în 0x34 | §4.3 |
| Q-09 | Template-uri OEM | Unii OEM livrează flash job-uri `.jar` | §4.6, R-07 |
| — | Cerințe noi | ZenZefi + certificate, GUI stand-alone, reglementare automotive, STmin configurabil | §4.7.2, §12, §13, §4.1 |

### Întrebări deschise

| ID | Întrebare |
|---|---|
| Q-08 | Poți furniza un `.cdd` și un `.pdx` de exemplu (sau anonimizate)? |
| Q-10 | **ZenZefi**: ai acces la documentația API-ului? Ce variante de 0x29 folosesc ECU-urile țintă (APCE unidirecțional / bidirecțional, ACR)? Certificatele sunt pe rol (ex. „supplier”, „development”, „production”)? |
| Q-11 | **`.jar`**: sunt job-uri Java ODX (`SINGLE-ECU-JOB` din PDX, scrise pentru un D-Server MCD-3D) sau aplicații Java independente? Poți furniza un exemplu? Aveți acces la definițiile API-ului ASAM MCD-3D? |
| Q-12 | La fișierele comprimate: `memorySize` din 0x34 trebuie să fie dimensiunea **necomprimată** (cum cere ISO 14229) sau cea transferată? De unde vine dimensiunea necomprimată (configurare manuală, header în fișier, ODX)? |
| Q-13 | **Ținta de reglementare**: tool-ul se folosește în producție (EOL), în service sau doar în dezvoltare? Ce TCL ISO 26262 și ce nivel ASPICE sunt cerute? E nevoie de evaluare externă? |
| Q-14 | CAN XL este necesar? |
| Q-15 | Câte ECU-uri în paralel (ordin de mărime)? Sunt pe bus-uri separate sau și mai multe pe același bus? |
| Q-16 | Sunt necesare utilizatori și roluri (login, operator vs inginer) și politici de tipul „doar proiecte semnate în producție”? |

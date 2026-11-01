# FlashTool — Tool Safety Manual (DRAFT)

> Status: **draft de lucru**, nu document eliberat. Clasificarea finală (TCL) o stabilește proiectul care
> folosește tool-ul, pe baza use case-urilor sale (ISO 26262-8 §11). Documentul descrie ce oferă tool-ul ca
> argument și ce măsuri trebuie să ia utilizatorul.

## 1. Identificare

| Câmp | Valoare |
|---|---|
| Tool | FlashTool |
| Versiune | `flashtool --version` (versiunea din pachet) + tag-ul git al build-ului |
| Configurație | `dist/SHA256SUMS` (hash-urile tuturor fișierelor livrate) și `dist/sbom.cdx.json` (componentele SOUP) |
| Cerințe | [requirements.yaml](requirements.yaml) |
| Arhitectură | [ARCHITECTURE.md](ARCHITECTURE.md) |

## 2. Use case-uri și clasificare (propunere)

| UC | Descriere | TI | Măsuri de detecție | TD propus | TCL propus |
|---|---|---|---|---|---|
| UC-01 | Programare ECU în dezvoltare (laborator, HIL) | TI2 | §3 | TD1 | TCL1 |
| UC-02 | Programare la final de linie (EOL) | TI2 | §3 + verificare de versiuni după flash, imposibil de dezactivat prin politică | TD1 | TCL1 |
| UC-03 | Programare în service | TI2 | §3 | TD1 | TCL1 |

TI2: o eroare a tool-ului poate duce la un software greșit sau corupt în ECU.
TD1 se sprijină în primul rând pe verificările făcute **de ECU** (bootloader), independente de tool.

## 3. Defecte posibile ale tool-ului și cum sunt detectate

| ID | Defect | Detecție | Unde |
|---|---|---|---|
| MF-01 | Fișier greșit sau modificat | SHA-256 în raport și în audit log; comparare cu valoarea din release | raport, audit |
| MF-02 | Date corupte la transfer | CRC32 / semnătura verificate de ECU prin rutina *check memory* (`expect_data`) | ECU |
| MF-03 | Bloc parțial scris | 0x37 respins de ECU dacă lungimea nu corespunde; *check memory* | ECU |
| MF-04 | Adresă sau dimensiune greșită (0x34) | Validarea ALFID, a suprapunerilor și a hărții de memorie înainte de conectare; ECU respinge zonele în afara memoriei | tool, ECU |
| MF-05 | Bloc lipsă | *check programming dependencies* (0x31 FF01) | ECU |
| MF-06 | Pas omis din secvență | Secvența rezolvată apare integral în raport; template-urile de producție sunt revizuite | raport, proces |
| MF-07 | Încălcarea timpilor ISO-TP | Abaterile (`stmin_tx_override_ms`) sunt opt-in și apar ca avertisment în raport | raport |
| MF-08 | Raport sau log falsificat | Audit log cu lanț de hash-uri (`flashtool audit-verify`) | tool |
| MF-09 | Template care execută cod | Limbaj de expresii restricționat, fără `eval` | tool |

## 4. Măsuri obligatorii pentru utilizator

1. Nu eliminați pașii `check_memory` și `check_dependencies` și nici `expect_data` din template-urile folosite în producție.
2. Revizuiți secvența rezolvată (`flashtool sequence PROIECT`) la fiecare schimbare de template și păstrați-o împreună cu revizia.
3. Folosiți numai versiuni eliberate ale tool-ului; verificați `SHA256SUMS` după instalare.
4. Nu folosiți `stmin_tx_override_ms` și nici providerul de certificate `file` în producție.
5. Păstrați rapoartele, trace-urile și audit log-ul conform politicii de arhivare a proiectului; verificați periodic lanțul audit log-ului.
6. Seed&Key DLL-urile și certificatele se obțin numai din sursele OEM; nu se copiază în proiect.

## 5. Limitări cunoscute

- Calea Vector XL (VN16xx/VN56xx) și LIN pe hardware real nu sunt încă validate pe HIL (REQ-05).
- Pe Linux, suportul Vector depinde de driverele disponibile de la Vector (risc R-02).
- Timing-ul sub 1 ms pe Windows nu e garantat din Python (risc R-01); se măsoară în validarea HIL.
- SBOM-ul curent descrie mediul de build complet, inclusiv uneltele de dezvoltare. Pentru release, build-ul se face într-un mediu doar cu dependențele de runtime.
- ZenZefi și job-urile OEM `.jar` nu sunt implementate (Q-10, Q-11).

## 6. Dovezi de validare

| Dovadă | Cum se obține |
|---|---|
| Teste automate (unit + integrare cu ECU-ul simulat) | `python -m pytest --trace-json=out/trace.json` |
| Matrice cerințe → teste | `python tools/traceability.py out/trace.json -o out/traceability.md` |
| Regulile de arhitectură | `lint-imports` |
| Teste HIL | de definit pe ECU-urile de referință (un ECU per tip de bus × o categorie VN) |
| Componente SOUP | `dist/sbom.cdx.json` |

## 7. Stare

Tool-ul **nu este încă calificat**. Lucruri deschise: validarea HIL, ținta de reglementare (Q-13), revizia
acestui document de către responsabilul de safety al proiectului.

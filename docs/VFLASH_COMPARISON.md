# FlashTool vs. Vector vFlash — comparație de funcționalități

Surse pentru vFlash, verificate pe 2026-11-01:
- [pagina de produs vFlash](https://www.vector.com/en/product/vflash/), versiunea 12;
- [matricea de funcții pe ediții](https://www.vector.com/dam/jcr:07a7a39c-78fd-45d0-aacb-28d589aa81ea/vFlash_Feature_Matrix.2026-06-30-11-29-10.pdf).

Manualul de utilizare vFlash nu a fost accesibil, așa că detaliile de nivel „manual” (de exemplu câmpurile exacte din proiect) nu sunt comparate.

Legendă: ✅ există · 🟡 parțial · ❌ lipsește · — nu se aplică

| Domeniu | vFlash (sursa: Vector) | FlashTool | Note |
|---|---|---|---|
| **Bus-uri** | CAN, CAN FD, LIN, FlexRay, Ethernet (DoIP, SoAd) | ✅ CAN, CAN FD, LIN, DoIP · ❌ FlexRay, SoAd | FlexRay cere hardware VN76xx/VN89xx și FR-TP (ISO 10681-2) |
| Ethernet | IPv4/IPv6, VLAN, DHCP, Activation Line, TC10 wake-up, TLS | 🟡 IPv4/IPv6 și TLS prin socket-ul sistemului | Activation Line și TC10 cer hardware Vector |
| Hardware | VN15xx/16xx/56xx/4610/5430/7572/76xx; PassThru și D-PDU API la cerere | 🟡 Vector XL (Windows), SocketCAN (Linux), simulator | J2534 și D-PDU API: nu (încă) |
| **Formate** | Intel HEX, Motorola S, binar, ODX-F | ✅ toate patru, plus PDX | |
| Formate OEM | „manufacturer-specific”, de exemplu JLR ODX-F→VBF și Bosch multi-datablock HEX | ✅ VBF (Volvo/Geely/JLR) · ❌ Bosch HEX + Memory Description File | |
| Mai multe blocuri per ECU | flashware cu mai multe logical blocks, secvențe din template-ul OEM | ✅ un fișier per bloc sau mai multe blocuri dintr-un ODX-F/VBF; tipul (BOOT/APP/CAL/DATA) alege secvența blocului | Secvențele per tip le definești în template (`block_sequences`) |
| Date comprimate/criptate | da | ✅ trimise ca atare, cu DFI/memorySize configurabile | La fel ca vFlash, tool-ul nu decomprimă |
| Fișiere de lucru în dezvoltare | „referințe flexibile la flashware” | ✅ release nou prin drag & drop / Load release, cu opțiunile păstrate | |
| **Template-uri OEM** | peste 250 de bootloadere OEM și Tier-1 | 🟡 template standard ISO 14229 + template-uri proprii (extends/patch) | Template-urile Vector se bazează pe specificații OEM confidențiale; le adaugi pe ale tale |
| Extensii (Custom Actions) | scripturi VDS înainte/după flash: coding, backup/restore | ✅ pași „Custom action” în Python, rulați oriunde în secvență | Rulează doar dacă ECU-ul are `allow_custom_actions`; SHA-256-ul scriptului ajunge în raport |
| **Securitate** | „Security Manager” add-on-uri de la OEM; TLS pentru DoIP | ✅ Seed&Key (DLL 32/64, .so, Python), 0x29 cu certificate, TLS | Interfața „Security Manager” Vector: nu |
| **Flash paralel** | Station: până la 10 ECU-uri pe canale separate | ✅ un proces per bus, fără limită fixă | |
| System Update | mai multe ECU-uri dependente pe un singur canal, sincronizate | ✅ grupuri de bus cu pași funcționali o singură dată și barrier | |
| Mesaje de rețea | trimitere ciclică / la eveniment a mesajelor CAN statice | 🟡 ciclic (în flash și manual, din ECU tools) și wake-up: da · la eveniment: nu | |
| Trezire ECU | Activation Line / TC10 pentru Ethernet | 🟡 frame-uri de wake-up pe CAN, wake-up signal pe LIN · ❌ Activation Line, TC10 | Pentru Ethernet e nevoie de hardware |
| Identificare ECU | (necunoscut, manualul n-a fost accesibil) | ✅ ECU tools → Read identification (DID-urile ISO 14229) | |
| **Automatizare** | API C, C#, Python, SOVD; integrare ca bibliotecă | 🟡 API Python (`flashtool.api`), CLI cu evenimente JSON · ❌ C/C#, SOVD | Din C/C# se poate apela CLI-ul cu `--json` |
| Integrare | CANoe, CANoe.DiVa, SIL Kit, CI/CT cu HIL | 🟡 CI/CD prin CLI/API și simulatorul inclus · ❌ CANoe, SIL Kit | |
| **Pachete** | `.vflashpack`, `.vflashsealed` (protejat la scriere) | ✅ `.ftpack` cu manifest SHA-256, opțional sigilat | |
| **Interfață** | ECU-uri afișate ierarhic, progres în „system view”, analiza procesului de flash | ✅ carduri per ECU cu progres/viteză/ETA, pagină Reports cu analiza duratelor, temă light/dark | |
| Rapoarte | „short report” integrat, rapoarte prin Custom Actions, imprimante de etichete | ✅ HTML/JSON, trace UDS, BLF, audit log semnat cu lanț de hash-uri · ❌ etichete | |
| Trace | CAN(FD) și Ethernet | 🟡 CAN (BLF/ASC) și trace UDS · ❌ pcap pentru Ethernet | |
| **Platformă** | Windows 10/11 x64 | ✅ Windows, Linux, macOS (macOS doar cu simulatorul) | |
| Licențiere | Device / Named User / Test Bench, reactivare anuală | — | |

## Ce rămâne de făcut pentru paritate

| Funcție | Ce trebuie |
|---|---|
| FlexRay | Hardware VN cu FlexRay și ISO 10681-2 |
| J2534 / D-PDU API | Backend nou în HAL; util pentru hardware non-Vector |
| Bosch HEX cu Memory Description File | Exemple de fișiere |
| Template-uri OEM | Specificațiile bootloader-ului de la fiecare OEM (confidențiale) |
| API C/C# și SOVD | De decis dacă e nevoie de ele |
| Mesaje CAN la eveniment, pcap pentru Ethernet, imprimante de etichete | Implementare directă, dacă sunt utile |

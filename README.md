# PID transfer monitor — přestup 336 → 384 na Zličíně

Sleduje každý všední den ráno, jestli by se stihl přestup z linky **336**
(příjezd na Zličín) na linku **384** (odjezd ze Zličína), a ukládá si k tomu
skutečná zpoždění z realtime dat PID. Cílem je průběžná statistika „na kolik
procent se na ten přestup můžu spolehnout".

Sledované spoje (`connections.json`):

| id | feeder | příjezd (JŘ) | connector | odjezd (JŘ) | rezerva JŘ | 384 do Berouna,sídliště |
|------|--------|--------------|-----------|-------------|------------|------------|
| 0601 | 336    | 6:10         | 384       | 6:20        | 10 min | 6:50 |
| 0701 | 336    | 7:11         | 384       | 7:20        | 9 min | 7:50 |

Přestup se počítá jako **úspěšný**, když skutečný odjezd 384 je aspoň
`transfer_buffer_min` (teď **1 min**) po skutečném příjezdu 336.

Navíc se (jen když prestup vyjde) sleduje i **poslední úsek** — skutečný
příjezd 384 do Beroun,sídliště — a spočítá se, jestli to i s chůzí stihneš
do školy do 8:00 (viz [Škola do 8:00](#škola-do-800) níže).

---

## 1. Golemio API klíč (nutné, jednorázově)

Realtime data i odjezdové tabule vyžadují přístupový token.

1. Otevři <https://api.golemio.cz/api-keys/auth/sign-in> a **zaregistruj se**
   (e-mail + heslo, zdarma, osobní účet — musíš udělat sám, účet za tebe
   nezaložím).
2. Po přihlášení v sekci **API keys** klikni na **Create new token**.
3. Token zkopíruj.
4. V téhle složce zkopíruj `config.example.json` → `config.json` a vlož token
   do pole `api_key`.

```json
{
  "api_key": "eyJhbGciOi...tvůj token...",
  "transfer_buffer_min": 1,
  "poll_interval_s": 60
}
```

Ověř, že klíč funguje a spoje se správně páruje:

```bash
python monitor.py selftest
```

Mělo by to vypsat aktuální zpoždění linek 336 a 384 na Zličíně.

---

## 2. Sběr dat

```bash
python monitor.py collect            # běží celé ráno, na konci každého okna zapíše řádek
python monitor.py collect --once     # jen jeden dotaz teď, pro ladění
python monitor.py snapshot           # krátké ~6min polování kolem času přestupu (cloud/cron)
```

`collect` počká na začátek prvního okna (5:45), pak se každou minutu ptá na
odjezdovou tabuli Zličína, drží si poslední známé zpoždění 336 a 384 a v
6:35 / 7:40 zapíše výsledek dne do `data/results.csv`. Syrové vzorky jdou do
`data/samples.jsonl`, log do `logs/`.

- Víkendy se přeskakují automaticky.
- Svátek / výluka (linky se v tabuli neobjeví) → řádek `NO_SERVICE`, do
  statistiky úspěšnosti se nepočítá.
- Když už výsledek pro dnešek existuje, spoj se přeskočí (bezpečné pouštět víckrát).

## 3. Automatické spouštění

### 3a. GitHub Actions (doporučeno) — `.github/workflows/snapshot.yml`

Sběr běží jako **GitHub Actions workflow** v tomhle repu (nezávislé na tvém PC).
GitHub-hosted runner má volný ven na internet — Claude cloud prostředí naopak
`api.golemio.cz` blokuje egress politikou, takže tam to nejde.

- Cron: `*/15 3-7 * * 1-5` (UTC) — běží každých 15 minut v širokém okně
  3:00–7:59 UTC (5:00–9:59 léto / 4:00–8:59 zima). **GitHub scheduled workflow
  neni spolehlivy na minutu** — v provozu (9.–11. 9. 2026) jsme viděli záběr
  zpožděný o 2,5 h i den, kdy do 07:04 UTC nenaběhl vůbec. Proto místo 3 přesných
  časů běží tahle vysoká frekvence: `snapshot --wait` sám pozná, jestli je nějaký
  přestup „na řadě" (~40 min před ním až do jeho konce) — pokud ne, doběhne za
  pár vteřin, takže to nic nestojí, ale dává to hodně šancí trefit se do okna
  i když se GitHub zpozdí.
- Skript počká na čas přestupu, ~16 min polluje odjezdovou tabuli Zličína
  (u 336 drží *nejhorší* zpoždění, u 384 *poslední*) a pak — pokud se přestup
  povedl — pokračuje sledováním příjezdu do Beroun,sídliště až do jeho
  plánovaného času + rezerva. Zapíše řádek do `data/results.csv` a
  `monitor.py` sám **commitne + pushne** (funkce `git_sync`); workflow má navíc
  ještě svůj vlastní commit krok jako pojistku.
- **Letní/zimní čas řeší kód** — nic se nikdy ručně nepřenastavuje.
- API klíč: GitHub **secret** `GOLEMIO_API_KEY` (Settings → Secrets and variables
  → Actions). `monitor.py` bere klíč z proměnné `GOLEMIO_API_KEY`, jinak z
  `config.json` (lokálně).
- Ruční spuštění / test: záložka **Actions → PID snapshot → Run workflow**.

Nastavení (jednorázově):
```bash
gh secret set GOLEMIO_API_KEY --repo JindraKK/pid-transfer-monitor   # vloží klíč
gh auth refresh -s workflow                                          # aby šlo pushnout .github/workflows/
```

### 3b. Lokálně na Windows (záloha) — `register-task.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\register-task.ps1
```

Úloha Po–Pá 5:43 s *Probudit počítač*. Na tomhle notebooku (jen Modern
Standby S0, „povolit časovače pro probuzení" vypnuté na baterii, noční
restarty kvůli Windows Update) se nespouští spolehlivě na minutu — ale
**když se rozjede, i se zpožděním, teď už výsledek sám commitne a pushne**
(dřív zůstával jen lokálně v `data/`, nikdy se nedostal do repa). Funguje tak
jako skutečná záloha ke GitHub Actions, ne jen jako mrtvý kód — 10. 9. 2026
to fakticky zachránilo den dat, který cloud tou dobou nesebral.

Ruční test: `Start-ScheduledTask -TaskName "PID transfer monitor"`
Odebrání: `Unregister-ScheduledTask -TaskName "PID transfer monitor" -Confirm:$false`

## Škola do 8:00

Kromě přestupu na Zličíně se (jen v dnech, kdy přestup vyšel) sleduje i
**skutečný příjezd 384 do Beroun,sídliště** a spočítá se odhad příchodu do
školy: `final_eff_arr + walk_min_to_school`. Když je pod `school_deadline`
(8:00), je `school_ok = 1`.

Nastavení je v `connections.json` → `final_leg`:
```json
"final_leg": {
  "route": "384",
  "sched_arrival": "06:50",
  "node_asw_id": 1747,
  "node_name": "Beroun,sidliste",
  "walk_min_to_school": 5,
  "school_name": "SZS Beroun (Mladeze 1102/8)",
  "school_deadline": "08:00"
}
```

**Nejisté:** stop `"SZS Beroun"`, který jsi zmínil (1 min chůze od školy), se
v aktuálním PID GTFS ani přes živé Golemio API nenašel pod žádným názvem
(`SZS`, `SZS Beroun`, `Zdravotnická škola`, ...). Adresa školy (Mládeže 1102/8)
ale přesně odpovídá cíli chůze, který PID plánovač počítal z **Beroun,sídliště**
(viz původní screenshoty — „Přesun asi 5 min na Beroun, Mládeže 1102/8"), takže
jsem použil tenhle stop a **5 min chůze** (ne 1 min). Pokud znáš přesnější
zastávku nebo kratší čas chůze, uprav `node_asw_id`/`walk_min_to_school` v
`connections.json` — `walk_min_to_school: 1` by dělalo rozdíl hlavně u spoje
0701 (příjezd 7:50, jinak jen 10 min rezervy do 8:00).

Poznámka: `school_ok` se počítá jen když **přestup vyšel** (`verdict == OK`) —
pokud se prestup nepovede, řídil by ses jinym (pozdejsim) spojem a odhad by
neodpovídal realite; v takovem dni je sloupec prazdny s poznamkou.

## 4. Statistika

```bash
python monitor.py report
python monitor.py report --html data/report.html
```

Vypíše celkovou úspěšnost, rozpad po spojích, statistiku zpoždění 336
(průměr / medián / p90 / max) a posledních 14 dní. `--html` uloží přehlednou
stránku.

## 5. Týdenní aktualizace jízdních řádů

GTFS z pid.cz se mění každý týden a **trip_id se mění taky** — proto skript
spoje páruje podle čísla linky a času z tabule, ne podle trip_id, takže běžnou
výměnu JŘ přežije bez zásahu.

Když se ale **změní časy** spojů (jiný jízdní řád), uprav `sched_arrival` /
`sched_departure` v `connections.json`. Kontrola proti staženému GTFS:

```bash
python monitor.py verify-timetable "C:\Users\Jindra\Documents\Claude\Projects\PID\Jizdní řády"
```

## Soubory

```
monitor.py                     hlavní skript (selftest / collect / snapshot / report / verify-timetable)
.github/workflows/snapshot.yml  GitHub Actions – automatický ranní sběr
config.json                    lokální API klíč (NENÍ v repu; v Actions se bere secret GOLEMIO_API_KEY)
config.example.json            šablona pro config.json
connections.json               definice sledovaných spojů
register-task.ps1              lokální záloha: úloha do Plánovače úloh Windows
data/results.csv               jeden řádek na spoj a den  ← z tohohle se dělá statistika
data/samples.jsonl             syrové vzorky (pro pozdější rozbor)
logs/                          logy běhu (necommitují se)
```

## Zdroje dat

- Realtime: Golemio API `GET /v2/pid/departureboards` (`X-Access-Token`).
  Dokumentace: <https://api.golemio.cz/pid/docs/openapi/>
- Jízdní řády (GTFS): <https://pid.cz/o-systemu/opendata/>

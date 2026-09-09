# PID transfer monitor — přestup 336 → 384 na Zličíně

Sleduje každý všední den ráno, jestli by se stihl přestup z linky **336**
(příjezd na Zličín) na linku **384** (odjezd ze Zličína), a ukládá si k tomu
skutečná zpoždění z realtime dat PID. Cílem je průběžná statistika „na kolik
procent se na ten přestup můžu spolehnout".

Sledované spoje (`connections.json`):

| id | feeder | příjezd (JŘ) | connector | odjezd (JŘ) | rezerva JŘ |
|------|--------|--------------|-----------|-------------|------------|
| 0601 | 336    | 6:10         | 384       | 6:20        | 10 min |
| 0701 | 336    | 7:11         | 384       | 7:20        | 9 min |

Přestup se počítá jako **úspěšný**, když skutečný odjezd 384 je aspoň
`transfer_buffer_min` (teď **1 min**) po skutečném příjezdu 336.

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

### 3a. Cloud (doporučeno) — `monitor.py snapshot`

Sběr běží jako dvě **cloudové rutiny Claude Code** (nezávislé na tvém PC):

| rutina | cron (UTC) | čas Praha (letní / zimní) | spoj |
|--------|-----------|---------------------------|------|
| PID přestup 6:01 | `15 4 * * 1-5` | 6:15 / *5:15 → přehodit na `15 5`* | 0601 |
| PID přestup 7:01 | `16 5 * * 1-5` | 7:16 / *6:16 → přehodit na `16 6`* | 0701 |

Každá rutina naklonuje tenhle repo, spustí `python monitor.py snapshot --minutes 6`
(krátké polování kolem času přestupu — u 336 si drží *nejhorší* zpoždění, u 384
*poslední*), zapíše jeden řádek do `data/results.csv` a **commitne + pushne** zpět
do repa. Statistika se pak dělá z `data/results.csv` kdekoli (`report`).

Předpoklady:
- GitHub účet propojený s Claude Code na claude.ai (Settings → Connectors / GitHub).
- `config.json` s API klíčem je v repu (repo je **private**). Klíč jde kdykoli
  otočit na api.golemio.cz a nahradit v `config.json`. Alternativně nastav
  `GOLEMIO_API_KEY` jako proměnnou prostředí (má přednost před `config.json`).

**Letní/zimní čas:** cron je vždy v UTC. Praha přechází na zimní čas 26. 10. 2026 —
pak je potřeba obě rutiny posunout o hodinu (viz tabulka).

### 3b. Lokálně na Windows (záloha) — `register-task.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\register-task.ps1
```

Úloha Po–Pá 5:43 s *Probudit počítač*. **Pozor:** na tomhle notebooku (jen
Modern Standby S0, „povolit časovače pro probuzení" vypnuté na baterii, noční
restarty kvůli Windows Update) se v praxi nespouští spolehlivě — proto cloud.
Ruční test: `Start-ScheduledTask -TaskName "PID transfer monitor"`
Odebrání: `Unregister-ScheduledTask -TaskName "PID transfer monitor" -Confirm:$false`

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
monitor.py            hlavní skript (selftest / collect / snapshot / report / verify-timetable)
config.json           API klíč a nastavení (v private repu; klíč jde otočit / dát do env GOLEMIO_API_KEY)
config.example.json   šablona
connections.json      definice sledovaných spojů
register-task.ps1     lokální záloha: registrace úlohy do Plánovače úloh Windows
data/results.csv      jeden řádek na spoj a den  ← z tohohle se dělá statistika
data/samples.jsonl    syrové vzorky (pro pozdější rozbor)
logs/                 logy běhu (necommitují se)
```

## Zdroje dat

- Realtime: Golemio API `GET /v2/pid/departureboards` (`X-Access-Token`).
  Dokumentace: <https://api.golemio.cz/pid/docs/openapi/>
- Jízdní řády (GTFS): <https://pid.cz/o-systemu/opendata/>

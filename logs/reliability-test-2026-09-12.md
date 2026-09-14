# GitHub Actions scheduled-trigger reliability — findings (9.–12. 9. 2026)

Plánovaný test (jednorázový cron `*/15 3-7 12 9 *`, 20 očekávaných spuštění
v sobotu 12. 9. mezi 3:00–7:45 UTC) doběhla naplánovaná Claude Code úloha jen
částečně — zůstala viset na potvrzení nástroje (nikdo nebyl u počítače) a
výsledek nikdy nedopsala ani nepushla. Číslo níže je dopočítané ručně přímo
z GitHub API (`gh api repos/JindraKK/pid-transfer-monitor/actions/runs`).

## Co se doopravdy stalo (všechny `schedule`-triggered běhy, 9.–14. 9. 2026)

| datum spuštění (UTC) | cron aktivní ten den | očekávaných záběrů | skutečných běhů | odchylka od nejbližšího záběru |
|---|---|---|---|---|
| 2026-09-10 08:25:44 | `55 3,4,5 * * 1-5` (3/den) | 3 | **1** | +2 h 31 min (od 05:55) |
| 2026-09-11 12:18:32 | `*/15 3-7 * * 1-5` (20/den) | 20 | **1** | +4 h 34 min (od posledního 07:45) |
| 2026-09-12 07:46:58 | `*/15 3-7 12 9 *` (20/den, testovací) | 20 | **1** | +2 min (od posledního 07:45) |

**Zjištění: GitHub scheduled workflow ve všech 3 dnech spustil prakticky
jen JEDNOU denně, bez ohledu na to, jestli byl v cronu 1 záběr nebo 20.**
Frekvence cronu tedy sama o sobě nic neřeší — GitHub evidentně škrtí/slučuje
scheduled běhy na zhruba 1×/den na tomhle repu, a čas toho jednoho běhu je
nepředvídatelný (od pár minut do 4,5 h po očekávaném okně). Za 3 dny testu
nezachytil ranní přestup v okně 3:00–7:45 UTC ani jednou přesně.

## Závěr

`schedule:` trigger GitHub Actions není použitelný jako spolehlivý zdroj dat
o ranním přestupu, ani při vysoké frekvenci cronu. Skutečná data teď chodí
hlavně z **lokálního Plánovače úloh Windows** (`register-task.ps1` + `git_sync`
v `monitor.py`) — ten dodal platná data 10.9. a 14.9., i když taky s určitým
zpožděním. GitHub Actions zůstává zapnutý jako bezplatný bonus (`workflow_dispatch`
pro ruční/testovací běhy funguje spolehlivě), ale nepočítá se s ním jako s
primárním zdrojem.

Zvážit do budoucna: externí cron služba (např. cron-job.org), která by
`workflow_dispatch` spouštěla přes GitHub REST API v přesný čas — na rozdíl od
GitHub vlastního `schedule:` triggeru takové služby čas dodržují přesně,
protože nejde o GitHubem interně škrcenou frontu.

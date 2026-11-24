# Morning Numbers — a numbers-only market brief that lives on a Sprite

Built for Fly.io Hacksgiving (Nov 23–29, 2026).

An agent that lives on a [Fly.io Sprite](https://fly.io/sprites). Each morning it
fetches four public market numbers, writes a short English brief with
day-over-day changes, sends it as a Pushover notification, and archives it to
GitHub. Provider API tokens never touch the Sprite: both integrations go through
**Sprites Connectors** (Pushover + GitHub). No personal or portfolio data is used.

```
Numbers-only market brief | 2026-11-24 (JST)
US session: 11/23
VIX           26.62  -25.83%  [11/23 vs 11/20]  Cboe-verified 11/20
BTC-USD   61,189.97  +0.04%  [11/23 vs 11/22 UTC close]
S&P 500    7,237.17  +4.73%  [11/23 vs 11/20]
US 10Y        4.92%  +0 bp  [11/23 vs 11/20]
```

### It refuses to make numbers up — and shows it

- **One base date for the US rows.** VIX, S&P 500 and the 10Y are aligned on the
  latest US session any of them has. A row still on an older session prints
  `not updated: last value 11/20 (US latest 11/23)` with no change, so a stale
  move is never read next to a fresh one.
- **VIX is cross-checked.** The latest closes come from Yahoo `^VIX`; the official
  Cboe CSV (about one session behind at 07:00 JST) confirms every date it already
  has (`Cboe-verified 11/20`). A disagreement prints `UNVERIFIED: ...` instead of
  being hidden. If Yahoo is down, the Cboe value is used alone.
- **It knows when markets were shut.** If the US session is the same as in the
  previous brief (Thanksgiving, weekends), the brief says
  `No new US session since last brief.`

## How it works

| Step | Module | Notes |
| --- | --- | --- |
| Fetch | `agent/sources.py` | Latest *completed* daily value + the one before. A failed source shows `n/a`; nothing is estimated. |
| Brief | `agent/briefing.py` | Values as fetched; the only derived numbers are % change (bp for the 10Y). US rows aligned on one session date. |
| Notify | `agent/notify.py` | `POST {PUSHOVER_GATEWAY_URL}/1/messages.json`. Dry-run (masked payload) when unconfigured. |
| Archive | `agent/archive.py` | `archive/YYYY-MM-DD.md` (appended per run) + `.json`; GitHub Contents API `PUT /repos/{repo}/contents/archive/<date>.md` through the connector. |
| Entry | `server.py` | `/run`, `/ask?q=`, `/healthz` on port 8080 — the Sprite URL wakes it. `/run` sends at most once per JST day (`?force=1` to resend); `?dry=1` sends and archives nothing. Notify and archive fail independently and the reason is kept in the response and `archive/<date>.json`. |

`/ask` answers only from the archived numbers (e.g. `/ask?q=vix` →
"VIX was 26.62 on 2026-11-23, down from 35.89 on 2026-11-20 (-25.83%) [Cboe-verified 11/20]").

### Data sources (public, no API key)

| Indicator | Source |
| --- | --- |
| VIX | Yahoo Finance chart API `^VIX` (unofficial), cross-checked against Cboe `VIX_History.csv` (official) |
| BTC-USD | Coinbase Exchange public candles, UTC daily close (open day dropped) |
| S&P 500 | Yahoo Finance chart API `^GSPC` (unofficial; intraday bar dropped while the session is open) |
| US 10Y | U.S. Treasury daily par yield curve CSV (official) |

## Sprite + Connectors mapping

| Hacksgiving piece | Here |
| --- | --- |
| Sprite (persistent Linux VM, wakes on HTTP) | Runs `server.py` as a Sprite service on port 8080 |
| Pushover Connector | Holds the app token; the Sprite sends only `user` + message |
| GitHub Connector (OAuth) | Writes `archive/<date>.md` via the Contents API |

## Run locally

```sh
python3 run_once.py --dry-run          # one pass, nothing sent
PORT=8080 python3 server.py            # then: curl -X POST 'localhost:8080/run?dry=1'
python3 -m unittest discover -s tests -t .
```
Python 3.9+ standard library only.

## Deploy to a Sprite

```sh
sprite login                                   # human approves in the browser
sprite create --skip-console morning-numbers
sprite use morning-numbers
sprite exec -- git clone https://github.com/<you>/hacksgiving-agent /home/sprite/app
sprite exec -- sprite-env services create web --cmd python3 \
  --args /home/sprite/app/server.py --http-port 8080
sprite info                                    # prints https://<name>-<org>.sprites.app
curl -H "Authorization: Bearer $SPRITES_TOKEN" "https://<url>/run?dry=1"
```
Service env vars (`PUSHOVER_GATEWAY_URL`, `PUSHOVER_USER_KEY`, `GITHUB_GATEWAY_URL`,
`GITHUB_REPO`) come from the connector detail pages; see `.env.example`.
Keep URL auth on `sprite` (default) so only token holders can trigger `/run`.

## Safety

- Market data only; no personal amounts, no trading.
- API tokens stay in Connectors (only the Pushover user key, a recipient id, is in the Sprite env); logs record request paths only.
- Pushover: 1,024-char limit respected; rejected requests are not retried unchanged.
- One production run per JST day; the waker calls `/run` once and never retries.

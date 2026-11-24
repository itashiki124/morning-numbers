# Morning Numbers — No model. No guesses.

An agent with no model and no opinions, built for Fly.io Hacksgiving (Nov 23–29, 2026).

- **Demo (60 s, one take):** `<demo post link — coming 2026-11-29>`
- **What it has written so far:** [`archive/`](archive/) — one numbers-only brief per
  morning, plus [`archive/declined.md`](archive/declined.md), the questions it declined
  to answer.

It lives on a [Fly.io Sprite](https://fly.io/sprites). Once each morning an outside
request wakes it (a LaunchAgent on my Mac calls `/run` at 07:05 JST; unattended runs
start 2026-11-25). It fetches four public market numbers (VIX, BTC, S&P 500, US 10Y),
writes a short English brief with day-over-day changes, and commits it to this
repository through the **GitHub Connector**. No personal or portfolio data is used.

Three facts about how it is built:

1. **No model calls.** There is no LLM anywhere in the code. Every number in a brief
   is printed as fetched; the only arithmetic is % change (bp for the 10Y).
2. **This repository was made by the Sprite.** It was created from inside the Sprite
   through the GitHub Connector (`POST /user/repos`), and every file here, code
   included, was put there the same way (Contents API). Neither my Mac nor the
   Sprite holds a git credential.
3. **Narrow permission.** The connector holds a `public_repo` token only, and its access
   policy allows two paths: `/user/repos` and `/repos/itashiki124/morning-numbers/*`.
   Anything else (for example `GET /user/emails`) gets `403 endpoint is not in allowed list`.

Ask it why, and it declines:

```
GET /ask?q=why did VIX fall?
I don't do "why", "will" or "should". Here is what I have:
VIX was 26.62 on 2026-11-23, down from 35.89 on 2026-11-20 (-25.83%) [Cboe-verified 11/20]. Source: Yahoo ^VIX, checked against Cboe VIX_History.csv.
Declined so far: 1.
```

Each declined question becomes one line in `archive/declined.md` and one commit
named `declined: <question>`.

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
| Notify | `agent/notify.py` | Optional Pushover Connector, `POST {PUSHOVER_GATEWAY_URL}/1/messages.json`. **Not configured in this deployment**: every run records `notify: not configured`. |
| Archive | `agent/archive.py` | `archive/YYYY-MM-DD.md` (appended per run) + `.json`; GitHub Contents API `PUT /repos/{repo}/contents/archive/<date>.md` through the connector. |
| Entry | `server.py` | `/run`, `/ask?q=`, `/healthz` on port 8080 — the Sprite URL wakes it. `/run` sends at most once per JST day (`?force=1` to resend); `?dry=1` sends and archives nothing. Notify and archive fail independently and the reason is kept in the response and `archive/<date>.json`. |

`/ask` answers only from the archived numbers:

- `/ask?q=vix` → "VIX was 26.62 on 2026-11-23, down from 35.89 on 2026-11-20 (-25.83%) [Cboe-verified 11/20]".
- `/ask?q=vix this week` → the stored daily values of the last 7 days, read from every archived brief.
- Questions asking for a reason, a forecast or advice (why, will, should, buy, sell,
  tomorrow, …) get one fixed line, `I don't do "why", "will" or "should". Here is what I have:`,
  then the same numbers and `Declined so far: N.` The question (ASCII only, 80 chars
  max, 20 a day) is appended to `archive/declined.md` and committed. `?dry=1` declines
  without logging. `/ask` sits behind the Sprite URL auth, so only the owner can add lines.

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
| GitHub Connector (OAuth, `public_repo`) | Created this repo; writes `archive/<date>.md` and `archive/declined.md` via the Contents API |
| Pushover Connector (optional) | Supported in code, not configured here. If added, it holds the app token and the Sprite sends only `user` + message |

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
sprite exec -- git clone https://github.com/itashiki124/morning-numbers /home/sprite/app
sprite exec -- sprite-env services create web --cmd python3 \
  --args /home/sprite/app/server.py --http-port 8080
sprite info                                    # prints https://<name>-<org>.sprites.app
curl -H "Authorization: Bearer $SPRITES_TOKEN" "https://<url>/run?dry=1"
```
Service env vars come from the connector detail pages; see `.env.example`. This
deployment sets only `GITHUB_GATEWAY_URL` and `GITHUB_REPO` (plus `PORT`); the
Pushover pair (`PUSHOVER_GATEWAY_URL`, `PUSHOVER_USER_KEY`) is unset.
Keep URL auth on `sprite` (default) so only token holders can trigger `/run`.

## Safety

- Market data only; no personal amounts, no trading.
- No API token on the Sprite: the GitHub token stays in the Connector. Logs record request paths only.
- If Pushover is ever added: only the user key (a recipient id) goes in the Sprite env; 1,024-char limit respected; rejected requests are not retried unchanged.
- One production run per JST day; the waker calls `/run` once and never retries.

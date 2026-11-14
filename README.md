# JobPilot

[![ci](https://github.com/Nilish1994/job-match-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Nilish1994/job-match-agent/actions/workflows/ci.yml)

**An AI job-search copilot for Australian tech roles.** It does the searching, screening and writing, and leaves
every decision to you.

**Website:** https://jobpilot.imisstech.com.au (invite-only beta)

Each week JobPilot scores new Melbourne tech jobs against each user's profile with Claude and sends the best matches
to Telegram. Tap **Approve** and you get a one-tap application kit:
- a CV PDF tailored to the role
- a cover-letter PDF
- paste-ready answers to the usual screening questions

You apply on the employer's site and tap **Applied**. A week later JobPilot asks how it went.

```
melbourne-tech-jobs ──► /feed/jobs ──► shared jobs table
   (daily Adzuna collection)                │
                                            ▼  for each active user (weekly)
              profile (encrypted) ──► prefilter ──► Claude: score (batched, structured output)
                                                        │
                       Telegram ◄── that user's top matches
                          │  ✅ Approve    ──► Claude: letter + CV emphasis ──► CV & letter PDFs + answers
                          │  📨 Applied    ──► follow-up in 7 days ──► 🎉 / ❌ / 🔇 outcome
                          └  /cv /prefs /pause /deleteme
```

## Principles

- **It never applies for you.** Job boards prohibit bots, and a quick human check catches what a model misses. The
  agent does the expensive work (reading adverts, judging fit, drafting); the decision and the final click stay with
  you.
- **It never invents experience.** Profiles come from your CV. The tailored CV is rendered from that profile, and
  Claude only chooses emphasis: any skill or project name it returns that isn't in your profile is dropped. Anything
  it can't verify goes under "check before sending" instead of into the letter.
- **It respects work rights.** You say whether you're a citizen or PR, hold a visa with full work rights, or need
  sponsorship. Adverts requiring citizenship, a clearance or "no sponsorship" are skipped before Claude sees them, and
  the rest are scored accordingly.
- **Your data is yours.** See [PRIVACY.md](PRIVACY.md).
  - Profiles are encrypted at rest with Fernet.
  - CV files are read once and discarded.
  - `/deleteme` removes everything.

## How it works

| Part | Detail |
|---|---|
| **Onboarding** | A personal, single-use invite link from the owner (`/invite`) leads to a privacy notice and consent. The bot has no "request an invite" button: strangers are told it is invite-only and pointed to jobpilot.imisstech.com.au. The user uploads a CV PDF; text is extracted with pypdf and Claude turns it into a structured profile. A few taps set target roles, location, salary floor and work rights. Scanned or oversized PDFs get clear replies. |
| **Multi-user isolation** | Shared job data lives in one table. Everything personal goes through a `UserStore` scoped to one user, so one user's Telegram buttons can't read or change another's matches. Tests check this. |
| **Scoring** | A rules prefilter skips jobs that clearly don't fit. Claude then scores the rest in batches against the profile: 0–100, a verdict, reasons, gaps and red flags. Results are validated with Pydantic, and server-side refusal fallback is on. |
| **Application kit** | A tailored CV and cover letter rendered with ReportLab, plus screening answers built from the user's own preferences. |
| **Cost control** | Each user has a per-run scoring cap and a daily draft cap. A weekly budget for all users combined pauses scoring and drafting and alerts the owner once. Token usage is recorded per user, and `/users` shows each user's activity and cost for the owner. |
| **Reliability** | A resumable feed cursor, per-user error isolation in the weekly run, and in-place SQLite schema migrations (`PRAGMA user_version`). The single-user v1 database migrates to multi-user with the original owner's data intact. |

## Commands

| User | Owner (`OWNER_CHAT_ID`) |
|---|---|
| `/profile` `/cv` `/prefs` `/pause` `/resume` `/deleteme` `/run` `/pending` `/stats` `/help` | `/invite [note]` creates a single-use invite link · `/users` shows activity and cost per user · `/requests` lists invite requests made before requests closed, to approve or decline |

## Run it

Requires the [melbourne-tech-jobs](https://github.com/Nilish1994/melbourne-tech-jobs) stack on the same Docker host,
a Claude API key and a Telegram bot from [@BotFather](https://t.me/BotFather).

```bash
cp .env.example .env     # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, DATA_KEY (see comments)
docker compose up -d --build
# message your bot: it replies with your chat id -> set OWNER_CHAT_ID, then:
docker compose up -d
```

Then send `/invite` to your bot and share the link. Matching runs at `DAILY_HOUR`, every day or weekly with
`RUN_WEEKDAY=sunday`. Follow-ups are checked daily.

**Cost:** about $1 per user per weekly run with Claude Opus 5.5, plus a few cents per approved application. Set
`SCORE_MODEL=claude-haiku-5-5` for much cheaper screening. `WEEKLY_BUDGET_USD` caps the total.

## Tests

```bash
pip install '.[test]' && pytest
```

46 tests run with fake Claude and Telegram clients, so they make no network calls and cost nothing. They cover:
- **Lifecycle:** strangers get no request button, earlier requests can still be approved, single-use invites, consent, CV upload (including scanned and oversized
  files), preferences, activation and first matches
- **Data:** encryption at rest, `/deleteme` removing every row, and the v1 to v2 migration keeping the owner's data
- **Isolation:** cross-user button presses
- **Running and cost:** budget caps, pause and resume, the weekly run and follow-ups
- **Application kit:** PDFs that contain only profile facts
- **Logging:** request URLs (which contain the bot token) are never logged

## Status

A free, invite-only beta for Melbourne IT jobs. Before any paid use, Adzuna's commercial terms need to be confirmed,
since job data comes from Adzuna's API.

## License

MIT

# Job Match Agent

[![ci](https://github.com/Nilish1994/job-match-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Nilish1994/job-match-agent/actions/workflows/ci.yml)

**An AI job-search assistant that does the searching, screening and writing, and leaves the decisions to you.**

Every morning it pulls new Melbourne tech jobs, has Claude score each one against your profile, and sends the best
matches to Telegram. Tap **Approve** and it drafts a tailored cover letter and tells you which projects to lead your
CV with. You apply on the employer's site and tap **Applied**. A week later it asks how it went and keeps the
whole pipeline in one place.

```
melbourne-tech-jobs ──► /feed/jobs ──► prefilter ──► Claude: score (batches, cached profile)
   (daily Adzuna collection)            (rules)        │
                                                       ▼
                       Telegram ◄── digest of top matches (score ≥ 70)
                          │  ✅ Approve        ──► Claude: cover letter + CV highlights
                          │  📋 Full advert    ──► rewrite using the pasted advert
                          │  📨 Applied        ──► follow-up reminder in 7 days
                          └  🎉/❌/🔇 Outcome   ──► pipeline stats (/stats)
```

## Why it never applies by itself

This is deliberate. The agent never submits an application, fills in an employer's form or sends an email.
- Job boards such as Seek and LinkedIn prohibit automated applications. Accounts that use bots get restricted.
- Recruiters can tell a mass-applied generic application from a tailored one. A few good applications beat many
  generic ones.
- A human check catches what a model can miss: a role that isn't right, a salary that's off, or a wrong company
  name in a letter.

So the expensive work is automated (reading every advert, judging fit, drafting), and the decision and the final
click stay with you, which takes about 5 minutes a day.

## How it works

| Step | Detail |
|---|---|
| **Collect** | Reads new jobs from the [melbourne-tech-jobs](https://github.com/Nilish1994/melbourne-tech-jobs) feed with a resumable cursor. It never calls a job board. |
| **Prefilter** | Cheap rules drop titles that aren't software roles or that you listed as "avoid", and rank the rest by skill overlap. Only these go to Claude, capped per run. |
| **Score** | Claude scores jobs in batches against your profile: 0–100, a verdict, reasons, gaps and red flags such as citizenship requirements. Structured output is validated with Pydantic. Several adverts share one request, and the rules and profile form a stable prefix marked for prompt caching, which takes effect once the profile is long enough to cache. |
| **Digest** | The top matches above `MIN_SCORE` go to Telegram as cards with inline buttons. Only your chat id can use the bot. |
| **Draft** | On approval, Claude writes a 180–260 word cover letter grounded strictly in your profile. It lists anything it couldn't verify under "check before sending", rather than inventing experience. |
| **Track** | SQLite records each job's status (`sent → approved → applied → interview / rejected / no_reply`), with follow-ups and token usage. `/stats` shows the pipeline and the month's Claude cost. |

**Reliability:**
- **Refusals:** server-side refusal fallback is on. Refusals, rate limits and outages are reported in Telegram,
  and the affected jobs are retried on the next run.
- **Crashes:** the feed cursor only advances after a page is stored, so a crash causes a re-fetch, never a gap.
- **Bad updates:** one bad Telegram update can't stop the bot.

## Run it

Requires the melbourne-tech-jobs stack running on the same Docker host, a Claude API key and a Telegram bot.

1. **Create a bot:** message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Configure:**
   ```bash
   cp .env.example .env                          # add ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
   cp profile.example.yaml data/profile.yaml     # describe yourself
   docker compose run --rm agent python -m agent check
   docker compose up -d --build
   ```
3. **Pair your chat:** message your bot. It replies with your chat id. Put that in `TELEGRAM_CHAT_ID` and run
   `docker compose up -d`.
4. **Try it:** send `/run` to match now. After that, it runs daily at `DAILY_HOUR`, 8am Melbourne time by default.

**Cost:** about 100 new jobs a day, scored in batches of 8, plus a few letters comes to roughly $1–2 a day with
Claude Opus 5.5. You can set `SCORE_MODEL=claude-haiku-5-5` for much cheaper screening.

## Tests

```bash
pip install '.[test]' && pytest
```

The tests use fake Claude and Telegram clients, so they make no network calls and cost nothing. They cover:
- the full approve → draft → applied → follow-up → outcome flow
- rewriting a letter from a pasted advert, and skipping
- ignoring every chat except yours, and the first-run setup mode
- rate-limit handling, HTML escaping, feed paging and resuming, and prefilter rules
- the exact request sent to Claude: structured output, cached system prompt, fallback beta

## License

MIT

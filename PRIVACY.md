# JobPilot privacy notice (beta)

JobPilot is a free, invite-only beta run by Nilish Aluthwatta. This notice explains what it stores and why.

## What is stored
- **Your profile.** JobPilot builds it from your CV: name, contact details on your CV, experience, education,
  skills and projects, plus the preferences you choose. It is **encrypted at rest**.
- **Your Telegram chat id**, so the bot can message you.
- **Your job matches**, their scores and your decisions (approved, skipped, applied, outcome).
- **Usage records** (token counts) used to keep costs within budget.

## What is not stored
- **Your CV file.** It is read once to build your profile, then discarded.
- **Passwords, payment details or job-site logins.** JobPilot never applies to jobs for you.

## Who processes it
- **Anthropic.** Your profile text and job adverts are sent to Anthropic's Claude API to score matches, draft
  cover letters and extract your profile from your CV.
- **Telegram.** Messages and files are delivered through Telegram.
- **Adzuna.** Job data comes from Adzuna's public API. Your data is never sent to Adzuna or to employers.

## Your choices
- `/profile` shows what is stored about you.
- `/cv` and `/prefs` update it.
- `/pause` stops matching.
- `/deleteme` permanently deletes your profile, matches and history.

## Questions
Message the bot, or contact the maintainer via [GitHub](https://github.com/Nilish1994).

# Naukri Job Application Bot

An automated bot that searches Naukri.com, scores jobs against your profile,
submits direct applications, refreshes your profile (resume + headline +
summary), and emails an HTML digest. It **learns from screener questions**: any
mandatory question it cannot answer is saved to JSON so you can answer it once,
after which the bot answers it forever.

Designed to run four times a day on GitHub Actions (or Render Cron), and locally
with a dry-run mode.

## Features

- Cookie-based authentication (no stored password), with `NAUKRI_COOKIES`
  support for CI, plus clear diagnostics for expired sessions vs. bot
  protection (`403 Access Denied`)
- Configurable queries, experience window, mandatory/preferred keywords and
  blacklists, plus a match score per job
- PostgreSQL (Neon) persistence with automatic **SQLite fallback** for local
  use; a job is never applied to twice
- **Self-training screener answers** (`config/qa_memory.json`)
- Direct-apply flow with unknown-question detection and retry on later runs
- **Profile refresh**: re-uploads a date-stamped resume (date in the filename
  *and* as a footer on the last PDF page), rotates the headline, and rotates the
  profile summary
- HTML email digest (Gmail/SSL or SMTP+STARTTLS, with automatic transport and
  credential fallback) and local report artifacts
- Structured logs with secret redaction and rotation
- `--self-check` validates config, resume, database, session and browser
  without touching Naukri
- Dry-run, headful, per-run limit and profile-only modes

## Project structure

```
.
├── .github/workflows/apply_bot.yml
├── assets/Subham_Resume.pdf
├── config/
│   ├── profile_data.json     # your skills, CTC, notice period, custom answers
│   ├── qa_memory.json        # self-trained screener answers
│   └── search_config.json    # queries, filters, limits, profile refresh
├── src/
│   ├── auth.py               # browser launch, cookies, session checks
│   ├── crawler.py            # search, match, apply
│   ├── db.py                 # Postgres + SQLite job history
│   ├── forms.py              # screener resolver (rules + learned answers)
│   ├── local_auth.py         # interactive session capture
│   ├── logging_setup.py      # console/file logging + redaction
│   ├── matcher.py            # relevance scoring
│   ├── notifier.py           # HTML digest + email
│   ├── profile.py            # resume/headline/summary refresh
│   └── qa_store.py           # question memory
├── tests/
├── main.py
├── requirements.txt
└── requirements-dev.txt
```

## Setup

1. **Install dependencies**

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   python -m playwright install chromium
   ```

2. **Capture your Naukri session**

   ```bash
   python -m src.local_auth
   ```

   Log in when the browser opens, press ENTER, and `auth_state.json` is saved.
   (Do not commit this file - it is gitignored.)

3. **Configure environment** - copy `.env.example` to `.env`:

   | Variable | Purpose |
   | --- | --- |
   | `NAUKRI_COOKIES` | Optional JSON cookie array (used when no `auth_state.json`) |
   | `DATABASE_URL` | PostgreSQL URL; omit to use `data/jobs.db` (SQLite) |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `MAIL_TO` | Email digest |
   | `SENDER_EMAIL`, `SENDER_APP_PASSWORD`, `RECIPIENT_EMAIL` | Legacy aliases, used automatically when the pair above does not authenticate |
   | `STORAGE_STATE_PATH` | Override the session file path |
   | `RESUME_PATH` | Override `assets/Subham_Resume.pdf` |
   | `QA_MEMORY_PATH`, `SQLITE_PATH`, `LOG_DIR`, `LOG_LEVEL` | Optional overrides |

   The SMTP transport is inferred from the port (`465` = implicit TLS,
   `587`/`25` = STARTTLS) unless `SMTP_SSL` says otherwise; if the first
   credential pair is rejected the bot retries with the other pair and the
   alternate transport, and logs which one worked.

4. **Customise**
   - `config/profile_data.json` - skills, CTC, notice period, resume path, and
     `custom_answers` for questions you want answered verbatim.
   - `config/search_config.json` - queries, filters, limits, delays, and the
     `profile_refresh` block.
   - `assets/Subham_Resume.pdf` - your real resume.

5. **Validate everything**

   ```bash
   python main.py --self-check             # config, resume, DB, session
   python main.py --self-check --check-browser
   ```

## Self-training screener answers

When a mandatory screener question cannot be resolved, the bot:

1. records it in `config/qa_memory.json` with status `pending`, and
2. writes `reports/pending_questions.md` with a summary.

Open the JSON, fill in `answer` once, and commit it:

```json
{
  "questions": {
    "do you have a valid passport": {
      "question": "Do you have a valid passport?",
      "answer": "Yes",
      "status": "answered",
      "times_asked": 3
    }
  }
}
```

Answers the bot derives from rules are saved automatically with status
`learned`, so slightly different wordings are answered instantly next time.
`custom_answers` in `profile_data.json` also works and takes priority.

Jobs skipped for an unanswered question are **retried automatically** (up to
two extra attempts) once an answer exists.

## Usage

```bash
python main.py                    # search, apply, refresh profile, email digest
python main.py --dry-run          # full flow without applying or emailing
python main.py --browser-mode headless  # force headless (fails on blocked networks)
python main.py --headful              # always show the browser window
python main.py --browser-mode auto  # default: headless, auto-retry headful if blocked
python main.py --limit 5          # cap applications this run
python main.py --no-profile-refresh
python main.py --only-profile     # refresh resume/headline/summary and exit
python main.py --no-email         # write reports/digest_*.html only
python main.py --save-session     # persist refreshed cookies locally
python main.py --log-level DEBUG
```

Every run writes:

- `logs/naukri_bot.log` (rotated, secrets redacted)
- `reports/run_<timestamp>.json` (full machine-readable report)
- `reports/digest_<timestamp>.html` (same digest that is emailed)
- `reports/pending_questions.md` when answers are missing
- `reports/screenshots/` on failures, helpful for selector drift

## Troubleshooting

**`403 Access Denied` / bot protection** - Naukri (Akamai) rejects headless
Chromium from some networks/IPs. In the default `--browser-mode auto` the bot
detects the block, logs it, and **automatically relaunches with a visible
window** when a display is available, then continues the run. If you explicitly
pass `--browser-mode headless` it stops with a clear message instead of
pretending the session expired. The GitHub workflow runs `--headful` under
`xvfb-run` for the same reason.

**Profile page shows an empty skeleton** - Naukri's logged-in React apps break
when their DOM is over-patched; that is why `playwright-stealth` is *not* used
(only the launch flags plus a `navigator.webdriver` mask are applied).

**`Missing session` in CI, or the secret will not save** - GitHub Actions
secrets are limited to 48 KB. Playwright's `auth_state.json` is usually ~40 KB,
so plain `base64` (~53 KB) is **too large**. Compress it first:

```bash
gzip -9 -c auth_state.json | base64 -w 0   # ~9 KB, fits
```

Paste that into `NAUKRI_STORAGE_STATE`; the workflow detects gzip automatically
and also accepts plain base64 or raw JSON. Alternatively, store only the cookie
array from `auth_state.json` in `NAUKRI_COOKIES`.

**Session expired** - re-run `python -m src.local_auth` and update the
`NAUKRI_STORAGE_STATE` secret.

**Questions still skipped** - check `reports/pending_questions.md` and add the
answers to `config/qa_memory.json`; the affected jobs are retried automatically
on the next run (up to two extra attempts).

## Tests

```bash
pytest -q
```

The suite is offline: it covers matching, answer resolution, QA memory,
SQLite persistence, digest escaping, date-stamped resume generation, and the
crawler URL/id helpers.

## Scheduling

`.github/workflows/apply_bot.yml` runs four times a day and can also be
triggered manually from the Actions tab:

| IST | UTC | Cron |
| --- | --- | --- |
| 8:00 AM | 02:30 | `30 2 * * *` |
| 11:00 AM | 05:30 | `30 5 * * *` |
| 3:00 PM | 09:30 | `30 9 * * *` |
| 6:00 PM | 12:30 | `30 12 * * *` |

GitHub's scheduler is best-effort and may start a run a few minutes late during
peak load. Add these repository secrets:

| Secret | Description |
| --- | --- |
| `NAUKRI_STORAGE_STATE` | **gzip + base64** of `auth_state.json` (`gzip -9 -c auth_state.json \| base64 -w 0`) |
| `NAUKRI_COOKIES` | Alternative: JSON cookie array only |
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `SMTP_*` / `MAIL_TO` | SMTP settings (or the legacy `SENDER_*` / `RECIPIENT_EMAIL`) |

Naukri sessions expire periodically. Re-run `python -m src.local_auth` and
refresh the secret when the workflow reports an authentication failure.

## Deployment

### Option A: GitHub Actions (recommended)

No server required; the workflow installs Chromium, restores the session, runs
the bot headful under `xvfb`, and uploads `reports/` and `logs/`.

1. Push the repository to GitHub (private recommended).
2. Go to **Settings -> Secrets and variables -> Actions** and add
   `NAUKRI_STORAGE_STATE`, `DATABASE_URL` and the `SMTP_*` / `MAIL_TO` values.
3. Open the **Actions** tab, select **Naukri Automation Pipeline**, and run it
   once with **Run workflow** to verify.
4. Scheduled runs start automatically. On a public repo Actions minutes are
   free; private repos have 2,000 free Linux minutes per month, so monitor
   **Settings -> Billing** if you run 4x/day.

### Option B: Render Cron Job

Included for teams that prefer a private repo with cheap scheduled compute.
Render Cron Jobs bill per running minute (around $1-3/month for this workload)
and **do not** support persistent disks, so `DATABASE_URL` (Neon) is required.

1. Push the repo to GitHub. `render.yaml`, `Dockerfile`, and `.dockerignore` are
   already present.
2. In Render, choose **New -> Blueprint**, connect the repo, and apply.
   Alternatively create **New -> Cron Job**, select Docker, and set the schedule
   `30 2,5,9,12 * * *` (the times are always UTC).
3. Fill in `NAUKRI_COOKIES` (a JSON cookie array), `DATABASE_URL`, and the SMTP
   variables. Use `SMTP_PORT=587` (STARTTLS) or `465` (SSL).
4. Trigger a manual run and confirm the exit code is `0` and the digest email
   arrives.

Render blocks outbound SMTP port `25`; `465` and `587` are allowed on paid
compute. If your provider blocks them, switch to an email API.

## Disclaimer

Use responsibly and in accordance with Naukri.com's terms of service. Automated
applications may be rate-limited or blocked by the platform. Screener answers
are submitted on your behalf - review `config/qa_memory.json` periodically.

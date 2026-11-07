# AI job ranker

A personal job-search agent. Twice a day it pulls new postings from 84 companies' own job boards, three remote job feeds, and LinkedIn and Indeed searches. It filters them down to the roles that fit, has Claude score each posting against a written candidate profile, and pushes the best matches to a phone as ranked alerts.

I built it with Claude Code during my own job search in October 2026. In its first week it scored 120 postings; by November 7 it had scored 1,201.

## How it works

1. **Fetch.** Reads Greenhouse, Ashby, and Lever job boards directly through their public APIs, plus the Himalayas, Jobicy, and We Work Remotely feeds. LinkedIn is read from its public job search, paced a few seconds apart; Indeed comes through [JobSpy](https://github.com/speedyapply/JobSpy).
2. **Filter.** Keeps titles that match the search (implementation, onboarding, customer success, solutions, AI ops) and drops senior, engineering, and sales titles, non-US roles, and jobs whose posted pay tops out under the floor. The same job on two boards counts once, even when the boards spell the company differently ("Fieldwire by Hilti" and "Fieldwire", "Acme, Inc." and "Acme"). LinkedIn and Indeed results swing from run to run, so their jobs stay on the list for 3 days after they were last seen instead of flickering off.
3. **Score.** Sends new postings to Claude in batches of 12 with the profile and a rubric. Before scoring, any company it hasn't seen yet gets one employee-rating lookup (Glassdoor or Indeed, plus recent layoffs) through Claude's web search, saved with the date and source. So far it has looked up 479 companies. Each job gets three scores out of 5:
   - would a hiring manager pick this candidate
   - does the candidate want this job
   - is it a good job (pay, company health, growth)
4. **Rank.** Mutual fit counts 70% and job quality 30%. A job loses half a point when either side of the fit is weak, and another half point when a staffing agency posted it, since agencies hide the real employer. The ranked list is saved as `ranked.md` to read and `ranked.json` for the apply step.
5. **Alert.** The morning run sends the top 5 to apply for. The afternoon run sends only new jobs that crack the top 10. Each alert has Applied and Skip buttons that take the job off the list.
6. **Kits.** For the top jobs, Claude drafts an application kit in `kits/`: a resume reworded for the posting (Markdown and a one-page PDF), a cover letter, three points to stress, likely screening questions, and an outreach note. It also lists referral leads: people in your exported contacts who work there, and public LinkedIn profiles from that company's customer-facing team. Drafts only; nothing is sent.
7. **Outcomes.** On Mondays, any job marked Applied 5 or more days ago with no answer gets a "Heard back?" alert with Interview, Rejected, and No reply buttons (at most 3 Mondays per job). Once there are 5 real outcomes, they go back into the scoring prompt so the "would they pick this candidate" score learns from what actually happened. The weekly check-in shows applied, interview, offer, rejected, and waiting counts.

## Auto-apply helper

`apply/` fills out application forms for the top ranked jobs, but **only submits after you tap "Send it" on your phone** for that exact application.

1. **Prep** (twice a day). Takes the top ranked jobs not yet handled (6 per run by default, staffing agencies skipped), opens each form in Chrome, reads every field, and has Claude write the answers from your fixed facts, resume, and work history. It fills the form without submitting to prove every required field takes an answer, then sends a phone alert showing every written answer and the cover letter, with Send it and Skip buttons.
2. **Send** (every 3 minutes, checking every 10 seconds). Reads the taps. Send it re-opens the form, fills it the same way, submits it, waits for a confirmation page, and marks the job Applied in the job list. Skip drops it.
3. Anything it can't finish (a sign-in wall, a CAPTCHA, a question only you can answer) comes back as "Apply yourself" with a link.

## Guardrails

- **Posting text is untrusted.** The scorer prompt fences each posting in tags and tells Claude never to follow instructions found inside them. Scoring runs Claude with no tools; the rating lookup gets web search only.
- **Nothing is submitted without a tap.** The apply step submits only on a Send it tap for that application. It never signs in, makes accounts, fills password fields, or solves a CAPTCHA, and it leaves background-check questions for you.
- **Answers must fit the form.** Choices Claude gives that don't match a field's real options are dropped, and if the form's questions changed since the answers were written, it stops instead of sending.
- **Invented numbers get flagged.** Any number in a kit or written answer that isn't in the resume, facts, or posting is listed under "check" before you use it.
- **No invented pay.** Any pay figure Claude reports that isn't in the posting text is blanked to "not listed" (`audit_pay` in `scorer.py`).
- **Scoring can't break a run.** If Claude is unavailable, jobs still alert as "not ranked yet" and get scored on a later run.
- **Bad output is dropped.** Scores outside 1-5, unknown job ids, and bad location values are thrown out.
- **No double alerts.** The same title at the same company won't alert again for 45 days.
- **Overlapping runs are blocked** with a lock file.
- **A weekly "still running" check-in** goes out, so a quiet phone means no new jobs, not a broken watcher.

## Setup

Requires Python 3.9+ and the [Claude Code](https://claude.com/claude-code) CLI signed in (`claude auth login`). The watcher itself uses only the standard library.

1. `cp config.example.json config.json` and set `ntfy_topic` and `marks_topic` to two long random strings. Anyone who knows a topic can read it, so treat them like passwords.
2. `cp profile.example.md profile.md` and write your own profile and rubric.
3. Install the [ntfy](https://ntfy.sh) app and subscribe to your `ntfy_topic`.
4. Optional, for Indeed and LinkedIn through JobSpy: create a Python 3.12 virtual environment at `.jobspy/` and `pip install python-jobspy`. Set `"enabled": false` under `indeed` in `config.json` to skip it.
5. Optional, for kits: `cp resume.example.md resume.md` and write your own. Kits render PDFs with Google Chrome.
6. Optional, for the auto-apply helper: create a virtual environment at `.apply/` and `pip install playwright` (it drives your installed Chrome). `cp apply/facts.example.json apply/facts.json` and fill it in, put `AI_Resume*.pdf` and `CS_Resume*.pdf` (each with a same-named `.html` copy) in `resumes/`, and optionally add work history notes in `apply/history.md`. Edit the paths in the two `apply/*.plist` files and load them with `launchctl`.
7. Try it: `python3 job_watch.py --dry-run` prints matches and sends nothing.
8. Schedule it (cron or launchd). The first run at or after 8 AM sends the morning list; the first at or after 4 PM sends the afternoon update.

## Commands

| Command | What it does |
| --- | --- |
| `python3 job_watch.py --dry-run` | Print matches only; send and save nothing |
| `python3 job_watch.py --seed` | First run: mark everything open now as seen and send one summary |
| `python3 job_watch.py` | Normal run; picks morning, afternoon, or tap-reading mode by the clock |
| `python3 job_watch.py --rank-now` | Score and rank right away |
| `python3 job_watch.py --mark applied "acme onboarding"` | Mark a job by name: applied, skip, interview, rejected, noreply, or offer |
| `.apply/bin/python apply/apply.py --prep` | Fill forms for the top jobs and send them to your phone for review |
| `.apply/bin/python apply/apply.py --send` | Read Send it / Skip taps and submit the approved ones |
| `.apply/bin/python apply/apply.py --dry URL` | Write answers for one form, fill it, screenshot it, and stop before submit |
| `.apply/bin/python apply/check_waiting.py` | Re-fill every application waiting for a tap to make sure it's still ready |
| `python3 -m unittest discover -s tests` | Run the 31 tests |

## Files

| File | Role |
| --- | --- |
| `job_watch.py` | Fetching, filtering, alerts, and the run loop |
| `scorer.py` | Claude prompt, reply parsing, and the pay audit |
| `ranker.py` | Composite score, agency penalty, dedupe key, and `ranked.md` / `ranked.json` output |
| `marks.py` | Reads Applied, Skip, and outcome taps back from ntfy |
| `ratings.py` | One-time employee-rating lookup per company |
| `kits.py` | Application kits for the top jobs |
| `referrals.py` | Referral leads from your contacts and public LinkedIn profiles |
| `apply/apply.py` | Auto-apply helper: prep, phone review, and send on tap |
| `apply/form.py` | Reads, fills, and submits application forms in Chrome (Playwright) |
| `apply/facts.example.json` | Example fixed answers for application forms |
| `resume.example.md` | Example resume for kits |
| `jobspy_fetch.py` | Runs inside `.jobspy/` for Indeed and LinkedIn |
| `config.example.json` | Companies, title rules, pay floors, and searches |
| `profile.example.md` | Example candidate profile and scoring rubric |

## License

MIT

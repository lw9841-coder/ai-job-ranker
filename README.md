# AI job ranker

A personal job-search agent. Twice a day it pulls new postings from 84 companies' own job boards, three remote job feeds, and LinkedIn and Indeed searches. It filters them down to the roles that fit, has Claude score each posting against a written candidate profile, and pushes the best matches to a phone as ranked alerts.

I built it with Claude Code during my own job search in October 2026. In its first week it scored 120 postings.

## How it works

1. **Fetch.** Reads Greenhouse, Ashby, and Lever job boards directly through their public APIs, plus the Himalayas, Jobicy, and We Work Remotely feeds. LinkedIn is read from its public job search, paced a few seconds apart; Indeed comes through [JobSpy](https://github.com/speedyapply/JobSpy).
2. **Filter.** Keeps titles that match the search (implementation, onboarding, customer success, solutions, AI ops) and drops senior, engineering, and sales titles, non-US roles, and jobs whose posted pay tops out under the floor.
3. **Score.** Sends new postings to Claude in batches of 12 with the profile and a rubric. Each job gets three scores out of 5:
   - would a hiring manager pick this candidate
   - does the candidate want this job
   - is it a good job (pay, company health, growth)
4. **Rank.** Mutual fit counts 70% and job quality 30%. A job loses half a point when either side of the fit is weak.
5. **Alert.** The morning run sends the top 5 to apply for. The afternoon run sends only new jobs that crack the top 10. Each alert has Applied and Skip buttons that take the job off the list.

## Guardrails

- **Posting text is untrusted.** The scorer prompt fences each posting in tags and tells Claude never to follow instructions found inside them. Claude runs with no tools.
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
5. Try it: `python3 job_watch.py --dry-run` prints matches and sends nothing.
6. Schedule it (cron or launchd). The first run at or after 8 AM sends the morning list; the first at or after 4 PM sends the afternoon update.

## Commands

| Command | What it does |
| --- | --- |
| `python3 job_watch.py --dry-run` | Print matches only; send and save nothing |
| `python3 job_watch.py --seed` | First run: mark everything open now as seen and send one summary |
| `python3 job_watch.py` | Normal run; picks morning, afternoon, or tap-reading mode by the clock |
| `python3 job_watch.py --rank-now` | Score and rank right away |
| `python3 -m unittest discover -s tests` | Run the 22 tests |

## Files

| File | Role |
| --- | --- |
| `job_watch.py` | Fetching, filtering, alerts, and the run loop |
| `scorer.py` | Claude prompt, reply parsing, and the pay audit |
| `ranker.py` | Composite score, ordering, and `ranked.md` output |
| `marks.py` | Reads Applied and Skip taps back from ntfy |
| `jobspy_fetch.py` | Runs inside `.jobspy/` for Indeed and LinkedIn |
| `config.example.json` | Companies, title rules, pay floors, and searches |
| `profile.example.md` | Example candidate profile and scoring rubric |

## License

MIT

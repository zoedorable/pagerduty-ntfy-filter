# pagerduty-ntfy-filter

Less noise on PagerDuty duty: the tool regularly polls PagerDuty for open incidents and
sends you a **loud** push via [ntfy](https://ntfy.sh) only when **one of your customers**
is in the title and the service matches. Everything else arrives as a **silent**
notification or is ignored.

There's also a dashboard in the browser where you can acknowledge and resolve incidents
directly in PagerDuty or hand off the rest of your shift.

## Features

- **Filter by customer and service:** customer list as a plain text file, matched as a
  substring of the incident title (case-insensitive), also editable in the dashboard.
  Optional service filter, e.g. only `CSOC (High)` loud.
- **Loud or silent:** loud alarms with ntfy priority *urgent*. Silent notifications
  without sound, optionally on a separate topic.
- **No double alarms:** each incident is reported only once, even after a restart.
- **Console** (`http://localhost:8790`, local only):
  - sidebar with status, on-call handoff, LAS and customers; main pane with the open
    incidents of your customers, the acknowledged ones ("Acked by") and the rest
  - Ack and Resolve with one click, multiselect, keyboard shortcuts (`?` shows them)
  - views as filters, search, history of all decisions, dark and light theme
  - change settings (silent notifications, service filter, schedule, time window …) live,
    without a restart
  - the previous dashboard is still at `/classic`
- **PAW / standalone:** open `console.html` directly from disk (`file://`). It then talks
  to the PagerDuty API itself, no Python and no ntfy needed; alerts are in-tab only.
- **Browser extension** (optional, Chrome/Edge): compact popup with Ack/Resolve, badge
  with the number of open incidents of your customers, icon blinks on loud alarms
- **Only while on call** (`ONLY_ON_CALL=true`): if PagerDuty says you're not on call right
  now, the filter pauses and sends nothing.
- **Hand off on-call:** a button creates an override from now until the end of the shift
  for another user (`OVERRIDE_USER`, e.g. a dummy user). After a break, "Take back on-call"
  gets the on-call back (deletes the override).
- **LAS (Last Analyst Standing):** a button turns on PagerDuty push notifications until
  18:00 (all customers), after that the filter removes them again automatically. "On (no end)"
  turns them on without an end time, until you turn them off yourself. In Settings you pick
  which contacts (push, SMS, call, email) and which urgency.
- **Working hours** (`ONLY_IN_WINDOW=true`): only active at fixed times.

## Quick start

Requirements: Python 3.9 or newer and a PagerDuty API key.

```bash
git clone https://github.com/mstump2s/pagerduty-ntfy-filter.git
cd pagerduty-ntfy-filter
cp config.env.example config.env          # fill in PD_API_TOKEN and NTFY_TOPIC
cp customers.txt.example customers.txt    # enter your own customers
chmod +x pd-filter.sh
./pd-filter.sh start                      # starts in the background
./pd-filter.sh view                       # open the dashboard
```

On Windows: `start.bat`, `stop.bat`, `view.bat`.

Then subscribe to your topic in the [ntfy app](https://ntfy.sh). To check how a title
would be handled without PagerDuty: `./pd-filter.sh test "Title" "Service"`.

All settings and details are in the [guide](GUIDE.md).

## Security

- `config.env` (API key, ntfy topics) and `customers.txt` are in `.gitignore` and
  don't belong in the repo.
- ntfy topics on ntfy.sh are publicly readable if you know the name: pick a long,
  random name. Privacy mode (`QUIET`) is always on: no incident titles end up in the
  log and no customer data in the ntfy notifications, they only say "Incident".
- The dashboard only listens on `127.0.0.1` and only accepts actions from its own page.

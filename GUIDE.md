# PD filter – guide

Regularly polls PagerDuty for open (triggered) incidents and sends you a **loud** push
via [ntfy](https://ntfy.sh) only when one of your customers is in the title **and** the
service matches (default: `CSOC (High)`). Other incidents optionally arrive as a
**silent** notification (no sound) or are ignored.
There's also a dashboard in the browser with Ack and Resolve buttons.

Requirements: Python 3.9 or newer. `requests` is installed automatically on the first
start in a separate `.venv` in the folder.

## 1. Setup

1. **Create config.env:** copy `config.env.example` to `config.env` and fill it in:
   - `PD_API_TOKEN` – personal PagerDuty API key
     (PagerDuty → My Profile → User Settings → API Access → Create API User Token)
   - `NTFY_TOPIC` – a long, hard-to-guess name, e.g. `pd_max_8f3k2q91`
   - optional `NTFY_TOPIC_SILENT` – separate topic for silent notifications
   - optional `PD_FROM_EMAIL` – only needed with an *account* API key (for Ack/Resolve)
   - The remaining values have sensible defaults and are explained in the file.
2. **Enter customers:** copy `customers.txt.example` to `customers.txt` and enter one
   customer per line.
   Case doesn't matter; it's a match if the text appears anywhere in the incident title.
   Lines starting with `#` are comments. Changes apply from the next poll, without a
   restart.
   With TextEdit, make sure to save as **plain text** (Format → Make Plain Text).
   Alternatively in the dashboard: the "Add customer" field or the × next to a customer.
   Without customers, **nothing** is reported loud (not everything).
3. **ntfy app:** install it on your phone and subscribe to your topic (or both topics).
   For loud alarms, allow sound / breaking through "Do Not Disturb" for priority
   "urgent" in the app.

## 2. Start and stop

**Mac / Linux** (in the folder, once `chmod +x pd-filter.sh`):

| Command | Effect |
|---|---|
| `./pd-filter.sh start` | starts in the background (creates the `.venv` the first time) |
| `./pd-filter.sh stop` | stops |
| `./pd-filter.sh status` | is it running? |
| `./pd-filter.sh log` | follow the log (Ctrl+C only ends the display) |
| `./pd-filter.sh view` | open the dashboard in the browser |
| `./pd-filter.sh test "Title" ["Service"]` | check whether a title would be loud/silent/ignored |

The old German commands (`an`, `aus`, `ansicht`) still work.

**Windows:** `start.bat` (starts minimized in a window "PD-Filter"), `stop.bat`, `view.bat`.

After changing `config.env`, stop and start once.

## 3. Console

`http://localhost:8790` (port configurable via `WEB_PORT`, only reachable from your own
machine). The sidebar shows status, on-call (hand off / take back), PagerDuty
notifications (LAS), the views and your customers. The main pane lists the incidents;
the views (Loud, Silent, Other service, Not my customer) and the search field filter it.
**Ack** and **Resolve** update the incident directly in PagerDuty (without confirmation).
The previous dashboard is still available at `http://localhost:8790/classic`.

Keyboard shortcuts (`?` shows them): `j`/`k` move, `x` select, `a` ack, `r` resolve
(the selection, otherwise the focused row), `o` or Enter opens the incident in
PagerDuty, `/` search, `1`–`5` views, `,` settings, `Esc` clears the selection.
The theme link at the bottom of the sidebar switches between auto, dark and light.

At the very top are the open, not yet acknowledged incidents of **your customers**
(with Ack/Resolve), below them the acknowledged incidents: all that **you** acknowledged
(first), and those of your customers that someone else acknowledged. The "Acked by"
column shows who it was and, if different, who the incident is assigned to. All with a
Resolve button. During LAS or "On (no end)", **all customers** count as yours (display,
extension counter; the ntfy notifications stay unchanged). The table of open incidents of
other customers contains the rest. The tool recognizes your PagerDuty user from the
personal API key; with an account key, set `PD_FROM_EMAIL` for this, otherwise all
acknowledged incidents are shown.

Under **Settings** you can change: silent notifications (`SILENT_FOR`), service
filter, "only while on call", override user, time window, poll interval and the selection
for LAS/shift. Changes apply immediately, without a restart, and are saved to
`config.env`. API key, ntfy topics and port deliberately stay in the file only.

Privacy mode (`QUIET`) is always on and can't be turned off in the dashboard or in
`config.env`: no incident titles end up in the log and no customer data at ntfy.
The notification only says "Incident" (loud or silent as usual), the details are in
the dashboard. Tapping it opens the incident in PagerDuty.

**Optional browser extension** (Chrome, Edge): open `chrome://extensions`, turn on
**Developer mode** at the top right, click **Load unpacked** and select the `extension`
folder. Clicking the icon opens a compact view (status, hand off shift, open incidents
of your customers and your acknowledged incidents with Ack/Resolve, link to the full
dashboard; also directly at `http://localhost:8790/mini`).
The badge shows the open incidents of your customers (`II` = paused, `!` = error,
`off` = filter not running). The icon is green (nothing open), red (customer incident
open) and **blinks red** while a loud alarm (customer + matching service) isn't
acknowledged. After updating the files, click ↻ on PD filter in `chrome://extensions`.
If you change `WEB_PORT`, adjust the port in the three files in `extension/` and reload
the extension.

### Standalone on the PAW (without Python and ntfy)

`console.html` also works on its own: copy the file to the PAW and open it in Edge or
Chrome (double click, `file://`). Without the Python filter it talks to the PagerDuty
API directly from the browser (PagerDuty allows that, CORS). On first open it asks for
your personal API key; with an account key, also enter your PagerDuty email.

- **Key:** "Remember on this computer" keeps it in the browser's local storage.
  Pages opened from disk share that storage, so other local HTML files could read it.
  Settings → "Forget key and disconnect" removes it.
- **Customers and settings** live in this browser only (Settings → "Customer list"
  takes a pasted `customers.txt`).
- **Alerts:** no ntfy. A new loud incident plays a sound (only after you clicked
  somewhere on the page once, browser rule), blinks the tab title and shows a toast.
  The favicon shows the number of open incidents of your customers.
- **LAS and handoff** work the same, but their state is kept in the browser that
  started them: LAS started on the Mac isn't shown on the PAW and the other way round.
  LAS ends automatically only while the page is open, otherwise when it's opened next.
- On the Mac you can test this mode with `http://localhost:8790/?direct=1`.

## 4. What happens when

| Customer in title | Service matches | Result |
|---|---|---|
| yes | yes | 🔔 **loud** (ntfy priority urgent) |
| yes | no | 🔕 silent if `SILENT_FOR=customers` or `all` |
| no | yes/no | 🔕 silent only with `SILENT_FOR=all`, otherwise ignored |

Each incident is reported only once (remembered in `seen.json`). If sending to ntfy
fails, it's retried on the next poll.

**Working hours:** with `ONLY_IN_WINDOW=true` the filter is only active from
`START_HOUR` to `END_HOUR` (default 8–18, with `WEEKDAYS_ONLY=true` only Mon–Fri) and
pauses otherwise, without exiting.

**Only while on call:** with `ONLY_ON_CALL=true` the filter is only active while you're
on call in a PagerDuty schedule (checked once a minute). Otherwise it pauses: no polling,
no notifications. If the status can't be checked (own user unknown, PagerDuty
unreachable), it keeps running to be safe.

**Hand off on-call:** while you're on call, the dashboard shows your schedule at the top
and a **"Hand off on-call"** button (to CSOC Dummy by default). One click creates an
override in PagerDuty (without confirmation) from now until the end of the shift for
`OVERRIDE_USER` (default `CSOC Dummy`, name or email). With `ONLY_ON_CALL=true` the
filter then pauses automatically.

**LAS (Last Analyst Standing):** on days when you take over all customers from 16:00,
click **"LAS on until 18:00"** in the dashboard (or mini view). The filter then makes
sure there's a push rule in PagerDuty for every device with the PagerDuty app (immediate,
high urgency); existing push rules (including delayed ones) are deleted and replaced by
an immediate one. At `LAS_END_HOUR` (default 18:00) it deletes these push rules again,
even if it was off in between (then on the next start). Before that, use **"Turn off"**.
Other rules, e.g. a call after 5 minutes, are never touched.

Under **Settings** you pick which contacts the PagerDuty notification turns on (devices
with the PagerDuty app, SMS, call, email; `LAS_CONTACTS`, nothing selected = all push
devices) and for which urgency (`LAS_URGENCY`: high, low or both). If a matching rule for
SMS, call or email already exists, it simply stays and isn't deleted at the end. The rules
are remembered in `las.json`. A new device only counts once you've signed in to the
PagerDuty app on it. The ntfy notifications stay unchanged during LAS.

The **"On (no end)"** button turns on the same rules, but without an automatic end: they
stay until you click **"Turn off"**.

**Take back on-call:** while an override you created is running, the dashboard (and the
mini view) shows a **"Take back on-call"** button, e.g. after a break. It deletes the
override in PagerDuty again; if it has already started, it's shortened to now. The filter
remembers the overrides it created in `overrides.json`, also across restarts. Overrides
created directly in PagerDuty are unknown to it.

The old German config values (`SILENT_FOR=kunden|alle|keine`,
`LAS_URGENCY=hoch|niedrig|beide`) are still accepted.

## 5. Files

| File | Content |
|---|---|
| `pd_filter.py` | the program |
| `console.html` | console (served by the filter, or standalone via `file://`) |
| `dashboard.html` | previous dashboard (`/classic`) |
| `config.env` | your settings incl. API key (**don't share**) |
| `customers.txt` | your customer list (not in the repo, template: `customers.txt.example`) |
| `seen.json`, `overrides.json`, `las.json`, `pd_filter.log`, `pd_filter.pid` | created automatically |

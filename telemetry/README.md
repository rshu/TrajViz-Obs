# TrajViz-Obs telemetry service

Telemetry is off by default. When enabled, Claude Code, OpenCode, and Pi queue allowlisted usage events locally and send them in the background during plugin activity. The collector validates and deduplicates uploads in SQLite. The local service saves a JSON snapshot of the report. The separate publisher refreshes HTML and JSON on a schedule.

Run commands from the TrajViz-Obs repository root with **Python 3.11+**. The service uses only the Python standard library; no `pip install` is needed.

If you do not already have Python 3.11+ selected, use [uv](https://docs.astral.sh/uv/) to create a small environment. In a macOS/Linux shell:

```sh
uv venv --python 3.11 telemetry/.venv
source telemetry/.venv/bin/activate
python --version
```

The examples below use that environment's `python`. In a new terminal, activate it again before running them.

## Try it locally

1. Start the local service. With no credential registry configured, it accepts loopback uploads without a token.

   ```sh
   python -m telemetry --database "$HOME/.local/state/trajviz-obs/usage.sqlite3" serve
   ```

2. Open [the local report](http://127.0.0.1:8765/) in a browser. It shows the most recent 30 UTC days. The server writes `usage.sqlite3.report.json` next to the database at startup and refreshes it when you reload the report.

3. In Claude Code, OpenCode, or Pi, run `/obs enable_telemetry`, then `/obs help` and `/obs telemetry` to check delivery. The enable command uses the local collector on port 8765 when no endpoint is configured. For a custom port, run `/obs telemetry_destination http://127.0.0.1:PORT`.

## Plugin configuration

| Launch environment variable | Setting |
| --- | --- |
| `TRAJVIZ_OBS_TELEMETRY` | Set to `1` to opt in at launch when no consent preference has been saved. Also set `TRAJVIZ_OBS_TELEMETRY_URL` or save a destination; launch opt-in alone does not select the loopback default. |
| `TRAJVIZ_OBS_TELEMETRY_URL` | Full URL ending in `/v1/events`, with no user info, query, or fragment. An explicit `/obs enable_telemetry` uses `http://127.0.0.1:8765/v1/events` when this is unset. HTTPS is required except on HTTP loopback (`localhost`, `127.0.0.1`, or `::1`). |
| `TRAJVIZ_OBS_TELEMETRY_TOKEN` | Bearer token issued by the collector. Required for authenticated receivers. |
| `TRAJVIZ_OBS_TELEMETRY_STATE_DIR` | Optional absolute path for local queue state. Defaults to `~/.local/state/trajviz-obs/telemetry` on macOS and Linux, honoring `XDG_STATE_HOME`, or `%LOCALAPPDATA%\trajviz-obs\telemetry` on Windows when set. |

Set these in Claude Code’s or Pi’s launch environment, or the OpenCode **server’s** launch environment for both terminal and Desktop clients. Keep telemetry and state-directory variables out of Claude Code's `settings.env`; conflicting settings disable telemetry controls. On macOS, apps opened from Finder do not inherit terminal exports. Restart the host or server after changing its launch environment. The `/obs` configuration commands below take effect without restarting.

`/obs enable_telemetry` and `/obs disable_telemetry` save the choice per adapter in the local state directory. Changes apply without a restart. Client upgrades preserve that choice. An explicit disable stops recording and pauses queued uploads until telemetry is enabled again. Reopen existing dashboards after enabling telemetry to track browser actions.

`/obs telemetry` reads local status, including the queue, last upload, and retry deadline, without sending an event. The queue holds up to 512 events per adapter and endpoint and retries on later activity for up to seven days. Temporary failures back off and respect `Retry-After` within the retry limit. Authentication failures preserve the queue for a replacement token. Upload failures do not block plugin commands.

Use `/obs telemetry_destination` to see the current collector or `/obs telemetry_destination https://telemetry.example.com` to change it during a session. Base URLs gain `/v1/events`; `default` restores the launch URL or loopback default. In Claude Code and Pi, append `--use-launch-token` to bind the launch token to a new destination. OpenCode sends its token only to the exact launch endpoint; update the server environment and restart it before changing an authenticated destination. Each endpoint has its own queue and installation ID, and changing it preserves consent.

## Collector configuration

For remote collection, load the following application with a production WSGI server behind HTTPS. Set `TRAJVIZ_OBS_INGEST_CREDENTIALS` to the absolute path of the registry or pass `credentials=` to the factory. Production always requires authentication. Forward `Authorization`, enforce a 16 KiB body limit with rate and read-time limits at the proxy, and keep credentials and IP addresses out of access logs. When the publisher runs separately, it needs the database and no address key.

```python
from telemetry.collector import create_production_app

application = create_production_app("/var/lib/trajviz-obs/usage.sqlite3")
```

| Setting | Default and behavior |
| --- | --- |
| `--database PATH` | Required before the subcommand, except for `credentials`. Use the same SQLite file for collection, reporting, and maintenance. Pass the path as `database` to the WSGI factory. |
| `--retention-days DAYS` | Defaults to 365 days (one year), accepts 32–365. Place before the subcommand and use the same value throughout, including the WSGI factory’s `retention_days`. |
| `serve --port PORT` | The local service binds to `127.0.0.1:8765` by default. Use `0` to pick a free port. Startup prints the upload endpoint, browser report URL, and local JSON path. |
| `TRAJVIZ_OBS_ADDRESS_KEY` | Enables network address measurement. Supply 32 random bytes as 64 hexadecimal digits in the collector environment, or pass the same value as `address_key=` to the WSGI factory. Keep the key out of reports, logs, and backups of the database. |
| `serve --credentials PATH` and `--require-auth` | The absolute path overrides `TRAJVIZ_OBS_INGEST_CREDENTIALS`. A configured registry authenticates uploads to `/v1/events`. `--require-auth` makes startup fail if no registry is configured. The browser report is available to local users. |
| `serve --requests-per-minute N` and `--global-requests-per-minute N` | Defaults to 60 per credential and 600 overall per UTC minute for authenticated uploads. Set `1 ≤ per credential ≤ global ≤ 60,000`. The factory accepts the same settings as `requests_per_minute` and `global_requests_per_minute`. |

Use `python -c 'import secrets; print(secrets.token_hex(32))'` to generate an address key and store it in your deployment secret store. Keep the same key across workers and restarts for one database. The collector rejects a different key for that database so overlapping reporting windows keep a consistent address count. Omit the key to pause address measurement. Use a fresh database to start a separate counting history with a new key.

Address counts use the WSGI server's `REMOTE_ADDR`. Behind HTTPS proxies, configure the trusted server or middleware to set it from a verified forwarding chain. Block direct access to that listener and strip incoming forwarding headers at the edge. The collector itself ignores forwarding headers. A directly connected local plugin normally appears as one loopback address. [Trusted proxy configuration](https://werkzeug.palletsprojects.com/en/stable/deployment/proxy_fix/)

Issue one credential per installation or shared server. Run registry commands as the WSGI service account. The registry stores token hashes and labels. `issue` prints the token once, so store it in the plugin host’s protected launch environment.

```sh
registry=/var/lib/trajviz-obs/credentials.json
python -m telemetry credentials --file "$registry" issue pilot-mac
```

Use `credentials --file "$registry" list` to see labels or `credentials --file "$registry" revoke pilot-mac` to disable a token.

For reports and maintenance, open another terminal in the repository, activate the environment above, and set the collector's database path:

```sh
usage_db="$HOME/.local/state/trajviz-obs/usage.sqlite3"
```

Use the actual database path if it differs from the local example. Run `python -m telemetry --database "$usage_db" status --days 7` for private aggregate delivery counters, including accepted requests, new events, rejections, throttling, and errors. The default window is seven UTC days. Schedule `python -m telemetry --database "$usage_db" prune` to remove expired rows and checkpoint SQLite. Keep the database and registry private. Use SQLite’s backup API for a live copy and apply retention to backups. [SQLite backup guide](https://sqlite.org/backup.html)

## Reports

To refresh the report without opening it in a browser, run the publisher as a separate process. It writes both files immediately, then refreshes them from the same snapshot on each interval. Each file replaces its previous copy atomically. Stop the publisher with Ctrl+C or SIGTERM.

```sh
python -m telemetry --database "$usage_db" publish \
  --html /tmp/trajviz-usage.html --interval-seconds 3600
```

- `publish` requires `--html PATH`. An output named `report.html` also writes `report.json` in the same directory. The default is `--days 30` with a refresh every 3,600 seconds.
- `report` defaults to 30 UTC days ending today. Set `--days N` or `--as-of YYYY-MM-DD` to change the window. Both the requested window and the rolling 30-day window must fit within retention. Use `--html PATH` to write HTML and a JSON copy, or `--json PATH` for JSON alone. Omit both to print JSON to stdout.
- Use **EN | 中** to switch between English and Simplified Chinese in the live report or a saved HTML report. [JSON schema](report.schema.json)

Report schema 4 includes Claude Code, OpenCode, and Pi, with separate installation counts and coding agent version filters. The viewer also opens previously exported schemas 1–3 without adding empty Pi rows.

| Question | How the report counts it |
| --- | --- |
| Are people trying the plugin? | First-seen opt-in installations in the selected UTC period. The earliest recorded event date determines first seen. |
| Do they come back? | Distinct active and returning installations in the trailing 7 and 30 UTC days. Active means at least one event, including activation. Returning means the collector first saw that installation before the window started and recorded activity in the window. |
| What do they use? | Tracked command, dashboard, and pane actions in the selected period, filterable by coding agent and host version. Older uploads appear under **Unknown**. An installation may use multiple versions during a period. |

The JSON export's `usage.features` includes success, error, and cancellation rates by feature and agent version. The denominator is the total of those three outcomes. Empty results, rejections, confirmation prompts, and browser observations count as tracked events and stay out of that denominator. Features without eligible outcomes have no rate. Browser interactions measure gestures. Native outcomes describe what the command reported. The top-level `features` rows retain the original denominator of success and error for older consumers.

Installation IDs are scoped to the local state, adapter, and collector. OpenCode Desktop and terminal share an installation when they use the same server state. Clearing state or expiring inactive IDs can affect first-seen counts. Automatic refreshes and polls are excluded.

The **IP counts** section shows distinct IPs and counts each pair of an IP and an installation. To collect these counts, set `TRAJVIZ_OBS_ADDRESS_KEY` on the collector and restart it. The collector measures IPs for new schema 2 uploads that arrive on the same UTC day as the event. Earlier, delayed, and legacy events still count toward installation activity. **Unavailable** means the collector has no IP data for that window. Open **Daily trends and breakdowns** for the daily chart and coding agent versions.

Events include a UTC day, adapter, surface, allowlisted feature, outcome, latency bucket, plugin version, host version when available, and random installation and event IDs. Prompts, transcripts, paths, command arguments, account identifiers, and result text stay on the host. The collector stores a keyed digest for each measured address within the configured retention period. Raw addresses and address digests are omitted from exports and the built-in request logs. The offline HTML sends no usage events.

## Downloads and installs

Run `python -m telemetry.downloads` for external counters. It reports GitHub release asset downloads and npm package downloads separately from opt-in usage. Defaults are `--repo rshu/TrajViz-Obs`, `--package trajviz-obs-opencode`, and `--period last-month`. The other npm windows are `last-day` and `last-week`.

Add `--github-traffic` with an authenticated `gh` session to include 14 days of repository clone totals. To include a live Claude directory install count, pass `--claude-directory-installs START END COUNT` using the inclusive UTC date range and count from the published listing’s live Usage tab. Unavailable counters are marked as such. Repository clone totals cover all adapters; Pi Git installs have no separate public installation counter. Downloads can include repeat fetches and CI, and opt-in installations cover only hosts that send events. [GitHub release assets](https://docs.github.com/en/rest/releases/assets) · [GitHub clone traffic](https://docs.github.com/en/rest/metrics/traffic#get-repository-clones) · [npm download counts](https://github.com/npm/registry/blob/main/docs/download-counts.md) · [Claude developer portal](https://claude.com/docs/connectors/building/after-publishing#track-published-plugin-usage)

## Test the client and service together

From the repository root, run the integration tests with the sender and collector in the same checkout.

```sh
uv sync --project bridge --locked --extra dev
uv run --project bridge --locked --extra dev python -m pytest telemetry -q
```

To test compatibility with a different client build, set `TRAJVIZ_OBS_CLIENT_CHECKOUT` to that checkout's root.

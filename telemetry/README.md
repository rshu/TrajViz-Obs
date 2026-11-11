# TrajViz-Obs telemetry service

Telemetry is off by default. When enabled, Claude Code and OpenCode queue allowlisted usage events locally and send them in the background during plugin activity. The collector validates and deduplicates uploads in SQLite. A separate publisher reads aggregate statistics and refreshes a self-contained HTML report on a schedule.

This guide uses the collector from `codex/telemetry-service` and plugins installed from `codex/usage-telemetry`. Run the collector commands from the service checkout with Python 3.11 or newer.

## Try it locally

1. Start the receiver in one terminal. With no credential registry configured, this loopback test accepts uploads without a token.

   ```sh
   python3 -m telemetry --database "$HOME/.local/state/trajviz-obs/usage.sqlite3" serve
   ```

2. In another terminal, set the plugin environment and launch Claude Code or the OpenCode server from that environment. Run `/obs help` or `/obs`, then `/obs telemetry` to check delivery.

   ```sh
   export TRAJVIZ_OBS_TELEMETRY=1
   export TRAJVIZ_OBS_TELEMETRY_URL=http://127.0.0.1:8765/v1/events
   unset TRAJVIZ_OBS_TELEMETRY_TOKEN
   ```

3. Export the aggregate snapshot and open the resulting file in a browser.

   ```sh
   usage_db="$HOME/.local/state/trajviz-obs/usage.sqlite3"
   python3 -m telemetry --database "$usage_db" report --html /tmp/trajviz-usage.html
   ```

## Plugin configuration

| Launch environment variable | Setting |
| --- | --- |
| `TRAJVIZ_OBS_TELEMETRY` | Set to `1` to opt in. Unset it and restart the host to turn telemetry off. |
| `TRAJVIZ_OBS_TELEMETRY_URL` | Full URL ending in `/v1/events`, with no user info, query, or fragment. There is no default. HTTPS is required except on HTTP loopback (`localhost`, `127.0.0.1`, or `::1`). |
| `TRAJVIZ_OBS_TELEMETRY_TOKEN` | Bearer token issued by the collector. Required for authenticated receivers. |
| `TRAJVIZ_OBS_TELEMETRY_STATE_DIR` | Optional absolute path for local queue state. Defaults to `~/.local/state/trajviz-obs/telemetry` on macOS and Linux, honoring `XDG_STATE_HOME`, or `%LOCALAPPDATA%\trajviz-obs\telemetry` on Windows when set. |

Set these in Claude Code’s launch environment or the OpenCode **server’s** launch environment for both terminal and Desktop clients. Claude Code ignores telemetry values in its settings files. On macOS, apps opened from Finder do not inherit terminal exports. Restart the host or server after changing the settings.

`/obs telemetry` reads local status, including the queue, last upload, and retry deadline, without sending an event. The queue holds up to 512 events per adapter and endpoint and retries on later activity for up to seven days. Temporary failures back off and respect `Retry-After` within the retry limit. Authentication failures preserve the queue for a replacement token. Upload failures do not block plugin commands.

## Collector configuration

For remote collection, load the following application with a production WSGI server behind HTTPS. Set `TRAJVIZ_OBS_INGEST_CREDENTIALS` to the absolute path of the registry or pass `credentials=` to the factory. Production always requires authentication. Forward `Authorization`, enforce a 16 KiB body limit with rate and read-time limits at the proxy, and keep credentials and IP addresses out of access logs.

```python
from telemetry.collector import create_production_app

application = create_production_app("/var/lib/trajviz-obs/usage.sqlite3")
```

| Setting | Default and behavior |
| --- | --- |
| `--database PATH` | Required before the subcommand, except for `credentials`. Use the same SQLite file for collection, reporting, and maintenance. Pass the path as `database` to the WSGI factory. |
| `--retention-days DAYS` | Defaults to 365 days (one year), accepts 32–365. Place before the subcommand and use the same value throughout, including the WSGI factory’s `retention_days`. |
| `serve --port PORT` | Development only. Binds to `127.0.0.1:8765` by default. Use `0` to pick a free port. |
| `serve --credentials PATH` and `--require-auth` | The absolute path overrides `TRAJVIZ_OBS_INGEST_CREDENTIALS`. A configured registry enables authentication. `--require-auth` makes startup fail if no registry is configured. |
| `serve --requests-per-minute N` and `--global-requests-per-minute N` | Defaults to 60 per credential and 600 overall per UTC minute for authenticated uploads. Set `1 ≤ per credential ≤ global ≤ 60,000`. The factory accepts the same settings as `requests_per_minute` and `global_requests_per_minute`. |

Issue one credential per installation or shared server. Run registry commands as the WSGI service account. The registry stores token hashes and labels. `issue` prints the token once, so store it in the plugin host’s protected launch environment.

```sh
registry=/var/lib/trajviz-obs/credentials.json
python3 -m telemetry credentials --file "$registry" issue pilot-mac
```

Use `credentials --file "$registry" list` to see labels or `credentials --file "$registry" revoke pilot-mac` to disable a token.

For the following commands, set `usage_db` to the collector’s database path. Run `python3 -m telemetry --database "$usage_db" status --days 7` for private aggregate delivery counters, including accepted requests, new events, rejections, throttling, and errors. The default window is seven UTC days. Schedule `python3 -m telemetry --database "$usage_db" prune` to remove expired rows and checkpoint SQLite. Keep the database and registry private. Use SQLite’s backup API for a live copy and apply retention to backups. [SQLite backup guide](https://sqlite.org/backup.html)

## Reports

For continuous operation, run the publisher as a separate process. It writes immediately and replaces the HTML atomically after each successful refresh. If a refresh fails, it keeps the previous report and retries at the next interval. Stop it with Ctrl+C or SIGTERM.

```sh
python3 -m telemetry --database "$usage_db" publish \
  --html /tmp/trajviz-usage.html --interval-seconds 3600
```

- `publish` requires `--html PATH`. It defaults to `--days 30` and a refresh every 3,600 seconds.
- `report` defaults to 30 UTC days ending today. Set `--days N` or `--as-of YYYY-MM-DD` to change the window. Both the requested window and the rolling 30-day window must fit within retention. Use either `--html PATH` or `--json PATH`, or omit both for JSON on stdout.
- Use **EN | 中** to switch between English and Simplified Chinese in the offline HTML report or [JSON viewer](viewer.html). The viewer accepts JSON snapshots up to 5 MiB and displays up to 100 plugin-version rows ranked by tracked actions. JSON retains all rows. A `null` metric has no eligible data and `0` is a measured zero. [JSON schema](report.schema.json)

Reports show observed opt-in installations, daily and rolling 30-day activity, repeat use, command and dashboard or pane interactions, success rates, and latency by adapter, surface, and version. Automatic refresh and polling are excluded. Installation IDs are random and scoped to the local state, adapter, and collector. A shared OpenCode server can represent multiple people. Clearing state or exceeding retention can affect first-seen counts.

Events include a UTC day, adapter, surface, allowlisted feature, outcome, latency bucket, plugin version, and random installation and event IDs. Prompts, transcripts, paths, command arguments, account identifiers, and result text stay on the host. The collector omits IP addresses and individual request logs. Exports omit installation and event IDs, and the offline HTML sends no usage events.

## Downloads and installs

Run `python3 -m telemetry.downloads` for external counters. It reports GitHub release asset downloads and npm package downloads separately from opt-in usage. Defaults are `--repo rshu/TrajViz-Obs`, `--package trajviz-obs-opencode`, and `--period last-month`. The other npm windows are `last-day` and `last-week`.

Add `--github-traffic` with an authenticated `gh` session to include 14 days of repository clone totals. To include a live Claude directory install count, pass `--claude-directory-installs START END COUNT` using the inclusive UTC date range and count from the published listing’s live Usage tab. Unavailable counters are marked as such. Downloads can include repeat fetches and CI, and opt-in installations cover only hosts that send events. [GitHub release assets](https://docs.github.com/en/rest/releases/assets) · [GitHub clone traffic](https://docs.github.com/en/rest/metrics/traffic#get-repository-clones) · [npm download counts](https://github.com/npm/registry/blob/main/docs/download-counts.md) · [Claude developer portal](https://claude.com/docs/connectors/building/after-publishing#track-published-plugin-usage)

## Test the client and service together

From the service checkout, point `TRAJVIZ_OBS_CLIENT_CHECKOUT` at the repository root of a separate checkout of `codex/usage-telemetry`.

```sh
uv sync --project bridge --locked --extra dev
TRAJVIZ_OBS_CLIENT_CHECKOUT=/absolute/path/to/TrajViz-Obs \
  uv run --project bridge --locked --extra dev python3 -m pytest telemetry -q
```

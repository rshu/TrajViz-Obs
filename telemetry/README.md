# Plugin usage telemetry

Claude Code and OpenCode can send opt-in usage events to a collector you operate. Set the following variables in the host’s launch environment. The plugin ships without a collector URL.

| Setting | Value |
| --- | --- |
| `TRAJVIZ_OBS_TELEMETRY` | `1` |
| `TRAJVIZ_OBS_TELEMETRY_URL` | An HTTPS endpoint with the path `/v1/events`. HTTP works on loopback for local testing. |
| `TRAJVIZ_OBS_TELEMETRY_TOKEN` | The token issued by an authenticated collector. The loopback receiver can run without one. |

Use Claude Code’s launch environment for the Claude plugin and the OpenCode **server’s** launch environment for terminal and Desktop clients. Claude Code ignores telemetry variables supplied through its settings files. On macOS, a Desktop app opened from Finder does not inherit variables set in a terminal. To turn reporting off, unset `TRAJVIZ_OBS_TELEMETRY` and restart the host or server.

## Try it locally

From the repository root, start the development receiver in one terminal. Use Python 3.11 or newer.

```sh
python3 -m telemetry --database "$HOME/.local/state/trajviz-obs/usage.sqlite3" serve
```

In another terminal, export the settings and start your installed host. Run `/obs help` or `/obs`, then `/obs telemetry` to see queued events, the last upload, and the retry deadline. The status command reads the on-disk queue without sending an event or contacting the collector.

```sh
export TRAJVIZ_OBS_TELEMETRY=1
export TRAJVIZ_OBS_TELEMETRY_URL=http://127.0.0.1:8765/v1/events
```

After the command, read the aggregate report. Use `--days` or `--as-of YYYY-MM-DD` to change its UTC reporting window.

```sh
usage_db="$HOME/.local/state/trajviz-obs/usage.sqlite3"
python3 -m telemetry --database "$usage_db" report --html /tmp/trajviz-usage.html
python3 -m telemetry --database "$usage_db" report --json /tmp/trajviz-usage.json
```

Open the exported HTML in a browser. It includes the snapshot and works offline. To inspect a JSON snapshot up to 5 MiB, open [viewer.html](viewer.html) locally and choose the JSON file. The viewer displays up to 100 plugin-version rows ranked by tracked actions. The JSON includes every row. Leave out `--html` and `--json` to print the JSON to stdout.

## Refresh the HTML report automatically

Run the publisher separately from the receiver. It reads a fresh SQLite snapshot immediately and replaces the HTML on each successful refresh. If a database or filesystem error interrupts a refresh, the previous HTML remains available and the publisher retries at the next interval.

```sh
python3 -m telemetry --database "$usage_db" publish \
  --html /tmp/trajviz-usage.html --interval-seconds 3600
```

Set `--days` to change the reporting window. Run the publisher under your service manager for continuous operation. Stop it with Ctrl+C or SIGTERM.

## Report format

SQLite is the collector’s event store. Reports read one consistent snapshot and export aggregate JSON with `format: "trajviz-obs-usage-report"` and `schema_version: 1`. All date windows use UTC and include both endpoints. Counts are integers, rates are fractions, and `null` means a statistic has no eligible data. A count of `0` is a measured zero. The report schema is versioned independently of the upload protocol. Use a new report version for incompatible field or enum changes. [JSON Schema](report.schema.json)

Keep the event database on the collector and share the aggregate JSON or HTML. Both exports omit installation and event IDs. Review small groups before sharing publicly. For a live database backup, use SQLite’s backup API so the copy includes committed changes in the WAL. [SQLite backup guide](https://sqlite.org/backup.html)

## What the report measures

| Area | Metrics |
| --- | --- |
| Adoption | Observed opt-in installations still in retention, first-seen installations, daily and rolling 30-day active installations, and installations that only activated during the period |
| Engagement | Invocations and UI interactions, engaged installations, repeat engagement across days, average activity per engaged installation, and exact-day return rates at day 7 and day 30 |
| Features | Counts for `/obs` actions, direct pane interactions, and live dashboard actions by adapter, surface, and plugin version. Automatic refresh, polling, and trajectory preload are excluded. |
| Reliability | Counts for success, error, empty results, cancellation, rejection, and confirmation when the host exposes them. Rates and latency percentiles use invocations recorded as success or error. Browser actions use `observed` to record the interaction itself. |

An installation is a random ID scoped to a local state directory, adapter, and collector. A person can have multiple IDs, and a shared OpenCode server can represent several people. The first received event makes an ID observable. Clearing local state or exceeding retention can affect first-seen counts. The report covers consenting hosts, so its totals cannot establish how many people installed or used the plugin overall.

The pane records opening, closing, switching tabs, and manual refresh where the host exposes those actions. OpenCode also records its explicit pane size controls. The live browser records rendering, tab changes, workflow step inspection, manual trajectory load, and comparison, export, analysis, and attribution button presses. Converge records rendering, tab changes, and comparison buttons. An export or analysis button press records intent. The exported offline HTML contains no usage instrumentation.

## Downloads and installs

`python3 -m telemetry.downloads` reports GitHub uploaded release asset downloads and npm package downloads as separate series. It reports unavailable when a channel has no counter. GitHub clone-based installs and local-path OpenCode installs have no public plugin download counter. npm downloads can include CI, updates, and repeat fetches. [GitHub release assets](https://docs.github.com/en/rest/releases/assets) · [npm download counts](https://github.com/npm/registry/blob/main/docs/download-counts.md)

Maintainers with an authenticated `gh` session can add the last 14 days of GitHub clone traffic with `python3 -m telemetry.downloads --github-traffic`. The counts cover the entire repository, including clones unrelated to either plugin. [GitHub repository traffic](https://docs.github.com/en/rest/metrics/traffic#get-repository-clones)

For a plugin published in Claude’s official directory, take the live install total and UTC date range from the developer portal’s **Usage** tab and pass `--claude-directory-installs START END COUNT`. The report preserves that publisher total separately. The portal shows sample figures while the tab is marked **Preview**. Claude’s own-marketplace installs and OpenCode’s local, Git, and npm installs have no publisher install count in this report. Successful launches appear in the opt-in adoption figures. [Claude developer portal](https://claude.com/docs/connectors/building/after-publishing#track-published-plugin-usage)

## Data and operation

Events contain random event and installation IDs, UTC day, adapter, surface, an allowlisted feature, an outcome, a latency bucket, and plugin version. The sender excludes prompts, transcripts, paths, command arguments, model names, result text, and account identifiers. The receiver does not store IP addresses or individual request logs. It exposes a write endpoint and reports through a local CLI.

The browser sends fixed actions to its dashboard server on the same origin. The server forwards the observations to the configured collector. Update the collector before enabling browser and pane telemetry on newer plugin builds so it recognizes the added actions.

The client keeps a bounded local queue and retries on later activity for up to seven days. Temporary failures use randomized backoff and respect the collector’s `Retry-After` within the client’s retry limit. The sender isolates permanently rejected events so later events can proceed. Authentication failures leave the queue intact for a replacement credential. Live dashboards save observations before attempting delivery. Collector failures do not block plugin commands.

The default queue location is `~/.local/state/trajviz-obs/telemetry` on macOS and Linux, honoring `XDG_STATE_HOME`, or `%LOCALAPPDATA%\trajviz-obs\telemetry` on Windows. `TRAJVIZ_OBS_TELEMETRY_STATE_DIR` can set an absolute directory. With the host stopped, remove this state to clear queued events and reset its installation IDs. Previously received events age out under the collector’s retention policy.

## Run an authenticated collector

Issue a separate credential for each participating installation or shared server. Run the commands on the collector host as the WSGI service account so it can read the registry after rotation. `issue` prints the token once. Store it in the plugin host’s protected launch environment as `TRAJVIZ_OBS_TELEMETRY_TOKEN`. The registry contains token hashes and management labels.

```sh
registry=/var/lib/trajviz-obs/credentials.json
python3 -m telemetry credentials --file "$registry" issue pilot-mac
python3 -m telemetry credentials --file "$registry" list
python3 -m telemetry credentials --file "$registry" revoke pilot-mac
```

For a remote deployment, set `TRAJVIZ_OBS_INGEST_CREDENTIALS` to the registry’s absolute path and serve `telemetry.collector.create_production_app("/var/lib/trajviz-obs/usage.sqlite3")` with a production WSGI server behind HTTPS. The production factory requires a registry. The default limits are 60 requests per credential and 600 total per UTC minute. Forward `Authorization` to the WSGI app and set edge rate and read-time limits and a 16 KiB body limit. Keep access logs free of authorization headers and IP addresses.

On the collector, read aggregate delivery counters with `python3 -m telemetry --database /var/lib/trajviz-obs/usage.sqlite3 status --days 7`. It shows accepted requests and new events, validation failures, unauthorized requests, throttled requests, and server errors. Use aggregate edge counters to monitor failures when the database itself is unavailable.

Keep the SQLite database and registry private. Run `python3 -m telemetry --database /var/lib/trajviz-obs/usage.sqlite3 prune` regularly. The default retention is 90 days. Apply it to backups and use the same setting for the receiver, status, and reports.

## Test the client and service together

From the service checkout, install the bridge’s development dependencies and point the tests at a checkout of the client branch.

```sh
uv sync --project bridge --locked --extra dev
TRAJVIZ_OBS_CLIENT_CHECKOUT=/absolute/path/to/TrajViz-Obs \
  uv run --project bridge --locked --extra dev python3 -m pytest telemetry -q
```

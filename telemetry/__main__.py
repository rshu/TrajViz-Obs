"""Run a loopback telemetry service or inspect its aggregates."""

from __future__ import annotations

import argparse
import re
import signal
import sqlite3
import sys
from datetime import date
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.types import WSGIApplication

from .collector import (
    DEFAULT_GLOBAL_REQUESTS_PER_MINUTE,
    DEFAULT_REQUESTS_PER_MINUTE,
    DEFAULT_RETENTION_DAYS,
    EVENTS_PATH,
    create_app,
    ingestion_status,
    init_database,
    prune,
    utc_day,
)
from .credentials import issue_credential, list_credentials, revoke_credential
from .export import export_report, render_html, report_json
from .publish import publish_reports
from .report import report

_LOCAL_HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", re.IGNORECASE)


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        """The development server does not write request metadata to its logs."""


def _with_local_report(ingest: WSGIApplication, database: Path, retention_days: int) -> WSGIApplication:
    def app(environ, start_response):
        if environ["PATH_INFO"] != "/":
            return ingest(environ, start_response)

        method = environ["REQUEST_METHOD"]
        headers = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY")]
        content_type = "text/plain; charset=utf-8"
        if not _LOCAL_HOST.fullmatch(environ.get("HTTP_HOST", "")):
            status, body = "403 Forbidden", b"Use a loopback address to view the usage report.\n"
        elif method not in {"GET", "HEAD"}:
            status, body = "405 Method Not Allowed", b"Use GET or HEAD to view the usage report.\n"
            headers.append(("Allow", "GET, HEAD"))
        else:
            try:
                snapshot = report(database, today=utc_day(), retention_days=retention_days)
                body = render_html(snapshot).encode("utf-8")
            except (OSError, sqlite3.Error, ValueError):
                status, body = "503 Service Unavailable", b"The usage report is temporarily unavailable.\n"
            else:
                status = "200 OK"
                content_type = "text/html; charset=utf-8"
        headers.extend([("Content-Type", content_type), ("Content-Length", str(len(body)))])
        start_response(status, headers)
        return [] if method == "HEAD" else [body]

    return app


def _day(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a UTC date in YYYY-MM-DD format.") from None
    if value != day.isoformat():
        raise argparse.ArgumentTypeError("Expected a UTC date in YYYY-MM-DD format.")
    return day


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 0 and 65535.")
    return port


def _manage_credentials(args: argparse.Namespace) -> None:
    registry = args.file.expanduser().absolute()
    if args.credential_action == "issue":
        print(issue_credential(registry, args.name))
    elif args.credential_action == "revoke":
        if not revoke_credential(registry, args.name):
            raise ValueError("Credential name was not found.")
    else:
        for name in list_credentials(registry):
            print(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Path to the receiver's SQLite database.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help="Days of data retained, from 32 to 365 (default: %(default)s). Use the same value for all operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local event receiver and browser report on loopback.")
    serve.add_argument("--port", type=_port, default=8765)
    serve.add_argument("--credentials", type=Path, help="Path to the collector's credential registry.")
    serve.add_argument("--require-auth", action="store_true", help="Fail startup without a credential registry.")
    serve.add_argument("--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE,
                       help="Per-credential request limit (default: %(default)s).")
    serve.add_argument("--global-requests-per-minute", type=int, default=DEFAULT_GLOBAL_REQUESTS_PER_MINUTE,
                       help="Total authenticated request limit (default: %(default)s).")
    summary = commands.add_parser("report", help="Read aggregate usage statistics as JSON or offline HTML.")
    summary.add_argument("--days", type=int, default=30, help="Number of days in the report, inclusive of its end date.")
    summary.add_argument("--as-of", type=_day, help="Report end date in UTC. Defaults to today.")
    output = summary.add_mutually_exclusive_group()
    output.add_argument("--json", type=Path, metavar="PATH", help="Write a portable JSON report to a file.")
    output.add_argument("--html", type=Path, metavar="PATH", help="Write a self-contained offline HTML report.")
    publish = commands.add_parser("publish", help="Refresh an offline HTML report periodically.")
    publish.add_argument("--html", type=Path, required=True, metavar="PATH", help="HTML report destination.")
    publish.add_argument("--days", type=int, default=30, help="Number of UTC days to include.")
    publish.add_argument("--interval-seconds", type=int, default=3600,
                         help="Time between refresh attempts (default: %(default)s).")
    status = commands.add_parser("status", help="Read private collector delivery counters as JSON.")
    status.add_argument("--days", type=int, default=7, help="Number of UTC days to include.")
    commands.add_parser("prune", help="Delete expired rows and checkpoint the SQLite journal.")
    credentials = commands.add_parser("credentials", help="Issue, list, or revoke ingestion credentials.")
    credentials.add_argument("--file", type=Path, required=True, help="Path to the collector's credential registry.")
    credential_actions = credentials.add_subparsers(dest="credential_action", required=True)
    issue = credential_actions.add_parser("issue", help="Create a credential and print its token once.")
    issue.add_argument("name", help="A non-personal label used to manage this credential.")
    revoke = credential_actions.add_parser("revoke", help="Revoke an issued credential.")
    revoke.add_argument("name", help="Label of the credential to revoke.")
    credential_actions.add_parser("list", help="List credential labels.")
    args = parser.parse_args(argv)

    try:
        if args.command == "credentials":
            _manage_credentials(args)
            return 0
        if args.database is None:
            parser.error(f"--database is required for {args.command}.")
        database = args.database.expanduser().resolve()
        today = utc_day()
        if args.command == "report":
            payload = report(database, today=args.as_of or today, days=args.days,
                             retention_days=args.retention_days, retained_on=today)
            if destination := args.html or args.json:
                written = export_report(payload, destination, database=database, as_html=args.html is not None)
                print(written)
            else:
                sys.stdout.write(report_json(payload))
        elif args.command == "publish":
            previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
            try:
                publish_reports(database, args.html, days=args.days, retention_days=args.retention_days,
                                interval_seconds=args.interval_seconds)
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGTERM, previous)
        elif args.command == "status":
            sys.stdout.write(report_json(ingestion_status(
                database, today=today, days=args.days, retention_days=args.retention_days,
            )))
        elif args.command == "prune":
            init_database(database)
            prune(database, today, args.retention_days)
        else:
            ingest = create_app(database, retention_days=args.retention_days, credentials=args.credentials,
                                require_auth=args.require_auth, requests_per_minute=args.requests_per_minute,
                                global_requests_per_minute=args.global_requests_per_minute)
            app = _with_local_report(ingest, database, args.retention_days)
            with make_server("127.0.0.1", args.port, app, handler_class=_QuietHandler) as server:
                address = f"http://127.0.0.1:{server.server_port}"
                print(f"{address}{EVENTS_PATH}", f"{address}/", sep="\n", file=sys.stderr, flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"telemetry: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

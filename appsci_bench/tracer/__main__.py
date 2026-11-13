"""Standalone entry point for the tracer server.

Run with:
    python -m appsci_bench.tracer --port 8080

This allows viewing historical runs without starting a benchmark -- both runs on this machine
(``--runs-dir``) and runs anyone mirrored to S3. ``/?trace_id=<run_id>`` resolves either: an id that
isn't local yet is pulled from the mirror first, so one URL loads any run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from shared.observability.s3_store import DEFAULT_BUCKET, DEFAULT_PREFIX, S3Config

from .server import TracerServer


async def main() -> None:
    """Run the tracer server standalone."""
    parser = argparse.ArgumentParser(
        description="BUILD Phase Tracer - Standalone server for viewing benchmark runs"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to run the server on (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1; every route is unauthenticated and serves any "
             "local run's eval report, so widen this deliberately)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="Directory containing run data (default: runs/)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--s3-bucket",
        default=None,
        help=f"S3 bucket holding mirrored runs (default: $APPSCI_S3_BUCKET or {DEFAULT_BUCKET}). "
             f"Lets /?trace_id=<id> resolve a run that isn't on this machine yet.",
    )
    parser.add_argument(
        "--s3-prefix",
        default=None,
        help=f"Key prefix within the bucket (default: $APPSCI_S3_PREFIX or {DEFAULT_PREFIX}).",
    )
    parser.add_argument(
        "--aws-profile",
        default=None,
        help="AWS config profile for S3 (default: $APPSCI_S3_PROFILE, else the standard boto3 "
             "credential chain). ./scripts/setup_s3_sso.sh sets up an auto-refreshing SSO profile "
             "so credentials survive rotation without restarting this server.",
    )
    # Named for what it does -- suppress *reads* of the mirror. `--no-s3` read like a global kill
    # switch, but it only stops this tracer from consulting the mirror; it has no effect on whether
    # runs get uploaded (that is the orchestrator's --no-upload-runs-to-s3). Kept as an alias.
    parser.add_argument(
        "--no-s3-reads", "--no-s3",
        dest="no_s3_reads",
        action="store_true",
        help="Never consult the S3 mirror: serve only runs already in --runs-dir, and don't resolve "
             "unknown ids. Read side only -- to stop runs being uploaded, use the orchestrator's "
             "--no-upload-runs-to-s3.",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    s3_config = (
        None
        if args.no_s3_reads
        else S3Config.from_env(
            bucket=args.s3_bucket, prefix=args.s3_prefix, profile=args.aws_profile
        )
    )

    # The runs directory is where local runs are read from AND where runs pulled from the mirror are
    # written, so in S3 mode an absent directory is normal (a fresh machine that only wants to view
    # someone else's run) -- create it instead of bailing out.
    if not args.runs_dir.exists():
        if s3_config is not None:
            # Creating it is deliberate (a fresh machine that only wants someone else's run), but say
            # so loudly: this branch also swallows a mis-typed --runs-dir, which used to be an error.
            args.runs_dir.mkdir(parents=True, exist_ok=True)
            print(f"NOTE: {args.runs_dir.absolute()} did not exist and was created.")
            print("      If you meant an existing runs directory, check --runs-dir for a typo; "
                  "pass --no-s3-reads to make a missing directory an error again.")
        else:
            print(f"Error: Runs directory does not exist: {args.runs_dir.absolute()}")
            print("\nTo view historical runs, specify the path to your runs directory:")
            print("  python -m appsci_bench.tracer --runs-dir ./runs")
            print("\nOr run a benchmark with --enable-observability first to create runs.")
            sys.exit(1)

    # Start server
    server = TracerServer(
        runs_dir=args.runs_dir,
        host=args.host,
        port=args.port,
        s3_config=s3_config,
    )

    url = await server.start()
    print(f"\nBUILD Phase Tracer running at: {url}")
    print(f"Watching for runs in: {args.runs_dir.absolute()}")
    if s3_config is not None:
        print(f"Shared runs mirrored at: {s3_config.uri()}")
        print(f"  Load any run by id: {url}/?trace_id=<run_id> (pulled from the mirror if needed)")
    print("\nPress Ctrl+C to stop\n")

    # Handle shutdown signals
    stop_event = asyncio.Event()

    def signal_handler():
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Wait for shutdown signal
    await stop_event.wait()

    print("\nShutting down...")
    await server.stop()
    print("Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())

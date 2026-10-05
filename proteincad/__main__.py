"""Command line entry point: python -m proteincad"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import from_env
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proteincad",
        description="Serve the proteinCAD viewer and its API.",
    )
    # Defaults are None so the environment can supply them; see config.py.
    parser.add_argument("--host", help="interface to bind (PROTEINCAD_HOST, default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port to listen on (PROTEINCAD_PORT, default 8080)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--web-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help="where structures and jobs are kept")
    parser.add_argument("--compute-url", help="GPU endpoint for design jobs (PROTEINCAD_COMPUTE_URL)")
    parser.add_argument("--compute-token", help="bearer token for it (PROTEINCAD_COMPUTE_TOKEN)")
    parser.add_argument("--ec2-instance",
                        help="an EC2 instance to start for design jobs and stop when they are "
                             "done, i-0abc... (PROTEINCAD_EC2_INSTANCE). See deploy/aws/")
    parser.add_argument("--ec2-region", help="the region it is in (PROTEINCAD_EC2_REGION)")
    parser.add_argument("--ec2-idle", type=float, metavar="MINUTES",
                        help="stop it after this long with no jobs; 0 never (PROTEINCAD_EC2_IDLE, "
                             "default 15)")
    parser.add_argument("-v", "--verbose", action="store_true", default=None, help="log every request")
    args = parser.parse_args(argv)

    config = from_env(
        host=args.host,
        port=args.port,
        web_dir=args.web_dir,
        data_dir=args.data_dir,
        verbose=args.verbose,
        compute_url=args.compute_url.rstrip("/") if args.compute_url else None,
        compute_token=args.compute_token,
        ec2_instance=args.ec2_instance,
        ec2_region=args.ec2_region,
        ec2_idle_minutes=args.ec2_idle,
    )
    serve(config=config, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

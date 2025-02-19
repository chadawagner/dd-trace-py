#!/usr/bin/env python
# -*- encoding: utf-8 -*-
import argparse
import logging
import os
import shutil
import sys
import typing  # noqa:F401

import ddtrace


if hasattr(shutil, "which"):
    _which = shutil.which
else:
    # Python 2 fallback
    from distutils import spawn

    _which = spawn.find_executable  # type: ignore[assignment]


def find_executable(
    p,  # type: str
):
    # type: (...) -> typing.Optional[str]
    if os.path.isfile(p):
        return p
    return _which(p)


# Do not use `ddtrace.internal.logger.get_logger` here
# DEV: It isn't really necessary to use `DDLogger` here so we want to
#        defer importing `ddtrace` until we actually need it.
#      As well, no actual rate limiting would apply here since we only
#        have a few logged lines
log = logging.getLogger(__name__)

USAGE = """
Execute the given Python command after configuring it to emit Datadog traces
and profiles.


Examples
ddtrace-run python app.py
ddtrace-run gunicorn myproject.wsgi
"""


def _add_bootstrap_to_pythonpath(bootstrap_dir):
    """
    Add our bootstrap directory to the head of $PYTHONPATH to ensure
    it is loaded before program code
    """
    python_path = os.environ.get("PYTHONPATH", "")

    if python_path:
        new_path = "%s%s%s" % (bootstrap_dir, os.path.pathsep, os.environ["PYTHONPATH"])
        os.environ["PYTHONPATH"] = new_path
    else:
        os.environ["PYTHONPATH"] = bootstrap_dir


def main():
    parser = argparse.ArgumentParser(
        description=USAGE,
        prog="ddtrace-run",
        usage="ddtrace-run <your usual python command>",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, type=str, help="Command string to execute.")
    parser.add_argument("-d", "--debug", help="enable debug mode (disabled by default)", action="store_true")
    parser.add_argument(
        "-i",
        "--info",
        help=(
            "print library info useful for debugging. Only reflects configurations made via environment "
            "variables, not those made in code."
        ),
        action="store_true",
    )
    parser.add_argument("-p", "--profiling", help="enable profiling (disabled by default)", action="store_true")
    parser.add_argument("-v", "--version", action="version", version="%(prog)s " + ddtrace.__version__)
    parser.add_argument("-nc", "--colorless", help="print output of command without color", action="store_true")
    args = parser.parse_args()

    if args.profiling:
        os.environ["DD_PROFILING_ENABLED"] = "true"

    if args.debug or ddtrace.config._debug_mode:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["DD_TRACE_DEBUG"] = "true"

    if args.info:
        # Inline imports for performance.
        from ddtrace.internal.debug import pretty_collect

        print(pretty_collect(ddtrace.tracer, color=not args.colorless))
        sys.exit(0)

    root_dir = os.path.dirname(ddtrace.__file__)
    log.debug("ddtrace root: %s", root_dir)

    bootstrap_dir = os.path.join(root_dir, "bootstrap")
    log.debug("ddtrace bootstrap: %s", bootstrap_dir)

    _add_bootstrap_to_pythonpath(bootstrap_dir)
    log.debug("PYTHONPATH: %s", os.environ["PYTHONPATH"])
    log.debug("sys.path: %s", sys.path)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Find the executable path
    executable = find_executable(args.command[0])
    if executable is None:
        print("ddtrace-run: failed to find executable '%s'.\n" % args.command[0])
        parser.print_usage()
        sys.exit(1)

    log.debug("program executable: %s", executable)

    if os.path.basename(executable) == "uwsgi":
        print(
            (
                "ddtrace-run has known compatibility issues with uWSGI where the "
                "tracer is not started properly in uWSGI workers which can cause "
                "broken behavior. It is recommended you remove ddtrace-run and "
                "update your uWSGI configuration following "
                "https://ddtrace.readthedocs.io/en/stable/advanced_usage.html#uwsgi."
            )
        )

    try:
        os.execl(executable, executable, *args.command[1:])
    except PermissionError:
        print("ddtrace-run: permission error while launching '%s'" % executable)
        print("Did you mean `ddtrace-run python %s`?" % executable)
        sys.exit(1)
    except Exception:
        print("ddtrace-run: error launching '%s'" % executable)
        raise

    sys.exit(0)

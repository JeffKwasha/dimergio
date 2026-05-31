from __future__ import annotations

import logging
import sys

from . import __version__
from .cli import build_parser, cmd_analyze, cmd_cleanup, cmd_status, cmd_undo, cmd_watch


def main() -> None:
    logging.basicConfig(
        format="[dimergio] %(levelname)s %(message)s",
        stream=sys.stderr,
        level=logging.INFO,
    )
    logger = logging.getLogger("dimergio")
    logger.info("dimergio v%s", __version__)

    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "watch":
            cmd_watch(args)
        case "analyze":
            cmd_analyze(args)
        case "status":
            cmd_status(args)
        case "cleanup":
            cmd_cleanup(args)
        case "undo":
            cmd_undo(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()

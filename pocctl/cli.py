"""Command-line dispatch for ./poc (DESIGN.md §12; README.md lists the commands)."""

from __future__ import annotations

import re
import shutil
import sys

from .core import Fatal, acquire_lock, cancel_background, configure_environment, die, error, prepare_lifecycle

UP_USAGE = "./poc up [--images-only | --resume <bake-id> | --gha [--resume [<bake-id>] [--idp-run <run-id>]]]"
USAGE = (f"usage: {UP_USAGE}|"
         "down [--keep-images]|status|deploy-idp [<image-id> | --gha [<run-id>]]|"
         "prune-idp-images|set-password <user>|watch-bake <run-id>|logs [service] [since]|task <task-id>|run <run-id>")


def _lifecycle() -> None:
    acquire_lock()
    prepare_lifecycle()


def _dispatch(argv: list[str]) -> int:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    if command == "up":
        gha = args[:1] == ["--gha"]
        options = args[1:] if gha else args
        idp_run = None
        if gha and options[:1] == ["--resume"] and len(options) >= 3 and options[-2] == "--idp-run":
            idp_run, options = options[-1], options[:-2]
            if not re.fullmatch(r"[1-9][0-9]*", idp_run):
                die(f"usage: {UP_USAGE}")
        if not (not options or not gha and options == ["--images-only"] or gha and options == ["--resume"]
                or (len(options) == 2 and options[0] == "--resume" and re.fullmatch(r"[1-9][0-9]*", options[1]))):
            die(f"usage: {UP_USAGE}")
        from .lifecycle import cmd_images_only, cmd_up

        _lifecycle()
        if args == ["--images-only"]:
            cmd_images_only()
        else:
            cmd_up(options[1] if len(options) == 2 else None, gha=gha, resume_gha=gha and bool(options), idp_run=idp_run)
    elif command == "down":
        if args not in ([], ["--keep-images"]):
            die("usage: ./poc down [--keep-images]")
        from .lifecycle import cmd_down

        _lifecycle()
        cmd_down(args == ["--keep-images"])
    elif command in ("deploy-idp", "prune-idp-images"):
        from .idp import cmd_deploy_idp, cmd_prune_idp_images

        _lifecycle()
        (cmd_deploy_idp if command == "deploy-idp" else cmd_prune_idp_images)(args)
    elif command == "status":
        from .ops import cmd_status

        cmd_status()
    elif command == "watch-bake":
        if shutil.which("gh") is None:
            die("missing tool: gh")
        if len(args) != 1 or not re.fullmatch(r"[1-9][0-9]*", args[0]):
            die("usage: ./poc watch-bake <run-id>")
        from .images import watch_bake

        return watch_bake(args[0])
    elif command == "set-password":
        from .ops import cmd_set_password

        cmd_set_password(args)
    elif command in ("logs", "task", "run"):
        from .ops import cmd_logs, cmd_run, cmd_task

        return {"logs": cmd_logs, "task": cmd_task, "run": cmd_run}[command](args)
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    configure_environment()
    try:
        return _dispatch(argv)
    except Fatal as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        error("interrupted")
        return 130
    finally:
        cancel_background()

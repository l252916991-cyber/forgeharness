"""Verify, back up, or restore the local commerce reference application's state."""

import argparse
import json
from pathlib import Path

from forgeharness.applications.commerce.operations import (
    create_backup,
    manifest_summary,
    restore_backup,
    verify_backup,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create a backup while the service is stopped")
    backup.add_argument("state", type=Path)
    backup.add_argument("destination", type=Path)
    verify = commands.add_parser("verify", help="verify backup membership and file digests")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore", help="restore to the original absent state path")
    restore.add_argument("backup", type=Path)
    restore.add_argument("state", type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        result = create_backup(args.state, args.destination)
    elif args.command == "verify":
        result = verify_backup(args.backup)
    else:
        result = restore_backup(args.backup, args.state)
    print(json.dumps(manifest_summary(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

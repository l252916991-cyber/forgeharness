"""Invitation-only local workbench; business data/model remain synthetic fixtures.

Initialize an administrator offline, then invite users through the web interface.
Passwords are read by getpass, never accepted as process arguments or printed.
"""

import argparse
import getpass
from pathlib import Path

from forgeharness.applications.commerce.catalog import load_catalog
from forgeharness.applications.commerce.workbench import bootstrap_admin, create_workbench


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    bootstrap = actions.add_parser("bootstrap")
    bootstrap.add_argument("--workspace", required=True)
    bootstrap.add_argument("--email", required=True)
    serve = actions.add_parser("serve")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--catalog", type=Path)
    serve.add_argument(
        "--loopback-http",
        action="store_true",
        help="Explicitly allow non-Secure cookies for local HTTP; listener stays 127.0.0.1",
    )
    args = parser.parse_args()
    if args.action == "bootstrap":
        password = getpass.getpass("Administrator password: ")
        if getpass.getpass("Repeat password: ") != password:
            parser.error("passwords did not match")
        bootstrap_admin(
            args.state,
            workspace_id=args.workspace,
            email=args.email,
            password=password,
        )
        print("Administrator created; use invitations to add business users.")
        return
    import uvicorn

    catalog = load_catalog(args.catalog) if args.catalog else ()
    app = create_workbench(args.state, catalog=catalog, secure_cookies=not args.loopback_http)
    # Existing invitation acceptance routes put credentials in the path. Avoid
    # copying them into a generic HTTP access log; domain audit events are separate.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()

"""
One-time setup: store the Lakebase connection string in a Databricks secret scope.

Run this ONCE from a Databricks notebook before you wire the secret into
App -> Resources. Nothing is typed into a cell, echoed, or written to disk --
getpass keeps the connection string out of notebook output and command history.

From a notebook cell:

    %pip install databricks-sdk --quiet
    %run ./setup_secrets

or from a notebook terminal:

    python setup_secrets.py

Afterwards, in your Databricks App:
    Resources -> Add resource -> Secret
      Scope        = the scope printed below
      Key          = the key printed below
      Resource key = lakebase-url      <- must match app.yaml's `valueFrom`
"""

from __future__ import annotations

import sys
from getpass import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceAlreadyExists

DEFAULT_SCOPE = "support-app"
DEFAULT_KEY = "lakebase-url"

# This must match `valueFrom` in app.yaml. It is the name the app resource is
# given inside the app, not the name of the secret itself.
RESOURCE_KEY = "lakebase-url"


def ask(prompt: str, default: str) -> str:
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def validate_url(url: str) -> str:
    """Catch the mistakes that otherwise show up as a confusing app crash."""
    url = url.strip()

    if not url:
        sys.exit("No connection string entered. Nothing was saved.")

    if not url.startswith(("postgresql://", "postgres://")):
        sys.exit(
            "That does not look like a Postgres connection string.\n"
            "Expected: postgresql://<role>:<password>@<host>...:5432/databricks_postgres?sslmode=require"
        )

    credentials = url.split("://", 1)[1].split("@")[0]
    if "@" not in url or ":" not in credentials:
        sys.exit(
            "The connection string has no role:password. In the Lakebase instance, "
            "create a role with PASSWORD authentication (not OAuth) and copy its URL."
        )

    if "sslmode=" not in url:
        print("  note: no sslmode in the URL. The app adds sslmode=require automatically.")

    return url


def create_scope(w: WorkspaceClient, scope: str) -> None:
    try:
        w.secrets.create_scope(scope=scope)
        print(f"  created secret scope '{scope}'")
    except ResourceAlreadyExists:
        print(f"  secret scope '{scope}' already exists, reusing it")


def main() -> None:
    print("Lakebase secret setup\n" + "-" * 21)

    w = WorkspaceClient()
    print(f"  workspace: {w.config.host}")

    scope = ask("Secret scope", DEFAULT_SCOPE)
    key = ask("Secret key", DEFAULT_KEY)

    print(
        "\nPaste the Lakebase connection string (input is hidden).\n"
        "Find it under: Compute -> Database instances -> your instance -> Roles.\n"
    )
    url = validate_url(getpass("LAKEBASE_URL: "))

    print()
    create_scope(w, scope)
    w.secrets.put_secret(scope=scope, key=key, string_value=url)
    print(f"  stored secret '{key}' in scope '{scope}'")

    # Read back the metadata only. The value itself is never printed.
    stored = [s.key for s in w.secrets.list_secrets(scope=scope)]
    assert key in stored, f"'{key}' was not found in scope '{scope}' after writing"
    print(f"  verified: scope '{scope}' now holds {stored}")

    print(
        f"""
Done. Next steps in the Databricks Apps UI:

  1. Open your app -> Resources -> Add resource -> Secret
  2. Scope        = {scope}
     Key          = {key}
     Resource key = {RESOURCE_KEY}
  3. Grant the app's service principal READ on the scope if prompted.
  4. Deploy. app.yaml maps this to LAKEBASE_URL via `valueFrom: {RESOURCE_KEY}`.

Confirm with GET /healthz once the app is running.
"""
    )


if __name__ == "__main__":
    main()

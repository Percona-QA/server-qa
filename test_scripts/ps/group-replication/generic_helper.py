"""Small, dependency-free string-escaping helpers shared across the suite.

These keep dynamic values (database names, credentials, identifiers) from breaking — or
being injectable into — the SQL, mysqlsh JS, and connection strings the helpers build.
"""

import json


def js_str(value: str) -> str:
    """Encode a Python string as a JavaScript string literal for mysqlsh --js scripts.

    JSON encoding yields a valid JS string literal with quotes, backslashes, and
    control/non-ASCII characters escaped, so values like a cluster name or a connection
    URI (which contains a password) can't break the script or inject.
    """
    return json.dumps(value)


def sql_str(value: str) -> str:
    """Quote a MySQL string literal, escaping backslashes and single quotes."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_ident(name: str) -> str:
    """Quote a MySQL identifier (e.g. schema/table), escaping embedded backticks."""
    return "`" + name.replace("`", "``") + "`"

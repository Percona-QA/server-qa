"""Small, dependency-free helpers shared across the suite.

The escaping helpers keep dynamic values (database names, credentials, identifiers) from
breaking — or being injectable into — the SQL, mysqlsh JS, and connection strings the
helpers build. companion_image() picks tool images that match the server version.
"""

import json
import re


def js_str(value: str) -> str:
    """Encode a Python string as a JavaScript string literal for mysqlsh --js scripts.

    JSON encoding yields a valid JS string literal with quotes, backslashes, and
    control/non-ASCII characters escaped, so values like a cluster name or a connection
    URI (which contains a password) can't break the script or inject.
    """
    return json.dumps(value)


def sql_str(value: str) -> str:
    """Quote a MySQL string literal safely regardless of sql_mode.

    Single quotes are doubled ('') — the ANSI-standard form that works whether or not
    NO_BACKSLASH_ESCAPES is set (a backslash escape like \\' would break/inject under
    NO_BACKSLASH_ESCAPES). Backslashes are still doubled so a value ending in one can't
    escape the closing quote under MySQL's default (backslash-enabled) mode.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_ident(name: str) -> str:
    """Quote a MySQL identifier (e.g. schema/table), escaping embedded backticks."""
    return "`" + name.replace("`", "``") + "`"


def companion_image(server_image: str, repo_name: str, parts: int, default: str) -> str:
    """Derive a tool image (router, xtrabackup) matching the server image's version.

    Router and XtraBackup both refuse to work against a server newer than themselves, so a
    fixed 8.4 default breaks as soon as SERVER_IMAGE points at e.g. 9.7. Map
    <repo>/percona-server:<X.Y.Z...> to <repo>/<repo_name>:<first `parts` version
    components>, keeping the registry/namespace (percona vs perconalab publish different
    tags) and dropping any build suffix, which differs between server and tool tags.
    Anything that doesn't fit that shape (other repos, digests, "latest") gets `default`.
    """
    repo, sep, tag = server_image.rpartition(":")
    if not sep or "/" in tag or "@" in server_image or not repo.endswith("percona-server"):
        return default
    m = re.match(r"\d+(?:\.\d+){1,2}", tag)
    if not m:
        return default
    version = ".".join(m.group().split(".")[:parts])
    return f"{repo[: -len('percona-server')]}{repo_name}:{version}"

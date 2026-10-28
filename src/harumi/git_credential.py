"""`harumi git-credential`: the password git asks for when it talks to Harumi's git server.

Git runs this as a credential helper (see `harumi.git.CREDENTIAL_HELPER`), so a project's `harumi` remote can be the
plain clone URL and the token never has to be written into `.git/config`, where `git remote -v`, IDEs and screen
shares can read it. It also means a plain `git push harumi` works outside the CLI.

The protocol is git's: key=value lines on stdin up to a blank line, and for `get` the same on stdout. Three rules
keep it safe, because a credential helper is handed to whatever git is talking to:

* It answers only for the host of a Harumi environment the user is logged in to, over https. A request for any other
  host, or for http, gets no answer at all, so the token is never sent anywhere else.
* It only ever *gets*. `store` and `erase` do nothing, so git cannot overwrite or delete the token we hold.
* Stdout carries the protocol lines and nothing else, and errors stay quiet: the helper has no banner, no update
  notice and no message, and when it has nothing to say it says nothing, so git fails with its own error instead.
"""

from __future__ import annotations

from typing import Iterable, Optional, TextIO
from urllib.parse import urlsplit

from harumi import config


def read_request(stream: TextIO) -> dict[str, str]:
    """git's `key=value` lines, up to a blank line or the end of input."""
    request: dict[str, str] = {}
    for line in stream:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            request[key] = value
    return request


def _host_of(git_url: str) -> str:
    return (urlsplit(git_url).netloc or "").rsplit("@", 1)[-1].lower()


def _known_environments() -> Iterable[str]:
    return config.ENVIRONMENTS.keys()


def credentials_for(request: dict[str, str]) -> Optional[tuple[str, str]]:
    """(username, token) for the host git asked about, or None when it is not one of ours or we hold no token."""
    if request.get("protocol") != "https":
        return None
    host = request.get("host", "").lower()
    if not host:
        return None
    for name in _known_environments():
        creds = config._read_json(config.credentials_path(name))
        token, username = creds.get("git_token"), creds.get("git_username")
        if not token or not username:
            continue
        git_url = creds.get("git_url") or config.ENVIRONMENTS[name].git_url
        if _host_of(git_url) == host:
            return username, token
    return None


def respond(action: str, stdin: TextIO, stdout: TextIO) -> None:
    """Handle one git credential-helper call. Never raises: a helper that fails loudly breaks a `git push` in a way
    the user cannot connect to us."""
    try:
        request = read_request(stdin)
        if action != "get":
            return
        found = credentials_for(request)
        if found is None:
            return
        username, token = found
        stdout.write(f"username={username}\npassword={token}\n")
        stdout.flush()
    except Exception:  # noqa: BLE001 - see the docstring
        return

"""`harumi files put` must finish what the API started.

Since harumi-api commit aae13d75 ("quarantine project file uploads"), `POST .../files/upload-url`
hands out a presigned PUT to an opaque `{project}/.quarantine/<uuid>` key. The object is not a
project file until `POST .../files/complete-upload` has virus-scanned it and promoted a clean copy.
The CLI used to PUT and stop, printing "Uploaded" for a file that never appeared.

An older server (production, until `dev` is promoted) returns the final key and has no
complete-upload route; for it the extra call must not be made.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

runner = CliRunner()
P = "proj-1"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway home: these tests must not read, or depend on, the real ~/.harumi."""
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    """The slice of harumi-api that `files put` talks to, in either generation."""

    def __init__(self, staged: bool = True, complete_status: int = 204, complete_detail: str = ""):
        self.staged = staged
        self.staged_key: str | None = None  # override the key a staging server hands out
        self.hide_uploads = False  # the listing never shows what was PUT
        # A healthy older server lists what was uploaded; a staging server lists it only after completion.
        self.listing_shows_uploads = not staged
        self.truncated = False
        self.uploaded: list[str] = []
        self.complete_status = complete_status
        self.complete_detail = complete_detail
        self.calls: list[str] = []
        self.completed: list[dict] = []
        self.timeouts: list[object] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.url.host == "s3.test":
            return httpx.Response(200)
        if path.endswith("/files") and request.method == "GET":
            shown = [] if self.hide_uploads or not self.listing_shows_uploads else [
                {"name": n, "key": f"{P}/{n}", "size": 8, "last_modified": "2026-10-20T12:00:00Z"} for n in self.uploaded
            ]
            return httpx.Response(200, json={"files": shown, "is_truncated": self.truncated})
        if path.endswith("/files/upload-url"):
            import posixpath

            dest = json.loads(request.content)["path"]
            self.uploaded.append(posixpath.normpath(dest.strip("/")))  # what the server stores
            # An older server builds the key from the normalised path (harumi-api's `_sanitized_key`).
            key = self.staged_key or (f"{P}/.quarantine/3f2a-uuid" if self.staged else f"{P}/{posixpath.normpath(dest.strip('/'))}")
            return httpx.Response(200, json={"url": f"https://s3.test/{key}", "key": key, "expires_in": 900})
        if path.endswith("/files/complete-upload"):
            self.completed.append(json.loads(request.content))
            self.timeouts.append(request.extensions.get("timeout"))
            if self.complete_status == 204:
                return httpx.Response(204)
            return httpx.Response(self.complete_status, json={"detail": self.complete_detail})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def api(monkeypatch):
    def make(**kw) -> Api:
        a = Api(**kw)
        monkeypatch.setattr(
            cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler))
        )
        return a

    return make


@pytest.fixture
def csv(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n")
    return f


def put(csv, *extra):
    return runner.invoke(cli.app, ["files", "put", str(csv), *extra, "--project", P])


def plain(text: str) -> str:
    return " ".join(text.split())


# -- a server that stages uploads ---------------------------------------------------------------------------


def test_a_staged_upload_is_completed_with_the_path_and_the_key_it_was_given(api, csv):
    a = api()

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0, result.output
    assert a.completed == [{"path": "inputs/data.csv", "upload_key": f"{P}/.quarantine/3f2a-uuid"}]


def test_the_order_is_ask_for_a_url_upload_then_complete(api, csv):
    a = api()

    put(csv)

    wanted = [c for c in a.calls if c.endswith("/files/upload-url") or c.endswith("/files/complete-upload") or ".quarantine" in c]
    assert [c.rsplit("/", 1)[-1] for c in wanted] == ["upload-url", "3f2a-uuid", "complete-upload"], a.calls


def test_uploaded_is_only_printed_after_the_server_has_accepted_the_file(api, csv, monkeypatch):
    """The success line must come *after* the completion call returns, not merely appear.
    Recorded in one list so the order is what is asserted, not the presence of either."""
    a = api()
    events: list[str] = []
    real = Client.complete_file_upload

    def completing(self, *args, **kwargs):
        events.append("complete: asked")
        real(self, *args, **kwargs)
        events.append("complete: accepted")

    monkeypatch.setattr(Client, "complete_file_upload", completing)
    real_print = cli.console.print

    def spy(*x, **k):
        text = str(x[0]) if x else ""
        events.append("printed: Uploaded" if "Uploaded" in text else "printed: Checking" if "Checking" in text else "printed: other")
        return real_print(*x, **k)

    monkeypatch.setattr(cli.console, "print", spy)

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0, result.output
    assert events.index("complete: accepted") < next(i for i, e in enumerate(events) if e.startswith("printed: Uploaded"))
    assert next(i for i, e in enumerate(events) if e.startswith("printed: Checking")) < events.index("complete: asked")
    assert a.completed  # and it really was asked


@pytest.mark.parametrize(
    "status, detail, said, next_step",
    [
        (422, "Uploaded file could not be verified", "could not be verified as safe", None),
        (413, "Uploaded file is too large to verify", "too large to be checked", None),
        (503, "Uploaded file could not be verified", "could not check this file right now", "Try again in a few minutes"),
        (404, "Upload not found", "did not arrive", "Try again"),
    ],
)
def test_a_refused_upload_says_so_and_never_claims_success(api, csv, status, detail, said, next_step):
    api(complete_status=status, complete_detail=detail)

    result = put(csv)

    out = plain(result.output)
    assert result.exit_code == 1
    assert said in out and "was not added" in out
    if next_step:  # a temporary failure says what to do; a refusal does not invite a retry of the same file
        assert next_step in out
    else:
        assert "Try again" not in out
    assert "Uploaded" not in out.replace("was not added", "")  # no success line


def test_an_unexpected_failure_is_not_dressed_up(api, csv):
    api(complete_status=500, complete_detail="boom")

    result = put(csv)

    assert result.exit_code == 1 and "HTTP 500" in result.output and "Uploaded" not in result.output


def test_the_check_is_given_time_to_finish(api, csv):
    """The server scans with ClamAV for up to two minutes before answering."""
    a = api()

    put(csv)

    assert a.timeouts and a.timeouts[0]["read"] >= 150


# -- a server that does not stage (production until dev is promoted) -------------------------------------


def test_an_older_server_gets_no_extra_call(api, csv):
    a = api(staged=False)

    result = put(csv)

    assert result.exit_code == 0 and "Uploaded" in result.output
    assert not any("complete-upload" in c for c in a.calls)


def test_only_a_key_in_this_projects_quarantine_counts_as_staged():
    """The strict cases: what is plainly not staging stays unstaged."""
    from harumi.client import upload_is_staged

    assert upload_is_staged("proj-1", "proj-1/.quarantine/abc")
    assert not upload_is_staged("proj-1", "proj-1/inputs/data.csv")
    assert not upload_is_staged("proj-1", "proj-2/.quarantine/abc")  # someone else's staging area
    assert not upload_is_staged("proj-1", "proj-1/.quarantine/")  # nothing after the prefix
    assert not upload_is_staged("proj-1", "proj-1/data/.quarantine/abc")  # a user folder with that name
    assert upload_is_staged("proj-1", "proj-1/.quarantine/a/b")  # a nested name still counts: see the drift tests below


# -- what does not change -----------------------------------------------------------------------------------


def test_the_size_cap_is_still_checked_before_anything_is_sent(api, csv, monkeypatch):
    a = api()
    monkeypatch.setattr("harumi.cli.check_project_sync_cap", lambda *x, **k: type("V", (), {"reason": "file-count", "would_be": 501, "limit": 500})())

    result = put(csv)

    assert result.exit_code == 1 and "Remove some files first" in plain(result.output)
    assert not any("upload-url" in c for c in a.calls)


# -- the skill reference tells an agent about the check ------------------------------------------------------


def test_the_skill_reference_documents_files_and_the_completion_step():
    import pathlib

    ref = (pathlib.Path(cli.__file__).resolve().parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    section = ref[ref.index("## `files`") : ref.index("## `dashboard`")]

    for needle in ("complete-upload", "500 files", "Checking the file", "was not added", "503", "413", "422", ".part"):
        assert needle in section, f"the files section does not mention {needle!r}"
    for sub in ("ls", "put", "get", "rm"):
        assert f"harumi files {sub}" in section


# -- a server whose staging key we do not recognise ---------------------------------------------------------
#
# Review of #152: detection mirrors today's key shape. If the server changed it, the check would
# silently say "not staged", skip complete-upload, and print "Uploaded" for a file that never appears,
# which is the bug that change fixed. The key alone cannot settle it (an old server's final key is the
# path after posixpath.normpath, so "key == project/dest" is false for "./x.csv"), so the outcome is checked.


def test_a_key_that_mentions_the_quarantine_is_treated_as_staged_even_if_the_shape_drifted():
    from harumi.client import upload_is_staged

    assert upload_is_staged("proj-1", "proj-1/.quarantine/abc")
    assert upload_is_staged("proj-1", "proj-1/.quarantine/2026/abc")  # a nested name
    assert upload_is_staged("proj-1", "staging/proj-1/.quarantine/abc")  # a different prefix
    assert upload_is_staged("proj-1", ".quarantine/abc")  # no project in front


def test_a_user_folder_that_happens_to_be_called_quarantine_is_not_mistaken_for_staging():
    """An old server returns the user's own path as the key. `data/.quarantine/x.csv` is a legitimate name."""
    from harumi.client import upload_is_staged

    assert not upload_is_staged("proj-1", "proj-1/data/.quarantine/x.csv")


def test_the_cli_completes_an_upload_whose_staging_key_changed_shape(api, csv):
    a = api()
    a.staged_key = "staging/proj-1/.quarantine/3f2a-uuid"  # not the shape the CLI was written against

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0, result.output
    assert a.completed and a.completed[0]["upload_key"] == "staging/proj-1/.quarantine/3f2a-uuid"


def test_an_unstaged_upload_that_does_not_show_up_is_not_reported_as_uploaded(api, csv):
    """The server gave a key we took for final, but the file is not in the project: say so, do not claim success."""
    a = api(staged=False)
    a.hide_uploads = True  # the listing never shows what was PUT

    result = put(csv, "inputs/data.csv")

    out = plain(result.output)
    assert result.exit_code == 1
    assert "does not appear" in out and "harumi files ls" in out
    assert "Uploaded" not in out.replace("does not appear", "")


def test_an_unstaged_upload_that_shows_up_is_reported_as_uploaded(api, csv):
    a = api(staged=False)
    a.listing_shows_uploads = True

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0 and "Uploaded" in result.output


def test_the_check_is_skipped_when_the_listing_is_truncated(api, csv):
    """A partial listing cannot prove a file is missing, so it must not turn a good upload into an error."""
    a = api(staged=False)
    a.hide_uploads = True
    a.truncated = True

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0 and "Uploaded" in result.output


def test_a_normalised_path_is_matched_the_way_the_server_stores_it(api, csv):
    """The server stores `./inputs//data.csv` as `inputs/data.csv`; the check must look for that."""
    a = api(staged=False)
    a.listing_shows_uploads = True

    result = put(csv, "./inputs//data.csv")

    assert result.exit_code == 0 and "Uploaded" in result.output


# -- the answer never came ------------------------------------------------------------------------------------
#
# The scan can take up to two minutes. If the connection drops or the call times out, the server may
# still finish and promote the file, so "it was not added" would be a claim the CLI cannot make.


@pytest.fixture
def silent_server(monkeypatch):
    """The completion call never gets an answer (a dropped connection or a timeout)."""

    def make() -> Api:
        a = Api()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files/complete-upload"):
                raise httpx.ReadTimeout("timed out waiting for the scan", request=request)
            return a.handler(request)

        monkeypatch.setattr(
            cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
        )
        return a

    return make


def test_no_answer_to_the_check_says_the_outcome_is_unknown_not_that_it_failed(silent_server, csv):
    silent_server()

    result = put(csv, "inputs/data.csv")

    out = plain(result.output)
    assert result.exit_code == 1
    assert "may or may not" in out and "harumi files ls" in out
    assert "was not added" not in out  # that would be a claim we cannot make
    assert "Uploaded" not in out


def test_an_unknown_outcome_does_not_blame_the_platform_being_down(silent_server, csv):
    silent_server()

    out = plain(put(csv).output)

    assert "HTTP 0" not in out and "status" not in out.lower().replace("harumi files ls", "")


def test_a_real_refusal_still_says_it_was_not_added(api, csv):
    """The unknown-outcome wording must not leak into the cases where the server did answer."""
    api(complete_status=422, complete_detail="Uploaded file could not be verified")

    assert "was not added" in plain(put(csv).output)


def test_a_listing_that_fails_does_not_turn_a_good_upload_into_an_error(monkeypatch, csv):
    """The check is a safeguard. If it cannot run, the upload it was guarding still stands.
    `put` lists the project once before uploading (the cap check); only the second listing, the
    one after the upload, fails here."""
    a = Api(staged=False)
    listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files") and request.method == "GET":
            listings.append(1)
            if len(listings) > 1:
                return httpx.Response(503, json={"detail": "listing unavailable"})
        return a.handler(request)

    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )

    result = put(csv, "inputs/data.csv")

    assert len(listings) == 2, "the post-upload check did not run, so this proves nothing"
    assert result.exit_code == 0 and "Uploaded" in result.output


def test_the_skill_reference_documents_the_unknown_outcome_and_the_landing_check():
    import pathlib

    ref = (pathlib.Path(cli.__file__).resolve().parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    section = " ".join(ref[ref.index("## `files`") : ref.index("## `dashboard`")].split())

    for needle in ("may or may not have been added", "does not appear in the project", "truncated listing", "directly after the project id", "settles it first", "read-after-write"):
        assert needle in section, f"the files section does not mention {needle!r}"


# -- review: the path the user asked for settles it ------------------------------------------------------------


def test_a_file_the_user_named_with_a_quarantine_folder_is_a_plain_upload_on_an_older_server(api, csv):
    """`put x .quarantine/x.csv` on a server that does not stage: the key it returns IS the path we asked
    for. Treating the shape as staging would call a route that server lacks and report "did not arrive"
    for a file that uploaded fine."""
    a = api(staged=False)
    a.listing_shows_uploads = True

    result = put(csv, ".quarantine/x.csv")

    assert result.exit_code == 0 and "Uploaded" in result.output, result.output
    assert not any("complete-upload" in c for c in a.calls)


@pytest.mark.parametrize("dest", [".quarantine/x.csv", "./.quarantine//x.csv", "/.quarantine/x.csv", "data/.quarantine/x.csv"])
def test_the_requested_path_wins_however_it_is_spelled(api, csv, dest):
    a = api(staged=False)
    a.listing_shows_uploads = True

    assert put(csv, dest).exit_code == 0
    assert not any("complete-upload" in c for c in a.calls)


def test_a_server_that_really_stages_is_still_completed_when_the_key_is_not_the_requested_path(api, csv):
    a = api()  # staged: the key is .quarantine/<uuid>, not the path we asked for

    assert put(csv, ".quarantine/x.csv").exit_code == 0
    assert a.completed and a.completed[0]["path"] == ".quarantine/x.csv"


def test_the_decision_function_takes_the_requested_path():
    from harumi.client import upload_is_staged

    assert not upload_is_staged("proj-1", "proj-1/.quarantine/x.csv", requested=".quarantine/x.csv")
    assert not upload_is_staged("proj-1", "proj-1/.quarantine/x.csv", requested="./.quarantine//x.csv")  # normalised
    assert upload_is_staged("proj-1", "proj-1/.quarantine/3f2a", requested=".quarantine/x.csv")  # differs: look at the shape
    assert upload_is_staged("proj-1", "proj-1/.quarantine/x.csv")  # no requested path given: the shape alone, as before


# -- the landing check must never turn a good upload into a traceback ----------------------------------------


@pytest.mark.parametrize("body", ["<html>gateway hiccup</html>", '{"files": "not a list"}', '{"files": [{"nope": 1}]}'])
def test_a_listing_that_is_not_what_we_expect_does_not_fail_a_good_upload(monkeypatch, csv, body):
    a = Api(staged=False)
    listings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files") and request.method == "GET":
            listings.append(1)
            if len(listings) > 1:
                return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        return a.handler(request)

    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )

    result = put(csv, "inputs/data.csv")

    assert len(listings) == 2, "the post-upload check did not run"
    assert result.exit_code == 0 and "Uploaded" in result.output, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)

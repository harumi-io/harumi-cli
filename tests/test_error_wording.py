"""When things go wrong: say what failed, whose problem it is, and what to do. Never a traceback.

A failed request reaches the user as one of: no answer at all (refused, DNS, timeout, TLS, cut off),
an answer that is not Harumi's (a proxy's web page, a captive portal), an answer the CLI cannot read,
or an error status. Each gets its own words. Driven end to end through the real client, with a fake
transport that raises the exceptions httpx really raises.
"""

from __future__ import annotations

import errno

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

try:  # click 8.1 mixes stderr into the output unless told not to; 8.2 removed the switch
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

REQ = httpx.Request("GET", "https://api.harumi.io/api/users/profile")
POST = httpx.Request("POST", "https://api.harumi.io/api/users/organizations")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG", "HARUMI_STATUS_URL", "HARUMI_DEBUG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 100)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


@pytest.fixture
def reply(monkeypatch):
    """Make every request meet `box['r']`: an exception to raise, or a response to return."""
    box: dict = {}

    def handler(request):
        r = box["r"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler)))

    def go(response, *cmd):
        box["r"] = response
        return runner.invoke(cli.app, list(cmd) or ["whoami"])

    return go


def said(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


# -- no answer at all ---------------------------------------------------------------------------------------

REFUSED = httpx.ConnectError("[Errno 61] Connection refused", request=REQ)
DNS = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=REQ)
TLS = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate", request=REQ)
CONNECT_TIMEOUT = httpx.ConnectTimeout("timed out", request=REQ)
READ_TIMEOUT = httpx.ReadTimeout("The read operation timed out", request=REQ)
DROPPED = httpx.ReadError("[Errno 54] Connection reset by peer", request=REQ)


@pytest.mark.parametrize(
    "error, words",
    [
        (REFUSED, "refused"),
        (DNS, "find Harumi's address"),
        (CONNECT_TIMEOUT, "didn't answer in time"),
        (READ_TIMEOUT, "didn't answer in time"),
        (TLS, "certificate"),
        (DROPPED, "cut off"),
    ],
)
def test_each_kind_of_network_failure_has_its_own_words_and_no_http_zero(reply, error, words):
    result = reply(error)

    out = said(result)
    assert result.exit_code == 1 and words in out
    assert "HTTP 0" not in out and "Errno" not in out and "[SSL" not in out  # the raw text told a person nothing


def test_an_unrecognised_connection_failure_still_shows_what_it_said(reply):
    out = said(reply(httpx.ConnectError("something new and strange", request=REQ)))

    assert "something new and strange" in out and "HTTP 0" not in out


@pytest.mark.parametrize("error", [REFUSED, CONNECT_TIMEOUT, READ_TIMEOUT, DROPPED])
def test_when_the_platform_could_be_down_the_status_page_is_offered(reply, error):
    out = said(reply(error))

    assert "https://status.harumi.io" in out and "harumi status" in out


@pytest.mark.parametrize("error", [DNS, TLS])
def test_when_it_is_your_network_the_status_page_is_not_blamed(reply, error):
    assert "status.harumi.io" not in said(reply(error))


def test_a_name_that_will_not_resolve_points_at_the_connection(reply):
    assert "internet connection" in said(reply(DNS))


def test_a_certificate_failure_says_how_to_trust_a_company_proxy(reply):
    out = said(reply(TLS))

    assert "SSL_CERT_FILE" in out and "company" in out.lower()


@pytest.mark.parametrize("error", [REFUSED, DNS, CONNECT_TIMEOUT])
def test_staging_users_are_asked_about_the_vpn(reply, monkeypatch, error):
    monkeypatch.setenv("HARUMI_ENV", "staging")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")

    assert "VPN" in said(reply(error))


@pytest.mark.parametrize("error", [REFUSED, DNS, TLS, CONNECT_TIMEOUT, READ_TIMEOUT, DROPPED])
def test_production_users_never_hear_about_the_vpn_or_an_environment(reply, error):
    out = said(reply(error)).lower()

    assert "vpn" not in out and "production" not in out and "staging" not in out


# -- a request that changes something and then loses the connection ------------------------------------------


def test_a_write_that_timed_out_after_sending_says_it_may_have_gone_through(reply):
    out = said(reply(httpx.ReadTimeout("timed out", request=POST), "org", "create", "New Co"))

    assert "may have" in out and "before you try again" in out.lower()


def test_a_dropped_write_says_so_too(reply):
    assert "may have" in said(reply(httpx.ReadError("reset", request=POST), "org", "create", "New Co"))


def test_a_write_that_never_connected_cannot_have_gone_through_so_it_does_not_say_so(reply):
    out = said(reply(httpx.ConnectTimeout("timed out", request=POST), "org", "create", "New Co"))
    assert "may have" not in out
    assert "may have" not in said(reply(httpx.ConnectError("[Errno 61] Connection refused", request=POST), "org", "create", "New Co"))


def test_a_read_that_timed_out_does_not_worry_about_duplicates(reply):
    assert "may have" not in said(reply(READ_TIMEOUT))


# -- an answer that is not Harumi's --------------------------------------------------------------------------

BAD_GATEWAY = "<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center><hr>nginx/1.25.3</body></html>"
FIREWALL = "<html><head><title>Access Denied</title></head><body>Blocked by corporate proxy policy 4412</body></html>"


def html(code, body):
    return httpx.Response(code, text=body, headers={"content-type": "text/html"})


@pytest.mark.parametrize("code, body", [(502, BAD_GATEWAY), (503, BAD_GATEWAY), (504, BAD_GATEWAY), (403, FIREWALL), (407, FIREWALL)])
def test_a_web_page_is_described_not_pasted(reply, code, body):
    out = said(reply(html(code, body)))

    assert "<" not in out and "nginx" not in out and "corporate proxy" not in out
    assert "web page" in out and str(code) in out


def test_a_gateway_page_still_offers_the_status_page_but_a_firewall_page_does_not(reply):
    assert "status.harumi.io" in said(reply(html(502, BAD_GATEWAY)))
    assert "status.harumi.io" not in said(reply(html(403, FIREWALL)))


def test_a_plain_text_error_is_kept_but_never_runs_on(reply):
    out = said(reply(httpx.Response(500, text="x" * 5000, headers={"content-type": "text/plain"})))

    assert "x" * 100 in out and "x" * 400 not in out and out.count("…") >= 1


def test_server_text_cannot_send_the_terminal_an_escape_sequence(reply):
    result = reply(httpx.Response(400, json={"detail": "bad \x1b[2J input \u202e done"}))

    assert "\x1b" not in result.stderr and "\u202e" not in result.stderr and "bad" in result.stderr


# -- an answer that cannot be read ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, words",
    [
        (html(200, "<html><body>Please log in to the guest WiFi</body></html>"), "web page"),
        (httpx.Response(200, content=b""), "isn't in the form"),
        (httpx.Response(200, text="not json at all", headers={"content-type": "text/plain"}), "isn't in the form"),
    ],
)
def test_a_reply_that_is_not_json_is_a_message_not_a_traceback(reply, response, words):
    result = reply(response)

    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    out = said(result)
    assert words in out and "JSONDecodeError" not in out and "guest WiFi" not in out
    assert "upgrade" in out.lower()


def test_a_reply_of_the_wrong_shape_says_to_upgrade_and_names_the_command(reply):
    out = said(reply(httpx.Response(200, json={"unexpected": True}), "projects", "list"))

    assert "upgrade" in out.lower()
    assert any(command in out for command in ("pip install --upgrade harumi", "pipx upgrade harumi", "uv tool upgrade harumi"))


def test_a_reply_of_the_wrong_shape_names_the_field_but_never_echoes_the_value(reply):
    bad = {"projects": [{"id": {"x": "hunter2-SECRET"}, "name": "n"}], "total_count": 1}

    result = reply(httpx.Response(200, json=bad), "projects", "list")

    out = said(result)
    assert result.exit_code == 1 and "hunter2" not in out and "id" in out


# -- status codes ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header, expected", [("37", "37 seconds"), ("120", "2 minutes"), ("1", "1 second"), ("3600", "1 hour")])
def test_a_rate_limit_says_how_long_to_wait(reply, header, expected):
    out = said(reply(httpx.Response(429, json={"detail": "Rate limit exceeded"}, headers={"Retry-After": header})))

    assert "Rate limit exceeded" in out and expected in out


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "soon"}, {"Retry-After": "-5"}])
def test_a_rate_limit_without_a_usable_wait_just_says_so(reply, headers):
    out = said(reply(httpx.Response(429, json={"detail": "Rate limit exceeded"}, headers=headers)))

    assert "Rate limit exceeded" in out and "second" not in out and "minute" not in out


def test_other_client_errors_are_unchanged(reply):
    assert "HTTP 404: Not Found" in said(reply(httpx.Response(404, json={"detail": "Not Found"})))


def test_a_failed_s3_transfer_is_not_called_http_zero():
    from harumi.errors import ApiError

    assert "HTTP 0" not in cli._format_api_error(ApiError(0, "Upload failed: S3 timed out"))


# -- a bug in the CLI itself ----------------------------------------------------------------------------------


def run_main(monkeypatch, capsys, raising, env=None):
    # A throwaway command on the real app (main() runs `cli.app`), removed again so it cannot leak into
    # tests that walk the command tree.
    before = list(cli.app.registered_commands)

    @cli.app.command("boom-test")
    def boom():
        raise raising

    try:
        return _run_main(monkeypatch, capsys, env)
    finally:
        cli.app.registered_commands[:] = before


def _run_main(monkeypatch, capsys, env):
    monkeypatch.setattr("sys.argv", ["harumi", "boom-test"])
    monkeypatch.setenv("HARUMI_NO_UPDATE_CHECK", "1")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr()


def test_an_unexpected_crash_is_short_names_the_kind_and_where_to_report_it(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, KeyError("projects"))

    text = " ".join(out.err.split())
    assert code == 1 and "Something unexpected went wrong" in text and "This is a bug" in text and "KeyError" in text and "github.com/harumi-io/harumi-cli/issues" in text
    assert "Traceback" not in text and "boom" not in text and "HARUMI_DEBUG" in text


def test_an_unexpected_crash_does_not_print_the_exception_text_which_may_hold_a_token(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, RuntimeError("push to https://user:glpat-SECRET@git.example/x.git failed"))

    assert code == 1 and "glpat-SECRET" not in out.err and "glpat-SECRET" not in out.out


def test_debug_mode_gives_the_full_traceback_for_a_bug_report(monkeypatch, capsys):
    with pytest.raises(KeyError):
        run_main(monkeypatch, capsys, KeyError("projects"), env={"HARUMI_DEBUG": "1"})


def test_the_app_turns_off_locals_in_crash_tracebacks_explicitly():
    """typer 0.16 to 0.21 print a table of every local in the failing frame, and these hold the tokens.

    Newer typer hides them by default, so a run on a new typer proves nothing: the setting has to be
    in the source, passed by name, whatever the installed version would do on its own.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(cli.__file__).read_text())
    # The root app is built by `_typer(...)` (a thin wrapper over `typer.Typer`); either spelling counts.
    names = {"Typer", "_typer"}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in names]
    top = [c for c in calls if any(k.arg == "invoke_without_command" for k in c.keywords)]  # the root app
    assert len(top) == 1
    flag = next((k.value for k in top[0].keywords if k.arg == "pretty_exceptions_show_locals"), None)
    assert isinstance(flag, ast.Constant) and flag.value is False


def test_a_gateway_page_is_not_said_to_have_been_returned_by_harumi_api(reply):
    out = said(reply(html(502, BAD_GATEWAY)))

    assert "harumi-api returned" not in out and out.count("502") == 1  # the status once, not twice
    assert "status.harumi.io" in out


def test_a_firewall_page_is_not_said_to_have_been_returned_by_harumi_api_either(reply):
    out = said(reply(html(403, FIREWALL)))

    assert "harumi-api returned" not in out and "web page" in out


def test_a_validation_error_from_something_that_is_not_an_api_model_is_not_blamed_on_the_server():
    import pydantic

    class LocalSetting(pydantic.BaseModel):
        port: int

    @cli._handle_errors
    def read_local_config():
        LocalSetting.model_validate({"port": "not a number"})

    with pytest.raises(pydantic.ValidationError):  # left to surface as what it is
        read_local_config()


def test_a_closed_pipe_is_quiet_not_a_bug_report(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, BrokenPipeError())

    # Nothing at all on stderr: the reader left on purpose (`| head`), so there is nothing to report.
    # (BrokenPipeError is an OSError, so without its own branch it would be printed as a disk problem.)
    assert code == 1 and out.err == ""


def test_a_disk_problem_is_reported_as_the_machine_not_as_a_cli_bug(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, OSError(errno.ENOSPC, "No space left on device", "/home/ana/.harumi/credentials.json"))

    err = " ".join(out.err.split())
    assert code == 1 and "No space left on device" in err and ".harumi/credentials.json" in err
    assert "bug" not in err.lower() and "Something unexpected" not in err


def test_commands_a_test_adds_do_not_outlive_it(monkeypatch, capsys):
    names = {c.name for c in cli.app.registered_commands}
    run_main(monkeypatch, capsys, KeyError("x"))

    assert {c.name for c in cli.app.registered_commands} == names


@pytest.mark.parametrize("error", [BrokenPipeError(), OSError(errno.ENOSPC, "No space left on device", "/x"), OSError(errno.ECONNRESET, "Connection reset by peer")])
def test_debug_mode_gives_the_traceback_for_an_oserror_too(monkeypatch, capsys, error):
    with pytest.raises(OSError):
        run_main(monkeypatch, capsys, error, env={"HARUMI_DEBUG": "1"})


@pytest.mark.parametrize("error", [OSError(errno.ECONNRESET, "Connection reset by peer"), OSError(0, "ssl handshake")])
def test_a_network_oserror_is_not_passed_off_as_a_disk_problem(monkeypatch, capsys, error):
    code, out = run_main(monkeypatch, capsys, error)

    err = " ".join(out.err.split())
    # The class is named, whatever the platform makes of the errno: OSError(104, ...) is a plain OSError
    # on macOS but a ConnectionResetError on Linux.
    assert code == 1 and "Something unexpected went wrong" in err and type(error).__name__ in err
    assert "reset by peer" not in err  # the text of an unexpected error is not printed


@pytest.mark.parametrize("number", [errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS, errno.ENOSPC])
def test_disk_and_permission_errors_are_reported_as_the_machine(monkeypatch, capsys, number):
    code, out = run_main(monkeypatch, capsys, OSError(number, "the OS's words", "/home/ana/.harumi/x"))

    err = " ".join(out.err.split())
    assert code == 1 and "the OS's words" in err and "Something unexpected" not in err

"""files and share: readable tables, a real answer to a delete, and no UUID typing at a terminal.

Driven against a fake API with the real payload shapes. The tables are rendered at 80 columns, the
width of a default terminal, because an id that is cut there cannot be pasted into the next command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
P = "proj-1"
L1, L2, L3 = (f"{i}{i}{i}{i}{i}{i}{i}{i}-aaaa-4bbb-8ccc-00000000000{i}" for i in (1, 2, 3))


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
    monkeypatch.setattr(cli.console, "_width", 80)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def link(id_, token, label=None, enabled=True, password=False, **perms):
    return {
        "id": id_, "project_id": P, "token": token, "label": label, "enabled": enabled, "password_set": password,
        "app_enabled": perms.get("app", False), "chat_enabled": perms.get("chat", False),
        "run_history_enabled": perms.get("history", False), "run_control_enabled": perms.get("control", False),
    }


class Api:
    def __init__(self):
        self.files = {
            "inputs/demand.csv": (2048, ago(days=3)),
            "models/model.lp": (1_500_000, ago(hours=5)),
            "data/2026/q3.csv": (90_000_000, ago(days=40)),
        }
        self.links = [
            link(L1, "tok_open", "Client dashboard"),
            link(L2, "tok_wide", None, app=True, chat=True, history=True, control=True),
            link(L3, "tok_off", "Old meeting", enabled=False, password=True),
        ]
        self.truncated = False
        self.calls: list[str] = []
        self.deleted_files: list[str] = []
        self.deleted_links: list[str] = []
        self.rotated: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        self.calls.append(f"{m} {p}")
        if request.url.host == "s3.test":
            return httpx.Response(200, content=b"x")
        if p.endswith("/files") and m == "GET":
            return httpx.Response(200, json={"files": [
                {"name": n, "key": f"{P}/{n}", "size": s, "last_modified": t} for n, (s, t) in self.files.items()
            ], "is_truncated": self.truncated})
        if p.endswith("/files") and m == "DELETE":
            name = request.url.params["path"]
            self.deleted_files.append(name)
            return httpx.Response(200, json={"deleted": 1})  # harumi-api answers 1 for any path
        if p.endswith("/files/download-url"):
            name = request.url.params["path"]
            if name not in self.files:
                return httpx.Response(404, json={"detail": "File not found"})
            return httpx.Response(200, json={"url": f"https://s3.test/{name}", "expires_in": 900})
        if p.endswith("/share-links") and m == "GET":
            return httpx.Response(200, json={"links": self.links})
        if p.endswith("/rotate"):
            lid = p.split("/")[-2]
            if not any(x["id"] == lid for x in self.links):
                return httpx.Response(404, json={"detail": "Share link not found"})
            self.rotated.append(lid)
            return httpx.Response(200, json={**self.links[0], "token": "tok_rotated"})
        if "/share-links/" in p and m == "DELETE":
            lid = p.split("/")[-1]
            if not any(x["id"] == lid for x in self.links):
                return httpx.Response(404, json={"detail": "Share link not found"})
            self.deleted_links.append(lid)
            return httpx.Response(204)
        if "/share-links/" in p and m == "PATCH":
            lid = p.split("/")[-1]
            row = next((x for x in self.links if x["id"] == lid), None)
            return httpx.Response(200, json={**row, **json.loads(request.content)}) if row else httpx.Response(404, json={"detail": "Share link not found"})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p: p or P)
    return a


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def pick(monkeypatch, value):
    seen = {}

    def select(message, options, *, flag=None, default=None):
        seen.update(message=message, values=[o.value for o in options], labels=[o.label for o in options], flag=flag)
        return value

    monkeypatch.setattr(interactive, "select", select)
    return seen


def plain(text: str) -> str:
    return " ".join(text.split())


# -- files ls -----------------------------------------------------------------------------------------------


def test_files_ls_shows_sizes_and_ages_for_people(api):
    out = runner.invoke(cli.app, ["files", "ls"]).output

    assert "1.4 MB" in out and "2.0 KB" in out and "85.8 MB" in out
    assert "5 hours ago" in out and "3 days ago" in out
    assert "1500000" not in out and "+00:00" not in out  # neither raw bytes nor a raw timestamp


def test_files_ls_names_are_never_cut(api):
    api.files["a/very/deeply/nested/folder/structure/with/a/long/final_name_of_a_file.csv"] = (1, ago(days=1))

    out = runner.invoke(cli.app, ["files", "ls"]).output

    # Folded, not cut: the pieces of the name are all present, in order, with no ellipsis.
    glued = "".join(line.strip("│ ").split("│")[0].strip() for line in out.splitlines() if line.startswith("│"))
    assert "a/very/deeply/nested/folder/structure/with/a/long/final_name_of_a_file.csv" in glued
    assert "…" not in out


def test_files_ls_ends_with_the_total_against_the_limit_every_run_enforces(api):
    out = plain(runner.invoke(cli.app, ["files", "ls"]).output)

    assert "3 files, 87.3 MB" in out and "of 500 files and 500.0 MB every run can use" in out


def test_files_ls_warns_when_close_to_the_limit(api):
    api.files = {f"f{i}.csv": (1, ago(days=1)) for i in range(460)}

    assert "close to the limit" in plain(runner.invoke(cli.app, ["files", "ls"]).output)


def test_files_ls_does_not_warn_when_there_is_room(api):
    assert "close to the limit" not in plain(runner.invoke(cli.app, ["files", "ls"]).output)


def test_files_ls_says_so_when_the_listing_is_cut_and_does_not_present_a_partial_total_as_a_total(api):
    api.truncated = True

    out = plain(runner.invoke(cli.app, ["files", "ls"]).output)

    assert "more files than shown" in out and "at least" in out


def test_files_ls_in_an_empty_project_says_how_to_add_one(api):
    api.files = {}

    out = plain(runner.invoke(cli.app, ["files", "ls"]).output)

    assert "No files" in out and "harumi files put" in out


# -- files rm -----------------------------------------------------------------------------------------------


def test_removing_a_file_that_is_not_there_says_so_instead_of_reporting_a_deletion(api):
    result = runner.invoke(cli.app, ["files", "rm", "nope.csv", "--yes"])

    out = plain(result.output)
    assert result.exit_code == 1 and "No file called nope.csv" in out and "harumi files ls" in out
    assert api.deleted_files == []  # and nothing was sent


def test_removing_a_folder_name_points_at_the_files_inside(api):
    result = runner.invoke(cli.app, ["files", "rm", "data", "--yes"])

    out = plain(result.output)
    assert result.exit_code == 1 and "data is a folder" in out and "data/2026/q3.csv" in out
    assert api.deleted_files == []


def test_removing_a_file_names_it_and_its_size(api):
    result = runner.invoke(cli.app, ["files", "rm", "models/model.lp", "--yes"])

    out = plain(result.output)
    assert result.exit_code == 0 and api.deleted_files == ["models/model.lp"]
    assert "Deleted models/model.lp" in out and "1.4 MB" in out
    assert "Deleted 1 file(s)" not in out


def test_the_question_names_the_file_and_what_it_holds(api):
    result = runner.invoke(cli.app, ["files", "rm", "models/model.lp"], input="n\n")

    out = plain(result.output)
    assert "models/model.lp" in out and "1.4 MB" in out and "Aborted" in out
    assert api.deleted_files == []


def test_a_name_that_is_not_there_is_reported_before_asking_anything(api):
    result = runner.invoke(cli.app, ["files", "rm", "nope.csv"], input="y\n")

    assert "[y/N]" not in result.output and result.exit_code == 1


def test_a_truncated_listing_cannot_prove_a_file_is_missing_so_it_asks_the_server(api):
    api.truncated = True

    result = runner.invoke(cli.app, ["files", "rm", "late/in/the/alphabet.csv", "--yes"])

    assert result.exit_code == 0 and api.deleted_files == ["late/in/the/alphabet.csv"]


# -- files get ----------------------------------------------------------------------------------------------


def test_a_missing_download_points_at_files_ls(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["files", "get", "nope.csv"])

    out = plain(result.output)
    assert result.exit_code == 1 and "No file called nope.csv" in out and "harumi files ls" in out
    assert not (tmp_path / "nope.csv").exists() and not (tmp_path / "nope.csv.part").exists()


def test_a_folder_name_in_get_points_at_the_files_inside(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = plain(runner.invoke(cli.app, ["files", "get", "data"]).output)

    assert "data is a folder" in out and "data/2026/q3.csv" in out


# -- share list ---------------------------------------------------------------------------------------------


def first_column(out: str) -> list[str]:
    cells = []
    for line in out.splitlines():
        parts = [c.strip() for c in line.strip("│ ").split("│")]
        # A wrapped row repeats nothing in the first column; only a cell that starts a row is an id.
        if line.startswith("│") and parts[0] and parts[0].isalnum():
            cells.append(parts[0])
    return cells


def test_every_id_the_list_prints_works_as_an_argument(api):
    """The point of an id column is to copy from it. What is printed must be accepted as-is."""
    out = runner.invoke(cli.app, ["share", "list"]).output
    ids = first_column(out)

    assert len(ids) == 3 and "…" not in out
    for shown in ids:
        result = runner.invoke(cli.app, ["share", "get", shown])
        assert result.exit_code == 0, f"{shown!r} was printed by `share list` but `share get` refused it:\n{result.output}"
        assert shown in result.output


def test_the_shown_ids_are_all_different(api):
    """Eight characters must be enough to tell the links apart (they are the start of a UUID)."""
    shown = first_column(runner.invoke(cli.app, ["share", "list"]).output)

    assert len(set(shown)) == len(shown)


def test_two_links_sharing_a_start_are_not_guessed_between(api):
    api.links[1]["id"] = L1[:8] + "-bbbb-4ccc-8ddd-999999999999"

    result = runner.invoke(cli.app, ["share", "get", L1[:8]])

    assert result.exit_code == 1 and "more than one" in plain(result.output)


def test_share_list_says_who_can_do_what_in_words(api):
    out = plain(runner.invoke(cli.app, ["share", "list"]).output)

    assert "view only" in out  # the first link
    for word in ("app", "assistant", "history", "control"):
        assert word in out  # the wide-open link names every permission, app included
    assert "password" in out.lower()


def test_share_list_flags_a_public_link_with_no_password_that_can_do_more_than_view(api):
    out = plain(runner.invoke(cli.app, ["share", "list"]).output)

    assert "no password" in out and "anyone with the link" in out


def test_share_list_does_not_print_the_tokens(api):
    out = runner.invoke(cli.app, ["share", "list"]).output

    assert "tok_open" not in out and "tok_wide" not in out  # the URL is in `share get`


def test_share_list_says_which_are_off(api):
    assert "off" in plain(runner.invoke(cli.app, ["share", "list"]).output)


def test_share_list_with_none_says_how_to_make_one(api):
    api.links = []

    assert "harumi share add" in plain(runner.invoke(cli.app, ["share", "list"]).output)


# -- share get / update / rm / rotate: no UUID to type at a terminal -------------------------------------


@pytest.mark.parametrize("args", [["share", "get"], ["share", "rm"], ["share", "rotate"], ["share", "update", "--disable"]])
def test_without_an_id_a_terminal_user_picks_from_the_links(api, monkeypatch, args):
    seen = pick(monkeypatch, L1)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    runner.invoke(cli.app, args, input="y\n")

    assert seen["values"] == [L1, L2, L3]
    assert any("Client dashboard" in label or L1[:8] in label for label in seen["labels"])


@pytest.mark.parametrize("args", [["share", "get"], ["share", "rm"], ["share", "rotate"], ["share", "update", "--disable"]])
def test_without_an_id_or_a_terminal_it_lists_the_links_and_names_the_argument(api, no_terminal, args):
    result = runner.invoke(cli.app, args)

    out = plain(result.output)
    assert result.exit_code == 1 and "LINK_ID" in out and L1 in out
    assert api.deleted_links == [] and api.rotated == []


@pytest.mark.parametrize("command", ["rm", "rotate"])
def test_yes_without_an_id_refuses_instead_of_acting_on_a_pick(api, monkeypatch, command):
    """--yes removes the question, so a slip in a list would have nothing standing in its way."""
    pick(monkeypatch, L1)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    result = runner.invoke(cli.app, ["share", command, "--yes"])

    assert result.exit_code == 1 and "link id" in plain(result.output).lower()
    assert api.deleted_links == [] and api.rotated == []


def test_picking_a_link_to_remove_still_asks_for_confirmation(api, monkeypatch):
    pick(monkeypatch, L1)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    runner.invoke(cli.app, ["share", "rm"], input="n\n")

    assert api.deleted_links == []


def test_backing_out_of_the_picker_does_nothing(api, monkeypatch):
    pick(monkeypatch, None)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    result = runner.invoke(cli.app, ["share", "rm"])

    assert result.exit_code == 0 and api.deleted_links == []


def test_an_id_on_the_command_line_never_opens_a_picker(api, monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened a picker")))

    result = runner.invoke(cli.app, ["share", "get", L2])

    assert result.exit_code == 0 and L2 in result.output


def test_a_short_id_that_is_unambiguous_is_accepted(api):
    """What a person copies from a narrow terminal is often only the start of the id."""
    result = runner.invoke(cli.app, ["share", "get", L2[:8]])

    assert result.exit_code == 0 and L2 in result.output


def test_a_wrong_id_points_at_share_list(api):
    result = runner.invoke(cli.app, ["share", "get", "nope"])

    out = plain(result.output)
    assert result.exit_code == 1 and "No share link 'nope'" in out and "harumi share list" in out


def test_an_update_to_a_wrong_id_points_at_share_list_too(api):
    out = plain(runner.invoke(cli.app, ["share", "update", "nope", "--disable"]).output)

    assert "harumi share list" in out


# -- what a link looks like once you have it ----------------------------------------------------------------


def test_a_link_is_described_in_plain_words(api):
    out = plain(runner.invoke(cli.app, ["share", "get", L2]).output)

    assert "anyone with this link" in out.lower()
    assert "https://app.harumi.io/share/tok_wide" in out
    assert "no password" in out.lower()


def test_a_closed_link_says_the_url_does_not_work(api):
    out = plain(runner.invoke(cli.app, ["share", "get", L3]).output)

    assert "disabled" in out.lower() and "does not work" in out.lower()
    assert "password" in out.lower()


# -- a full id is used as given -----------------------------------------------------------------------------


UNKNOWN = "99999999-aaaa-4bbb-8ccc-000000000009"


@pytest.mark.parametrize(
    "args",
    [["share", "get", UNKNOWN], ["share", "update", UNKNOWN, "--disable"], ["share", "rm", UNKNOWN, "--yes"], ["share", "rotate", UNKNOWN, "--yes"]],
)
def test_a_full_but_unknown_id_goes_to_the_server_and_comes_back_with_a_pointer(api, args):
    api.links = []  # nothing to match; the full id must still be tried as given

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1 and "harumi share list" in plain(result.output)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)  # not a traceback


def test_share_get_lists_the_links_once_whatever_the_id_looks_like(api):
    runner.invoke(cli.app, ["share", "get", L2])
    runner.invoke(cli.app, ["share", "get", L2[:8]])

    assert api.calls.count(f"GET /api/projects/{P}/share-links") == 2  # one per command, not two


def test_a_full_id_costs_no_extra_request(api):
    runner.invoke(cli.app, ["share", "rm", L1, "--yes"])

    assert api.calls == [f"DELETE /api/projects/{P}/share-links/{L1}"]  # no listing first


# -- the warning is for the exposed case only: it must not cry wolf ----------------------------------------


def _careful(out: str) -> bool:
    return "Careful" in out or "anyone with the link can use it" in plain(out)


@pytest.mark.parametrize(
    "case, row",
    [
        ("view only, no password", link(L1, "t", "A")),
        ("wide open but protected by a password", link(L1, "t", "B", password=True, app=True, chat=True, history=True, control=True)),
        ("wide open but switched off", link(L1, "t", "C", enabled=False, app=True, chat=True, history=True, control=True)),
    ],
)
def test_a_link_that_is_not_exposed_gets_no_warning(api, case, row):
    api.links = [row]

    assert not _careful(runner.invoke(cli.app, ["share", "list"]).output), case
    assert not _careful(runner.invoke(cli.app, ["share", "get", L1]).output), case


@pytest.mark.parametrize("perm", ["app", "chat", "history", "control"])
def test_each_single_permission_without_a_password_is_enough_to_warn(api, perm):
    api.links = [link(L1, "t", "X", **{perm: True})]

    assert _careful(runner.invoke(cli.app, ["share", "list"]).output)
    assert "Careful" in runner.invoke(cli.app, ["share", "get", L1]).output


def test_the_token_never_appears_in_a_table_cell(api):
    """The tokens are the secret in the public URLs; the list shows only that they exist."""
    out = runner.invoke(cli.app, ["share", "list"]).output

    for row in api.links:
        assert row["token"] not in out


# -- the skill reference matches ----------------------------------------------------------------------------


def test_the_skill_reference_describes_the_share_ids_pickers_and_exposure_warning():
    import pathlib

    ref = (pathlib.Path(cli.__file__).resolve().parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    section = ref[ref.index("## `share`") : ref.index("## `datasources`")]

    for needle in ("the start of it", "first 8 characters", "never prompt", "LINK_ID", "refuse `--yes` without an id", "token is not printed", "no password", "hard delete"):
        assert needle in section, f"the share section does not mention {needle!r}"
    for usage in ("share get [LINK_ID]", "share update [LINK_ID]", "share rm [LINK_ID]", "share rotate [LINK_ID]"):
        assert usage in section
    assert "valid types" in ref[ref.index("## `dashboard`") : ref.index("## `share`")]


# -- a script that removes a file that may already be gone -------------------------------------------------


def test_removing_a_missing_file_with_missing_ok_succeeds_quietly(api):
    """`files rm x --yes` used to exit 0 for any name (harumi-api answers deleted: 1 for everything).
    Cleanup scripts depend on that; --missing-ok keeps them working without hiding typos by default."""
    result = runner.invoke(cli.app, ["files", "rm", "nope.csv", "--yes", "--missing-ok"])

    out = plain(result.output)
    assert result.exit_code == 0 and "was not there" in out and "nothing to delete" in out
    assert api.deleted_files == []


def test_missing_ok_does_not_excuse_a_folder_name(api):
    result = runner.invoke(cli.app, ["files", "rm", "data", "--yes", "--missing-ok"])

    assert result.exit_code == 1 and "is a folder" in plain(result.output)


def test_missing_ok_still_deletes_a_file_that_is_there(api):
    result = runner.invoke(cli.app, ["files", "rm", "models/model.lp", "--yes", "--missing-ok"])

    assert result.exit_code == 0 and api.deleted_files == ["models/model.lp"]


def test_a_missing_file_without_the_flag_names_the_flag_for_scripts(api):
    out = plain(runner.invoke(cli.app, ["files", "rm", "nope.csv", "--yes"]).output)

    assert "--missing-ok" in out


# -- the confirmation names what you are deleting ----------------------------------------------------------


def test_the_share_rm_question_shows_the_label_so_a_wrong_prefix_is_noticed(api):
    result = runner.invoke(cli.app, ["share", "rm", L1[:8]], input="n\n")

    out = plain(result.output)
    assert "Client dashboard" in out and L1 in out and "Aborted" in out
    assert api.deleted_links == []


def test_the_share_rm_question_handles_a_link_with_no_label(api):
    out = plain(runner.invoke(cli.app, ["share", "rm", L2], input="n\n").output)

    assert "untitled" in out and L2 in out


def test_a_label_with_brackets_is_shown_whole_in_the_question(api):
    api.links[0]["label"] = "Q3 [draft]"

    assert "Q3 [draft]" in plain(runner.invoke(cli.app, ["share", "rm", L1], input="n\n").output)


def test_the_skill_reference_records_the_files_rm_exit_code_change_for_scripts():
    """`files rm <missing> --yes` used to exit 0 for any name. A script relying on that needs to know.
    The files section itself arrives with the complete-upload fix; until then the note lives here."""
    import pathlib

    root = pathlib.Path(cli.__file__).resolve().parents[2]
    ref = (root / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")

    section = ref[ref.index("## `files rm` and scripts") : ref.index("## `share`")]
    assert "harumi files rm REMOTE_PATH [--yes] [--missing-ok]" in section
    assert "exit 0" in section and "exits 1" in section and "folder name" in section


# -- review: one listing for rm, and a label cannot drive the terminal ---------------------------------------


def test_share_rm_lists_the_links_once_even_for_a_short_id(api):
    runner.invoke(cli.app, ["share", "rm", L1[:8]], input="n\n")

    assert api.calls.count(f"GET /api/projects/{P}/share-links") == 1, api.calls


def test_share_rm_with_a_full_id_and_yes_still_makes_no_listing_request(api):
    runner.invoke(cli.app, ["share", "rm", L1, "--yes"])

    assert api.calls == [f"DELETE /api/projects/{P}/share-links/{L1}"]


def test_a_label_with_terminal_control_characters_is_not_echoed_raw(api):
    api.links[0]["label"] = "Q3\x1b[2J\x07 board"

    result = runner.invoke(cli.app, ["share", "rm", L1], input="n\n")

    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "Q3" in result.output and "board" in result.output


def test_a_label_with_control_characters_is_clean_in_the_listing_too(api):
    api.links[0]["label"] = "Q3\x1b[2J board"

    assert "\x1b[2J" not in runner.invoke(cli.app, ["share", "list"]).output
    assert "\x1b[2J" not in runner.invoke(cli.app, ["share", "get", L1]).output


def test_a_file_name_with_terminal_control_characters_is_not_echoed_raw_by_files_ls(api):
    api.files["inputs/evil\x1b[2J\x07.csv"] = (1, ago(days=1))

    out = runner.invoke(cli.app, ["files", "ls"]).output

    assert "\x1b" not in out and "\x07" not in out and "inputs/evil" in out

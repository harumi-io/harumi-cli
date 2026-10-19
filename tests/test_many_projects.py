"""Big accounts: every project is reachable, recent work comes first, and
nothing is dropped without saying so."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import MAX_PROJECT_PAGES, PROJECTS_PAGE_SIZE, Client
from harumi.models import Project

runner = CliRunner()
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


def _row(i: int, **extra) -> dict:
    return {
        "id": f"proj-{i:04d}",
        "name": f"Project {i:04d}",
        "notebook_ids": [],
        "created_at": (NOW - timedelta(days=1000 - i)).isoformat(),
        "updated_at": (NOW - timedelta(days=1000 - i)).isoformat(),
        **extra,
    }


class Server:
    """harumi-api's `GET /projects`: creation order, `limit`/`offset`, real `total_count`."""

    def __init__(self, n: int, *, honour_paging: bool = True, total_override: "int | None" = None):
        self.rows = [_row(i) for i in range(n, 0, -1)]  # newest created first, like the API
        self.honour_paging = honour_paging
        self.total_override = total_override
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if request.url.path.endswith("/projects"):
            if self.honour_paging:
                limit = int(params.get("limit", 100))
                offset = int(params.get("offset", 0))
                page = self.rows[offset : offset + limit]
            else:
                page = self.rows
            total = self.total_override if self.total_override is not None else len(self.rows)
            return httpx.Response(200, json={"projects": page, "total_count": total})
        return httpx.Response(404, json={"detail": "not routed"})

    def client(self) -> Client:
        return Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def server(monkeypatch):
    holder = {}

    def make(n, **kw):
        holder["s"] = Server(n, **kw)
        monkeypatch.setattr(cli, "_get_client", lambda *a, **k: holder["s"].client())
        return holder["s"]

    return make


# -- fetching ------------------------------------------------------------------------


def test_every_project_is_fetched_not_just_the_first_hundred(server):
    s = server(450)

    projects, total = s.client().list_projects_with_total()

    assert len(projects) == 450
    assert total == 450
    assert len({p.id for p in projects}) == 450
    assert [r["offset"] for r in s.requests] == ["0", "200", "400"]


def test_a_single_short_page_costs_one_request(server):
    s = server(7)

    projects, _ = s.client().list_projects_with_total()

    assert len(projects) == 7
    assert len(s.requests) == 1


def test_an_exact_page_boundary_does_not_loop_or_drop_the_last_project(server):
    s = server(PROJECTS_PAGE_SIZE * 2)

    projects, total = s.client().list_projects_with_total()

    assert len(projects) == total == PROJECTS_PAGE_SIZE * 2


def test_an_account_with_no_projects_is_fine(server):
    s = server(0)
    assert s.client().list_projects_with_total() == ([], 0)


def test_a_server_that_ignores_paging_does_not_loop_forever_or_duplicate(server):
    s = server(350, honour_paging=False)

    projects, total = s.client().list_projects_with_total()

    assert len(projects) == 350
    assert len({p.id for p in projects}) == 350
    assert len(s.requests) <= 2


def test_the_page_cap_stops_a_server_that_never_says_stop(server):
    # Claims millions of projects; hands out a full page every time, always new ones.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        base = int(request.url.params["offset"])
        rows = [_row(base + i) for i in range(PROJECTS_PAGE_SIZE)]
        return httpx.Response(200, json={"projects": rows, "total_count": 10_000_000})

    client = Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))

    projects, total = client.list_projects_with_total()

    assert calls["n"] == MAX_PROJECT_PAGES
    assert len(projects) == MAX_PROJECT_PAGES * PROJECTS_PAGE_SIZE
    assert total == 10_000_000  # the truth is kept so callers can say "showing N of M"


def test_list_projects_still_returns_a_plain_list(server):
    server(5)
    assert len(server(5).client().list_projects()) == 5


# -- order and age ---------------------------------------------------------------------


def _p(name, updated=None, created=None):
    return Project(id=name, name=name, updated_at=updated, created_at=created)


def test_most_recently_updated_comes_first_even_if_created_long_ago():
    old_but_active = _p("old-but-active", updated=NOW, created=NOW - timedelta(days=900))
    new_but_idle = _p("new-but-idle", updated=NOW - timedelta(days=30), created=NOW - timedelta(days=2))

    ordered = cli._recent_first([new_but_idle, old_but_active])

    assert [p.name for p in ordered] == ["old-but-active", "new-but-idle"]


def test_a_project_with_no_timestamps_sorts_last_not_first():
    ordered = cli._recent_first([_p("blank"), _p("dated", updated=NOW)])
    assert [p.name for p in ordered] == ["dated", "blank"]


def test_naive_and_aware_timestamps_can_be_sorted_together():
    ordered = cli._recent_first(
        [_p("naive", updated=datetime(2026, 1, 1)), _p("aware", updated=NOW)]
    )
    assert [p.name for p in ordered] == ["aware", "naive"]


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=45), "45 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=5), "5 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=12), "12 days ago"),
        (timedelta(days=45), "1 month ago"),
        (timedelta(days=200), "6 months ago"),
        (timedelta(days=400), "1 year ago"),
        (timedelta(days=1000), "2 years ago"),
        (timedelta(seconds=-30), "just now"),  # a clock slightly ahead of the server's
    ],
)
def test_age_reads_naturally(monkeypatch, delta, expected):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(cli, "datetime", Frozen)
    assert cli._ago(NOW - delta) == expected


def test_no_timestamp_means_no_age():
    assert cli._ago(None) == ""


# -- projects list -------------------------------------------------------------------------


def test_projects_list_shows_all_of_them_newest_activity_first(server):
    server(250)

    result = runner.invoke(cli.app, ["projects", "list"])

    assert result.exit_code == 0, result.output
    assert "proj-0250" in result.output and "proj-0001" in result.output
    assert result.output.count("proj-") == 250
    assert result.output.index("proj-0250") < result.output.index("proj-0001")
    assert "updated" in result.output
    assert "Showing" not in result.output  # nothing was cut, so nothing to apologise for


def test_projects_list_limit_says_how_many_were_left_out(server):
    server(250)

    result = runner.invoke(cli.app, ["projects", "list", "--limit", "10"])

    assert result.exit_code == 0, result.output
    assert result.output.count("proj-") == 10
    assert "Showing 10 of 250" in result.output
    assert "Drop --limit" in result.output


def test_projects_list_admits_it_when_the_server_has_more_than_could_be_fetched(server):
    server(30, total_override=500)

    result = runner.invoke(cli.app, ["projects", "list"])

    flat = " ".join(result.output.split())  # the terminal may wrap the line
    assert "Showing 30 of 500" in flat
    assert "could not be fetched" in flat


def test_the_workspace_column_appears_only_when_projects_span_workspaces(server, monkeypatch):
    s = server(0)
    s.rows = [_row(1, customer_id=None), _row(2, customer_id="org-a"), _row(3, customer_id="org-a")]
    monkeypatch.setattr(cli, "_workspace_names", lambda client: {"org-a": "Acme Corp"})

    mixed = runner.invoke(cli.app, ["projects", "list"])

    assert "workspace" in mixed.output
    assert "Acme Corp" in mixed.output and "Personal" in mixed.output

    s.rows = [_row(2, customer_id="org-a"), _row(3, customer_id="org-a")]
    one = runner.invoke(cli.app, ["projects", "list"])
    assert "workspace" not in one.output


# -- the picker ------------------------------------------------------------------------------


def test_the_picker_offers_every_project_most_recent_first(server, monkeypatch):
    server(450)
    seen = {}

    def select(message, options, *, flag=None, default=None):
        seen["options"] = options
        return None

    monkeypatch.setattr(interactive, "select", select)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    runner.invoke(cli.app, ["clone"])

    options = seen["options"]
    assert len(options) == 450
    assert options[0].value == "proj-0450"  # newest activity first
    assert "proj-0450" in options[0].hint
    assert " ago" in options[0].hint  # how recently it was touched


def test_a_project_beyond_the_old_first_hundred_can_now_be_chosen(server, monkeypatch):
    server(250)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "select", lambda msg, options, **k: "proj-0042")
    chosen = {}
    monkeypatch.setattr(
        cli, "_clone_and_bind", lambda project, repo, dest: chosen.setdefault("p", project)
    )

    class Repo:
        pass

    # Only what is under test: the id reaches the clone step.
    s = server(250)
    monkeypatch.setattr(s.client().__class__, "get_project_repo", lambda self, pid: Repo())
    monkeypatch.setattr(
        s.client().__class__, "get_project", lambda self, pid: Project(id=pid, name="Forty-two")
    )

    result = runner.invoke(cli.app, ["clone"])

    assert result.exit_code == 0, result.output
    assert chosen["p"] == "proj-0042"


# -- runs ------------------------------------------------------------------------------------


class RunsServer:
    def __init__(self, n):
        self.n = n

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            runs = [
                {"id": f"run-{i:04d}", "project_id": "p", "status": "succeeded", "source": "cli"}
                for i in range(self.n, 0, -1)
            ]
            return httpx.Response(200, json={"runs": runs})

        return Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))


@pytest.fixture
def runs(monkeypatch):
    def make(n):
        monkeypatch.setattr(cli, "_get_client", lambda *a, **k: RunsServer(n).client())

    return make


def test_runs_list_shows_the_latest_25_and_says_there_are_more(runs):
    runs(312)

    result = runner.invoke(cli.app, ["runs", "list", "--project", "p"])

    assert result.exit_code == 0, result.output
    assert result.output.count("run-") == cli.RUNS_SHOWN == 25
    assert "run-0312" in result.output and "run-0001" not in result.output
    assert "Showing the latest 25 of 312" in result.output
    assert "--all" in result.output


def test_runs_list_all_shows_every_run(runs):
    runs(312)

    result = runner.invoke(cli.app, ["runs", "list", "--project", "p", "--all"])

    assert result.output.count("run-") == 312
    assert "Showing" not in result.output


def test_runs_list_limit_is_respected(runs):
    runs(40)

    result = runner.invoke(cli.app, ["runs", "list", "--project", "p", "-n", "5"])

    assert result.output.count("run-") == 5
    assert "Showing the latest 5 of 40" in result.output


def test_a_short_runs_list_prints_no_footer(runs):
    runs(3)

    result = runner.invoke(cli.app, ["runs", "list", "--project", "p"])

    assert result.output.count("run-") == 3
    assert "Showing" not in result.output


def test_projects_list_orders_by_activity_not_by_the_servers_creation_order(server):
    """The server hands back creation order. A project created long ago but
    updated today has to jump the queue, in the printed table itself."""
    s = server(0)
    s.rows = [
        _row(3, updated_at=(NOW - timedelta(days=2)).isoformat()),   # created last, idle
        _row(2, updated_at=(NOW - timedelta(days=9)).isoformat()),
        _row(1, updated_at=NOW.isoformat()),                          # created first, active today
    ]

    result = runner.invoke(cli.app, ["projects", "list"])

    positions = [result.output.index(f"proj-000{i}") for i in (1, 3, 2)]
    assert positions == sorted(positions), "expected proj-0001, then proj-0003, then proj-0002"


def test_the_picker_orders_by_activity_too(server, monkeypatch):
    s = server(0)
    s.rows = [_row(2, updated_at=(NOW - timedelta(days=5)).isoformat()), _row(1, updated_at=NOW.isoformat())]
    seen = {}
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "select", lambda msg, options, **k: seen.setdefault("o", options) and None)

    runner.invoke(cli.app, ["clone"])

    assert [o.value for o in seen["o"]] == ["proj-0001", "proj-0002"]


# -- review findings -------------------------------------------------------------------------------------


def test_limit_and_all_together_are_refused_even_when_limit_equals_the_default(runs):
    """Two ways of saying how many must not quietly disagree. `--limit 25 --all` is the
    sneaky one: a check against the default value would let it through."""
    runs(60)

    for args in (["--limit", "5", "--all"], ["--limit", "25", "--all"], ["-n", "1", "--all"]):
        result = runner.invoke(cli.app, ["runs", "list", "--project", "p", *args])
        assert result.exit_code == 1, args
        assert "mutually exclusive" in result.output
        assert "run-" not in result.output


def test_runs_list_still_shows_25_by_default_and_all_with_all(runs):
    runs(60)
    assert runner.invoke(cli.app, ["runs", "list", "--project", "p"]).output.count("run-") == 25
    assert runner.invoke(cli.app, ["runs", "list", "--project", "p", "--all"]).output.count("run-") == 60


def test_workspace_names_come_from_the_api_the_projects_came_from(monkeypatch):
    """The bug: `--api-url` pointed the project list at one stack while the organization
    names were looked up on the default one. Two servers answer here; every request must
    go to the one that was chosen, and the names shown must be that one's."""
    chosen, default = [], []

    def make_handler(log, org_name):
        def handler(request: httpx.Request) -> httpx.Response:
            log.append(request.url.path)
            if request.url.path.endswith("/users/organizations"):
                return httpx.Response(200, json=[{"id": "org-a", "business_name": org_name}])
            rows = [_row(1, customer_id=None), _row(2, customer_id="org-a")]
            return httpx.Response(200, json={"projects": rows, "total_count": 2})

        return handler

    chosen_transport = httpx.MockTransport(make_handler(chosen, "ChosenCo"))
    default_transport = httpx.MockTransport(make_handler(default, "DefaultCo"))

    def get_client(api_url=None, git_url=None, org=None):
        # What the real `_get_client` does: --api-url selects the server.
        transport = chosen_transport if api_url == "http://localhost:8000/api" else default_transport
        return Client(api_url=api_url or "https://api.harumi.io/api", org_id=org, transport=transport)

    monkeypatch.setattr(cli, "_get_client", get_client)

    result = runner.invoke(cli.app, ["projects", "list", "--api-url", "http://localhost:8000/api"])

    assert result.exit_code == 0, result.output
    assert default == [], f"talked to the default stack: {default}"
    assert any(p.endswith("/users/organizations") for p in chosen), "never looked the organization up"
    assert "ChosenCo" in result.output and "DefaultCo" not in result.output


def test_a_clock_slightly_ahead_of_the_server_does_not_print_a_negative_age():
    from datetime import datetime, timedelta, timezone

    assert cli._ago(datetime.now(timezone.utc) + timedelta(seconds=30)) == "just now"
    assert cli._ago(datetime.now(timezone.utc) - timedelta(seconds=59)) == "just now"

"""What bare `harumi` offers: a short "start here" for whoever just typed it.

One list of actions drives both renderings, so they can't drift apart:

* in a terminal it becomes an arrow-key menu (`harumi.interactive.select`);
* anywhere else (pipes, CI, coding agents) it is printed as plain text.

Everything here is pure: the CLI gathers the `State`, this module decides what
to show. Running the chosen action is the CLI's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class State:
    logged_in: bool
    environment: str
    email: Optional[str] = None
    project_id: Optional[str] = None  # set when this folder is bound to a project


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    command: str  # what the same thing looks like typed by hand


# Keys the CLI knows how to run.
LOGIN = "login"
SIGNUP = "signup"
NEW = "new"
IMPORT = "import"
CLONE = "clone"
RUN = "run"
CHECK = "check"
RUNS = "runs"
HELP = "help"
QUIT = "quit"


def status_line(state: State) -> str:
    """One sentence on where the user stands. Rich markup."""
    if not state.logged_in:
        return (
            "You're not logged in yet. Run [bold]harumi login[/bold] to get started "
            "(first time here? [bold]harumi login --signup[/bold])."
        )
    # "Using", not "logged in as": all we know offline is which account is on
    # file. Whether its session still works is only found out by asking the server.
    who = state.email or "your account"
    where = "" if state.environment == "production" else f" on [bold]{state.environment}[/bold]"
    line = f"Using [bold]{who}[/bold]{where}."
    if state.project_id:
        line += f" This folder is project [bold]{state.project_id}[/bold]."
    return line


def actions_for(state: State) -> list[Action]:
    """The next steps that make sense for this user, best first."""
    if not state.logged_in:
        return [
            Action(LOGIN, "Log in", "harumi login"),
            Action(SIGNUP, "Create an account", "harumi login --signup"),
        ]
    if state.project_id:
        return [
            Action(RUN, "Run this project", "harumi run --watch"),
            Action(CHECK, "Check it is ready to run", "harumi check"),
            Action(RUNS, "See recent runs", "harumi runs list"),
        ]
    return [
        Action(NEW, "Start a new project", 'harumi new "My Project"'),
        Action(IMPORT, "Turn this folder into a project", "harumi import"),
        Action(CLONE, "Get a project that already exists", "harumi clone <PROJECT_ID>"),
    ]


def plain_text(state: State) -> list[str]:
    """The same content as the menu, for output that is not a terminal."""
    actions = actions_for(state)
    width = max(len(a.command) for a in actions)
    lines = [status_line(state), "", "Start here"]
    lines.extend(f"  {a.command.ljust(width)}  {a.label}" for a in actions)
    lines += ["", "All commands: harumi --help    One command: harumi <command> --help"]
    return lines

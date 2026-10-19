# AGENTS.md

Agent-facing conventions for this repo.

## Issues, PRs and the project board

All work is tracked on one org board, [Harumi's Dev Project](https://github.com/orgs/harumi-io/projects/4)
(`harumi-io`, project `4`), across every repo. Before you change code, look at it; while you work,
keep it true, so nobody collides with or duplicates anyone else. Needs `gh` with the `project` scope
(`gh auth status`). "Me" is the person running the session (`gh api user --jq .login`).

This section replaces any older "do not edit issues or PRs" rule.

### What you may do

Without asking:

- create issues in any harumi-io repo for the work you are about to do;
- edit, label, assign, comment on and link issues and PRs that **I authored or am assigned to**;
- put an item on the board and set the Status of an item I am working on.

Ask first before you edit, comment on, label, reassign, re-status or close an issue or PR that
belongs to someone else. Never delete an issue, PR, comment or board item.

### Before writing code

Skip this for read-only questions and reviews. Otherwise, check, then say in a line or two what you
found:

```bash
# Board: everything not Done (status, assignees, repo, number, title)
gh project item-list 4 --owner harumi-io --limit 500 --format json \
  | jq -r '.items[] | select(.status!="Done") | [.status, ((.assignees//[])|join(",")), (.repository|sub("https://github.com/";"")), (.content.number|tostring), .title[0:80]] | @tsv' | sort

# Issues, open and closed, that mention the task (try several keywords, English and Portuguese)
gh search issues --owner harumi-io "<keywords>" --limit 20 --json repository,number,title,state,assignees \
  --jq '.[] | "\(.repository.nameWithOwner)#\(.number) [\(.state)] @\((.assignees//[])|map(.login)|join(",")) \(.title[0:70])"'

# Open PRs across the org, humans only
gh search prs --owner harumi-io --state open --limit 100 --json repository,number,title,author \
  --jq '.[] | select(.author.login|test("\\[bot\\]|^Copilot$")|not) | "\(.repository.nameWithOwner)#\(.number) @\(.author.login) \(.title[0:70])"'

# Open PRs in the repo you will change, bots included, with the files they touch
gh pr list --state open --limit 50 --json number,title,author,files \
  --jq '.[] | "#\(.number) @\(.author.login) \(.title[0:60]) [\(.files|map(.path)|.[0:4]|join(", "))]"'
```

- **Collision:** an "In progress" or "In review" item, open PR or branch by someone else that touches
  the same files, endpoint, table or feature. **Stop and ask me** (continue, coordinate or pick
  another task) before writing code. My own work is not a collision.
- **Bot PRs** (Dependabot, `harumi-docs-drift-manager`) are a heads-up about rebase and merge order
  when they touch the files you change (dependency manifests, workflows, docs, `cli-surface.json`).
  They are not a reason to stop.
- A **closed** issue that looks like the same problem: tell me, it may be a regression or duplicate.

### Create or use the issue

- A matching issue that is mine or unassigned: use it. One assigned to someone else is a collision.
- No match: **create one now, without asking**, in the repo where the code lands, with the right type:
  `gh issue create --type <Feature|Bug|Task|Chore> --title "Area: short description" --assignee @me --body-file <file>`
  (or `--template "<Bug|Feature|Task|Chore>"` to start from the repo's template). Fill the template's
  sections; use labels that already exist (`gh label list`) and never invent one. If the work needs more
  than one PR or repo, make a parent with sub-issues (see "Big tasks" below).
- If the issue is mine but stale or vague, fix it with `gh issue edit` so it states the real scope.
- Make sure it is on the board, then set **In progress**:

```bash
gh project item-add 4 --owner harumi-io --url <issue-url>   # skip if already on the board

item_id() { gh project item-list 4 --owner harumi-io --limit 500 --format json \
  | jq -r --arg r "$1" --argjson n "$2" '.items[] | select(.content.number==$n and (.repository|endswith("/"+$r))) | .id'; }
set_status() {  # set_status <repo> <issue-number> "<Status name>"; ids are looked up by name
  local f; f=$(gh project field-list 4 --owner harumi-io --format json)
  gh project item-edit --project-id "$(gh project view 4 --owner harumi-io --format json --jq .id)" \
    --id "$(item_id "$1" "$2")" \
    --field-id "$(jq -r '.fields[]|select(.name=="Status").id' <<<"$f")" \
    --single-select-option-id "$(jq -r --arg s "$3" '.fields[]|select(.name=="Status").options[]|select(.name==$s).id' <<<"$f")"
}

set_status <repo> <issue-number> "In progress"
```

Status names: `To Do`, `In progress`, `In review`, `Done`. If one is rejected the board was reconfigured: read
the current options with `gh project field-list 4 --owner harumi-io`. If `item_id` is empty right after
`item-add`, retry once.

### What each Status means

| Status | Means |
| --- | --- |
| `To Do` | Not started, or paused or dropped. Nobody is working on it. |
| `In progress` | Someone is working on it: a local or pushed branch, a draft PR, or a PR that only `Refs` the issue (part of the work). |
| `In review` | All the code that closes the issue is pushed in a non-draft PR (or merged) and is meant to be complete, but has not been reviewed or shown to work. Never for a draft, a partial PR or work that exists only locally. |
| `Done` | Merged and shown to work, or closed as not needed. |

### Link the PR and keep Status true

- The PR body names the issue: `Closes #123`, or `Closes harumi-io/<repo>#123` across repos. Use
  `Refs` when the PR only delivers part. For a PR that already exists, `gh pr edit --body`.
- Status follows the work, not the PR. A draft PR, or one that only `Refs` the issue, stays
  **In progress**. Set **In review** only when the PR is not a draft and holds all the code that
  closes the issue. Merged: GitHub moves it to **Done**; if it was never shown to work, put it
  back to **In review** and say what is unverified.
- Built-in board workflows move some cards by themselves (closed or merged goes to Done). After each
  step read the card's Status and set it only if it is wrong.
- Work paused or dropped: back to **To Do**, unassign if appropriate, and say so.
- Scope changed mid-task: update the issue so it stays true.
- Say what you did, one line per action ("Created harumi-api#512, set In progress").

### Naming and format

- **Branch:** `type/short-kebab-slug`.
- **Commit and PR title:** `type(scope): subject`. Types: `feat fix perf refactor docs test ci chore revert`.
  Scope is an optional lowercase area. Present tense, lowercase, no trailing period, 72 characters or
  fewer. The commit body explains why. No `Co-Authored-By` trailers. Exempt: `Promote dev to main`,
  `chore(deploy-dev): …`, bot PRs.
- **PR body:** use the PR template: `What`, `Why`, `Testing`, `Issue` (`Closes #N`, or `Refs #N` if the
  PR is partial) and, only for auth, billing, data, deploy or infra changes, `Risk`. Open as a draft
  and push every change to it while it is a draft (the review bot skips drafts). When the work is
  complete, CI is green and you have read your own diff, mark it ready yourself (`gh pr ready <n>`)
  and set the issue to In review, so the bot reviews once, on the final state. Do not mark it ready
  earlier, and do not wait for a person to do it. One concern per PR, about 400 changed lines or
  fewer (lockfiles and generated files do not count); say why if it is larger. UI changes get a
  screenshot. Do not force-push after review starts.
- **Issue:** title `Area: short description`, in English. Set the issue type (`Feature`, `Bug`, `Task`
  or `Chore`) instead of a type label, and use the matching template. One problem per issue; name its
  blockers.
- Never put secrets or customer data in an issue, PR, comment or screenshot.

### Priority and effort

These are org issue fields set on the issue, not labels and not board columns: `Priority` (Urgent,
High, Medium, Low) and `Effort` (High, Medium, Low). Set them only when I say so; never guess.

```bash
set_issue_field() {  # set_issue_field <repo> <issue-number> <field name> <value>
  local id; id=$(gh api orgs/harumi-io/issue-fields | jq -r --arg n "$3" '.[]|select(.name==$n).id')
  jq -n --argjson id "$id" --arg v "$4" '{issue_field_values:[{field_id:$id,value:$v}]}' \
    | gh api -X POST "repos/harumi-io/$1/issues/$2/issue-field-values" --input -
}
set_issue_field <repo> <issue-number> Priority High
```

### Big tasks: one parent, sub-issues for the pieces

If a task needs more than one PR, or touches more than one repo, do not file a pile of flat issues.
File **one parent** (type `Feature`: the goal, acceptance criteria, out of scope and the order of the
pieces) and **one sub-issue per PR-sized piece**:

```bash
gh issue create --type Feature --title "Billing: ..." --body-file parent.md
gh issue create --type Task --title "Billing 1: ..." --parent <parent-number-or-url>
gh issue edit <n> --parent <parent-number-or-url>   # attach an existing issue
```

- Number the pieces in order (`Billing 1`, `Billing 2a`, `Billing 2b`). A piece may live in another
  repo: pass the parent's URL.
- Each piece's PR says `Closes #piece`; the last one also closes the parent.
- On the board only the parent is meant to show, with its sub-issue progress bar; the board views
  filter pieces out (`no:parent-issue`). Pieces are still on the project, so the checks above see them.
  Set Status on the piece you work on. The parent goes **In progress** with the first piece and
  **Done** when the last one closes.

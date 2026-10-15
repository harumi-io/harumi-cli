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
- No match: **create one now, without asking**, in the repo where the code lands:
  `gh issue create --title "<title>" --assignee @me --body "<body>"`. Title short and specific, in the
  language of the repo's nearby issues. Body: the problem or goal, the approach in a few lines, what
  "done" looks like. Use labels that already exist (`gh label list`); never invent one. If the work
  spans repos, one issue in the main repo that names the others.
- If the issue is mine but stale or vague, fix it with `gh issue edit` so it states the real scope.
- Make sure it is on the board, then set **In progress**:

```bash
gh project item-add 4 --owner harumi-io --url <issue-url>   # skip if already on the board

item_id() { gh project item-list 4 --owner harumi-io --limit 500 --format json \
  | jq -r --arg r "$1" --argjson n "$2" '.items[] | select(.content.number==$n and (.repository|endswith("/"+$r))) | .id'; }
set_status() { gh project item-edit --project-id PVT_kwDOCpjL3c4BZpZI --id "$(item_id "$1" "$2")" \
  --field-id PVTSSF_lADOCpjL3c4BZpZIzhUmm0Q --single-select-option-id "$3"; }

set_status <repo> <issue-number> b4aa00e3   # In progress
```

Status ids: To Do `ea9597ea`, In progress `b4aa00e3`, In review `e19a2055`, Done `f1ccd3da`. If one is
rejected the board was reconfigured: re-read it with `gh project field-list 4 --owner harumi-io`.
If `item_id` is empty right after `item-add`, retry once.

### Link the PR and keep Status true

- The PR body names the issue: `Closes #123`, or `Closes harumi-io/<repo>#123` across repos. Use
  `Refs` when the PR only delivers part. For a PR that already exists, `gh pr edit --body`.
- PR ready for review: **In review**. Merged or closed: **Done**.
- Built-in board workflows move some cards by themselves (closed or merged goes to Done). After each
  step read the card's Status and set it only if it is wrong.
- Work paused or dropped: back to **To Do**, unassign if appropriate, and say so.
- Scope changed mid-task: update the issue so it stays true.
- Say what you did, one line per action ("Created harumi-api#512, set In progress").

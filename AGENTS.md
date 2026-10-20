# AGENTS.md

Agent-facing conventions for this repo.

## Issues, PRs and the project board

All work is tracked on one org board, [Harumi's Dev Project](https://github.com/orgs/harumi-io/projects/4)
(`harumi-io`, project `4`), across every repo. Before you change code, check it for existing work, so nobody collides with or
duplicates anyone else. You never write to it: the cards move by themselves (see "How cards move"), which
keeps the GitHub GraphQL quota free. "Me" is the person running the session (`gh api user --jq .login`).

This section replaces any older "do not edit issues or PRs" rule.

### What you may do

Without asking:

- create issues in any harumi-io repo for the work you are about to do;
- edit, label, assign, comment on and link issues and PRs that **I authored or am assigned to**.

Ask first before you edit, comment on, label, reassign, re-status or close an issue or PR that
belongs to someone else. Never delete an issue, PR, comment or board item.

**Do not write the board.** No `gh project item-add`, `item-edit`, `item-list` or `field-list`, and
no `set_status`: they are GraphQL, and many agents share one token, so they exhaust its 5,000/hour
quota and everything starts failing with `API rate limit already exceeded`. Cards move by
themselves ("How cards move"). Only a person moves one by hand, in the project UI, to pause an item.

### Before writing code

Skip this for read-only questions and reviews. Otherwise check, then say in a line or two what you
found. Everything below is **REST**, which has its own quota, and nothing queries the board:

```bash
# Issues and PRs, open AND closed, that mention the task (try several keywords, English and Portuguese)
s() { gh api -X GET search/issues -f q="$1 org:harumi-io" -f per_page=20 \
 --jq '.items[] | "\(.repository_url|sub(".*/";""))#\(.number) [\(.state)]\(if .pull_request then " PR" else "" end) @\((.assignees//[])|map(.login)|join(",")) \(.title[0:70])"'; }
s "<keywords>"
s "<keywords> is:open is:pr -author:app/dependabot"

# Open PRs in this repo (draft or not), then the files of any that look related
gh api "repos/{owner}/{repo}/pulls?per_page=50" --jq '.[] | "#\(.number) @\(.user.login) draft=\(.draft) \(.title[0:60])"'
gh api "repos/{owner}/{repo}/pulls/<n>/files" --jq '.[].filename'

# Remote branches touching the area
git fetch origin --prune && git branch -r | rg -i '<keyword>'
```

- **Collision:** an open issue assigned to someone else, or an open PR or branch by someone else,
 that touches the same files, endpoint, table or feature. **Stop and ask me** (continue,
 coordinate or pick another task) before writing code. My own work is not a collision.
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
- **Do not add it to the board or set its Status.** `add-to-project.yml` puts a new issue on the
 board at `To Do`, and the rest follows from its PR.
- GraphQL exhausted (`gh api rate_limit --jq .resources.graphql`)? `gh issue create` and
 `gh pr create` fail, but REST works: `gh api -X POST repos/{owner}/{repo}/issues -f title=... -f type=Task`
 and `gh api -X POST repos/{owner}/{repo}/pulls -F draft=true ...`. `gh pr ready` has no REST form:
 wait for the reset (`reset` in the same output) instead of retrying in a loop.

### How cards move

Nobody sets Status, agents included. The board does, from the PR:

| Status | Means | Moved there by |
| --- | --- | --- |
| `To Do` | Not started, or paused or dropped. Nobody is working on it. | `add-to-project.yml` when the issue is opened. |
| `In progress` | A PR that closes the issue exists and is a draft, or the issue was reopened. | The board's `Pull request linked to issue` and `Item reopened`; `pr-status.yml` when a PR goes back to draft. |
| `In review` | The PR that closes the issue is out of draft: the code is meant to be complete, not yet reviewed or shown to work. | `pr-status.yml`, when the PR is marked ready. |
| `Done` | Merged, or the issue closed. | The board's `Pull request merged` and `Item closed`. |

What you do: create the issue, open the PR as a **draft** with `Closes #N` in the body, and run
`gh pr ready <n>` when the work is complete. The card follows.

- `Refs #N` does **not** link the issue, so a PR that only `Refs` it leaves the card alone. That
 is right for partial work.
- Work with no PR yet (a local or pushed branch) leaves the card at `To Do`. Open the draft PR early.
- Merged but never shown to work? Reopen the issue and say what is unverified; the board follows.
- Never edit the card to "fix" it. If it looks wrong, say so to me.

### Link the PR

- The PR body names the issue: `Closes #123`, or `Closes harumi-io/<repo>#123` across repos. Use
 `Refs` when the PR only delivers part. For a PR that already exists, `gh pr edit --body`.
- Work paused or dropped: say so on the issue and unassign if appropriate; I move the card.
- Scope changed mid-task: update the issue so it stays true.
- Say what you did, one line per action ("Created harumi-api#512", "Opened draft PR #513").

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
  and the board moves the issue to In review, so the bot reviews once, on the final state. Do not mark it ready
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
  filter pieces out (`no:parent-issue`). Pieces are still on the project, so the collision search sees them.
 A piece's card moves with its PR. The parent has no PR of its own, so it stays **To Do** until it
 closes (**Done**); I may move it by hand meanwhile.

# DevCockpit

**See what is in progress, ready, or needs attention across GitHub repositories without opening each one.**

DevCockpit combines GitHub Issues, Projects v2, pull requests, reviews, CI checks, and releases into a read-only workspace. It shows current work, assigned work that has not started, available work, review requests, failing checks, and recent releases. A compact Brief view is available for quick status conversations.

GitHub remains the source of truth. DevCockpit cannot edit issues, pull requests, assignees, priorities, Project fields, or releases. SQLite is a disposable cache for GitHub data and observed changes between syncs.

## Requirements

- Linux with Python 3.10 or newer and the `venv` module (`start.sh` uses Linux `/proc` to restart an existing instance)
- [GitHub CLI](https://cli.github.com/) (`gh`) installed and authenticated to an account that can read the selected repositories
- Access to `api.github.com`

No personal access token or `.env` file is needed. DevCockpit calls `gh api` and `gh api graphql` using your existing CLI login. It does not read or store the CLI's credentials.

## Quick start

```bash
gh auth login
gh auth status
cp config.example.yml config.yml
# Edit config.yml with your own repositories and GitHub Project settings.
./start.sh
```

Open <http://127.0.0.1:5000>. `start.sh` creates `.venv`, installs dependencies when `requirements.txt` changes, checks the CLI login and configuration file, and starts the app. Re-running it stops a prior DevCockpit instance started from the same directory before starting a new one. Other processes are left alone. Set `HOST` and `PORT` in the process environment if you need a different listener.

The first full sync runs in the background. The UI shows the last successful sync and any repository-specific errors. **Sync now** requests an immediate refresh; the configured interval controls later automatic syncs. Syncs do not overlap. After changing `config.yml`, restart with `./start.sh` to load it, then allow the full sync to finish.

To rebuild the cache from GitHub, stop the app, remove `instance/cockpit.sqlite`, and start again. Observed change history begins anew after a rebuild; current GitHub facts are restored by the full sync.

## Configure repositories and Projects

`config.yml` is your private local configuration and is ignored by Git. Start from [`config.example.yml`](config.example.yml), then replace every example owner, repository path, and GitHub login with your own values. The example repository paths are placeholders, not working demo data. YAML indentation matters: use spaces, not tabs.

| Setting | Meaning |
| --- | --- |
| `github.organization` | Default owner for organization Projects; it does not limit which repositories can be listed. |
| `github.username` | Your GitHub login for the **My Issues** and **My Pull Requests** shortcuts. This is separate from the team list. |
| `repositories[].name` | Display name, unique within DevCockpit. |
| `repositories[].url` | Repository path in `/owner/repo` form. |
| `repositories[].project_number` | Number in the GitHub Project v2 URL for that repository, or `null` when none is used. Different repositories may use different Projects. |
| `repositories[].project_owner` | Optional owner override for a Project. |
| `repositories[].project_owner_type` | Set to `user` for a user-owned Project; organization is the default. |
| `team[].github` / `team[].name` | Exact GitHub login and a display name for a person shown in Team and Brief. Other contributors still appear on issues and PRs. |
| `sync.interval_seconds` | Automatic sync interval; minimum 10 seconds. |

### Repositories and Project ownership

Each entry in `repositories` is synchronized independently. `name` is the label shown in the UI; `url` identifies the actual GitHub repository. Find `project_number` at the end of a Project URL such as `https://github.com/orgs/example-org/projects/7` (number `7`). For a Project owned by a user, also set `project_owner` to that user's login and `project_owner_type: user`. Without those overrides, DevCockpit looks for an organization Project under `github.organization` (or the repository owner if no organization is configured).

Use `project_number: null` for a repository without a Project. Its issues, PRs, checks, and releases still sync, but workflow status is unavailable. Adding or changing repositories and Projects requires restarting `./start.sh`; **Sync now** refreshes data using the configuration already loaded by the running process.

### Workflow mapping

The `workflow` block tells DevCockpit **which GitHub Project field to read** and **how its option names map to the five stages shown in the UI**. It does not create those options or change any Project item.

```yaml
workflow:
  status_field: Status
  values:
    backlog: Backlog
    ready: Ready
    in_progress: In Progress
    in_review: In Review
    done: Done
```

`status_field` must match the name of the Project's single-select status field. The keys under `values` are DevCockpit's fixed stage identifiers; their values are the **exact, case-sensitive option names in GitHub**. For example, if your Project option says `In progress`, set `in_progress: In progress`. All configured Projects share this one mapping, so their status field and option names need to agree. If they differ, align the GitHub Project options before expecting one combined workflow view.

Only GitHub's Project status puts an issue in Backlog, Ready, In Progress, In Review, or Done. Assigning someone to an issue does **not** mark it In Progress; opening a PR does **not** mark it In Review. Backlog becomes Ready only when someone changes the Project status in GitHub. An issue outside its configured Project is shown as **Not in project**. A Project item without a status is shown as **No status**. An unreadable Project or field is shown as **Unavailable**, never silently treated as Backlog.

### Priority and team

Set `priority.source` to `project` to read the named Project field, or `issue_field` to read an organization Issue Field. `priority.field` is the exact GitHub field name. DevCockpit never combines those sources or substitutes a local priority. Issues and PRs still sync when a repository has no Project; workflow information is then unavailable. Repository overview pages provide direct links to configured Projects.

Add one `team` entry per person whose work you want in the Team and Brief views. `github` must be the exact GitHub login used as an issue assignee or PR author; `name` is only the display label. The example includes three placeholder members. People omitted from `team` still appear on their issues and PRs, but do not get a person section. Duplicate logins are collapsed. `github.username` above controls only the personal quick filters; add yourself to `team` as well if you want your own person section.

## GitHub access

The authenticated `gh` account needs read access to repository metadata, issues, pull requests, contents, checks or commit statuses, Actions, releases, and each configured Project. Organization Projects may require organization approval or the `read:project` scope. If GitHub reports a missing scope, run this yourself and sync again:

```bash
gh auth refresh -s read:project
```

DevCockpit does not change CLI authentication or request write permissions. GitHub may return 404 for repositories the account cannot read. A failure for one repository is shown in the UI and does not stop other repositories from syncing.

## Views

- **Briefing** (`/`): changes since the previous browser visit, **Needs me**, team work, Ready issues, recent GitHub Releases, and repository state.
- **Brief** (`/brief`): compact overview for a quick conversation. **Show as text** opens the same visible facts as selectable plain text; **Copy text** puts them on the clipboard for manual use elsewhere.
- **Now** (`/now`): open issues explicitly marked In Progress in their GitHub Project.
- **Team** (`/team`): each person's Now, Review, Ready next, Assigned Backlog, and open PRs.
- **Issues** (`/issues`), **Pull Requests** (`/pulls`), and **Releases** (`/releases`): searchable and filterable lists. Filters apply when selections change or typing pauses.
- **Repositories** (`/repositories`): release, workflow, PR, and attention summary for each repository, with direct links to its GitHub Project.
- **Board** (`/board`): read-only cross-repository view of Project status.
- **Search** (`/search`): search cached issues and PRs.

**Needs me** gives the reason for each item, including Urgent or High priority open issues, review requests, requested changes, and failing CI. PR review and CI states are technical signals separate from Project workflow status. PR age is calculated from the actual GitHub creation timestamp. Issue–PR links use GitHub's closing references, not title matching.

**Recently shipped** shows published GitHub Releases only, by default going back three calendar months. Drafts are excluded and prereleases are marked. Merged PRs are not counted as releases.

**Since your last visit** compares browser visits with changes observed between successful syncs. The visit timestamp is kept in that browser; observations are retained in SQLite for 30 days. It cannot reconstruct changes that appeared and disappeared between syncs.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Tests mock GitHub CLI responses and need no authenticated account or private configuration.

## Limits and safety

- DevCockpit has no user login. It binds to localhost by default; use an access-controlled proxy if you expose it to other users.
- GitHub API permissions and availability determine what the app can show. Missing workflow or priority data is labelled, never guessed.
- Review state summarizes submitted reviews; it does not reimplement branch protection or merge queue rules. CI shows check and status results without interpreting logs.
- Large repositories can make a full sync take time. A repository's previous cached data may remain visible when its refresh fails, alongside its error and last successful sync time.
- Keep `config.yml`, `.env`, and `instance/` private. They are excluded from Git. GitHub avatar images are loaded directly from GitHub in the browser.

The code is released under [CC0 1.0 Universal](LICENSE). Vendored Lucide icons retain their own [license](app/static/LUCIDE-LICENSE.txt).

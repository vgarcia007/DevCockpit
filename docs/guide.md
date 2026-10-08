# Configuration and feature guide

DevCockpit is a local, read-only dashboard. It reads GitHub through your signed-in `gh` CLI, and optionally reads OTRS tickets and Zabbix monitoring. It never edits issues, tickets, or monitoring configuration.

For installation and a first start, follow the [README](../README.md). Copy [`config.example.yml`](../config.example.yml) to `config.yml`, replace the example values, then run `./start.sh`. Open [http://127.0.0.1:7777](http://127.0.0.1:7777). Keep the terminal running; `Ctrl+C` stops the app. Run `./start.sh` again after configuration changes to restart this checkout.

## GitHub repositories and Projects

Each `repositories` entry needs a unique display `name` and a `url` in `/owner/repo` form. At least one repository is required. Set `github.username` to your own GitHub login for My work and personal filters.

Set `project_number` to the number at the end of a GitHub Projects v2 URL, such as `https://github.com/orgs/example-org/projects/7`, or use `null` for a repository without a Project. For a user-owned Project, also set `project_owner` to the person's login and `project_owner_type: user`. Otherwise, DevCockpit uses `project_owner`, `github.organization`, or the repository owner.

All configured Projects use one workflow mapping. `workflow.status_field` is the Project's status field name. The values below must match its options exactly, including case:

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

Project status determines the workflow groups in Overview, Now, Team, and Issues. Assigning someone does not mark an issue In Progress; opening a PR does not mark it In Review. Issues outside the Project show **Not in project**. Unreadable Projects or fields show **Unavailable**. Open GitHub Project links to manage the actual boards; the former `/board` route redirects to Issues.

### Priority

Choose the source your team uses:

```yaml
priority:
  source: issue_field # Or project for a Projects v2 field.
  field: Priority
```

`issue_field` reads an organization Issue Field on the issue itself. `project` reads a field on the Project item. The same field name in both places does not make them the same value. `priority.field` is the exact field name; **Urgent** and **High** are the values used for Need attention. Priority is refreshed during regular syncs, including when it changes on an existing issue.

### People and ticket assignments

Add people under `team` using their GitHub login and display name:

```yaml
team:
  - github: example-login
    name: Example Person
    project_number: 7 # Optional: github.com/users/example-login/projects/7.
    otrs_user: example-agent # Optional; omit when no OTRS mapping is needed.
```

An optional `team[].project_number` reads the member's Project directly, including Draft Issues. The owner defaults to the member's GitHub login and the type to `user`. For an organization-owned Project, add `project_owner: example-org` and `project_owner_type: organization` to the same team entry; an omitted organization owner defaults to `github.organization`. For another user's Project, set `project_owner` to their login and `project_owner_type: user`. Assignment remains with the configured team member regardless of who owns the Project. Use the positive number at the end of its URL, not a GraphQL ID; omit it or set `null` to disable it. All its tasks count for that member, even without GitHub assignees. The same workflow mapping applies. Draft priorities always use the Project field; real issues follow `priority.source`.

Personal tasks appear in Overview, Brief and its exports, Now, Team, My work, Issues/Board, Search and Statistics. Issues offers a source filter for personal projects. Draft Issues appear like other issues, without a Draft label or an invented issue number, and open the GitHub Project. A draft in the configured Done status is completed; moving it out of Done reopens it. Statistics records the first observed transition to Done, not an inferred GitHub closure date. Drafts already Done at first import have no completion date. Archived and removed items disappear from the working views.

Real issues appear once. A configured repository Project takes precedence for workflow and priority; the personal owner is added to the assignment used by work views and filters. Without a repository Project, the personal Project supplies those values. Multiple personal Projects use the first owner's GitHub login alphabetically. Actual GitHub assignees remain unchanged. Failed reads keep cached data and show a warning and the last successful project sync. Restart after configuration changes.

Team also shows issues assigned to those people in other repositories visible to your `gh` account. Those repositories do not have to be configured. Issues already tracked through a personal Project are excluded from this additional list. Remaining search results stay separate from the configured Project workflow. If this search fails or is incomplete, the person card shows a warning.

**My work** (`/work`) opens with `github.username` and can switch to any configured team member. Its first work section highlights assigned open issues in the configured In Progress status (for example, Doing). Below it are all open assigned issues from configured and other accessible repositories, open PRs created by or assigned to the person, requested reviews, and matching OTRS tickets.

`otrs_user` matches the OTRS **Besitzer** (owner) or **Verantwortlicher** (responsible) login or email; the responsible email's part before `@` can also match. Matching ignores case. Give each person a unique mapping. Add yourself to `team` with `otrs_user` if you want your tickets in My work.

Team cards on Overview and Team use two columns that pack cards vertically without stretching shorter cards. Smaller screens use one column. Team's person search filters these cards.

## Sync and GitHub access

DevCockpit uses `gh api` and `gh api graphql` with your existing GitHub CLI login. No GitHub token belongs in `config.yml`. Startup checks that `gh` is installed and authenticated before starting the app. Your account must be able to read each repository and Project. If Project access is missing, try `gh auth refresh -s read:project`.

| Source | Interval setting | Default | Minimum |
| --- | --- | --- | --- |
| GitHub | `sync.interval_seconds` | 300 seconds | 10 seconds |
| OTRS | `otrs.interval_seconds` | 900 seconds | 60 seconds |
| Zabbix | `zabbix.interval_seconds` | 60 seconds | 60 seconds |

Sources run independently. The next sync is scheduled after the previous run finishes. The sidebar shows a status and countdown for each enabled source. Hover or focus a row for the last successful sync and errors. Missing integration accounts pause that source until an account is saved; GitHub continues to work.

Conditional GitHub REST requests reuse cached responses when GitHub reports no change. When GitHub reports a rate limit, DevCockpit waits until the retry time and shows the pause in the sidebar. Repository-specific errors appear in the UI; failed syncs keep the last successful data.

Syncs do not reload the page automatically. An **Updates available** hint lets you choose when to show the latest data, preserving the URL, filters, and scroll position. The hint indicates a newer sync snapshot, which may contain unchanged data. The notification bell and sidebar continue updating while you work.

The top bar checks the branch that started Dev-Cockpit against its tracked remote every five minutes. If the local commit and remote branch commit differ, **Neue Version verfügbar** links to the repository. A checkout without a tracked branch, or a temporarily unreachable remote, keeps this hint hidden. The check does not fetch or modify the local checkout.

## Optional OTRS 5 tickets

Uncomment the `otrs` example in `config.yml`, then set your HTTPS frontend URL and queue IDs. Enter and verify your agent login in **Settings → Accounts** after starting the app. The integration uses an agent session and the AgentTicketSearch CSV export, without saving a search profile.

| Setting | Purpose |
| --- | --- |
| `enabled` | Set `false` to disable OTRS. Omitting the section also disables it. |
| `url` | HTTPS frontend URL, including any installation path. |
| `queue_ids` | Nonempty list of numeric queue IDs to read. |
| `excluded_states` | Status names to hide from active ticket views, ignoring case and surrounding spaces. Use your installation's closed/merged states. |
| `attention_queue_ids` | Configured queues whose active tickets appear in Need attention and notifications. Empty by default. |
| `highlight_queue_ids` | Configured queues whose rows are emphasized in the ticket table. Empty by default. |
| `interval_seconds` | Independent sync interval, as shown above. |

Tickets has its own searchable, filterable table. Ticket numbers open OTRS in a new tab. Tickets are separate from GitHub Issues. Person mappings add separate ticket groups to Overview, Now, Team, My work, and Brief; global Search also has a separate ticket section.

When mappings exist, the integration reads the responsible agent from the CSV's **Verantwortlicher** or **Responsible** column. If that column is missing, it reads each active ticket's detail page. An OTRS administrator can add `Responsible` to `Ticket::Frontend::AgentTicketSearch###SearchCSVData` to avoid those extra requests.

Disabling OTRS clears its local ticket cache and OTRS notifications on restart. Ticket sections disappear and GitHub views continue working. The saved account is retained for later use.

## Optional Zabbix 7.4 monitoring

Uncomment the `zabbix` example and set the HTTPS frontend URL, including its installation path. List each application's exact technical host name with `environment: prod` or `environment: preprod`. Enter your normal username and password under **Settings → Accounts**; no API token is needed.

The account must be allowed to use the API and read the selected hosts through `host.get`, `trigger.get`, and `event.get`. Password login requires an account without Zabbix MFA. The app logs out the API session after each sync.

**Monitoring → Applications** shows hosts and problem history. Search or filter by application, environment, severity, and open/resolved status. Acknowledged and suppressed problems remain visible. Resolved problems show their end time and duration, and links open Zabbix.

| Setting | Purpose |
| --- | --- |
| `enabled` | Set `false` to disable Zabbix. Omitting the section also disables it. |
| `url` | HTTPS frontend URL, including any installation path. |
| `hosts` | Nonempty list of technical `host` names and their `environment`. |
| `interval_seconds` | Independent sync interval, as shown above. |
| `history_days` | History window, 1–365 days; default 30. Older problems still open are included too. |
| `attention_min_severity` | Minimum severity for open problems in Need attention; default 0. |

Severity values: `0` Not classified, `1` Information, `2` Warning, `3` Average, `4` High, `5` Disaster. Need attention includes open problems from both environments at or above the configured threshold. Acknowledgement does not resolve a problem.

History includes short incidents that started and ended between syncs, subject to Zabbix retention and account permissions. Failed syncs keep the last successful data. Zabbix problems are not currently included in the notification bell or weekly Statistics. Disabling Zabbix stops its sync and hides monitoring data after a restart; its saved account and local cache remain.

## Accounts and storage

Enabled OTRS and Zabbix integrations need an account in **Settings → Accounts**. The app tests the connection before saving; an invalid replacement keeps the previous account. Passwords are never displayed. Change or remove an account on the same page, after any running sync finishes. An account change clears that integration's cache and starts a fresh sync.

Accounts survive restarts and are bound to their provider and server URL. Changing the URL requires an account for the new server. Disabling an integration retains its account.

| Local file | Contents |
| --- | --- |
| `config.yml` | Repository, workflow, team, and integration settings; no passwords. |
| `instance/cockpit.sqlite` | Cached source data and observed changes. |
| `~/.local/share/devcockpit/accounts.enc` | Accounts encrypted with AES-256-GCM. |
| `~/.config/devcockpit/account.key` | Random encryption key. |

Account directories have permissions `0700`, files `0600`. There is no master password or OS keyring requirement. Anyone who can read both account files can decrypt the credentials. Cached tickets and issues are not encrypted; keep your OS account and files private.

Back up both account files together. If the key is lost or storage damaged, restore the matching pair. To reset accounts, stop the app, remove both files, restart, and enter the accounts again. An existing encrypted file never gets a replacement key silently. Legacy `user` and `password` config fields are removed on startup without importing them; re-enter the accounts in the browser.

## Views, attention, and notifications

| View | What it shows |
| --- | --- |
| **Briefing / Overview** (`/`) | Observed changes, Need attention, team work, Ready issues, recent releases, and repository state. |
| **My work** | Personal GitHub items and mapped tickets, with a person selector. |
| **Now / Team** | Current work and each configured person's assignments. The activity dot indicates In Progress issues in configured Projects; hover or focus for details. |
| **Issues** | GitHub issues with search and filters, including workflow and priority. |
| **Pull Requests** | PRs, reviews, and CI status; the filter can hide Dependabot PRs. |
| **Repositories** | Repository summaries and Project links. |
| **Releases** | Published releases and a timeline; select a release to read its notes. |
| **Statistics** | Weekly activity across repositories, plus optional OTRS ticket counts. |
| **Search** | Cached issues and PRs, plus a separate ticket section when enabled. |
| **Brief** (`/brief`) | A compact text summary. **Copy as prompt** copies [`brief_prompt.txt`](../brief_prompt.txt) and the summary; it sends nothing to an AI service. |

**Wiki-Update kopieren** on Brief copies German Markdown for manual wiki updates, without an AI request. It groups all cached issues from configured GitHub repositories by project: open issues with the configured Backlog status go under Backlog, other open issues under current work with their actual status. Closed issues and published releases cover the last 30 days; prereleases are marked and drafts excluded. Entries include source links and issue assignees. The export includes its date, last GitHub sync, sync errors, and a placeholder for meetings, approvals and dependencies. Closed issues and releases do not confirm a production deployment. When OTRS is enabled, the export also includes all active tickets from `attention_queue_ids` under **OTRS – Handlungsbedarf**, using the same `excluded_states` filter as Brief. Tickets include source links, queues, statuses, priorities, owners and responsible agents, plus the last OTRS sync and any sync error. Active tickets are included regardless of age. Existing wiki content and annual archives are not read or replaced; external team repositories are not included.

### Need attention

This is a view of the latest synced state. It includes:

- Open issues with the configured priority **Urgent** or **High**.
- Open issues whose Project status is Done.
- Open PRs with failing CI, changes requested, waiting reviews, or approval before the latest commit.
- Active OTRS tickets in `attention_queue_ids`, when OTRS is enabled.
- Open Zabbix problems at the configured severity threshold, when Zabbix is enabled.

An item disappears when a successful sync sees that its condition no longer applies, such as an issue being closed. Use **Show** when the update hint appears to refresh the visible page; a restart is not required.

### Since your last visit and the bell

**Since your last visit** shows GitHub changes observed between successful syncs, rather than all currently open work. It includes workflow moves, urgent priority, issue closure, PR/review/CI changes, and releases. Mark individual items done and restore them in **Completed**. Completion and visit state live in this browser. A new visit starts when you return after at least 30 minutes away from the tab or on a new day; reloading during the same visit keeps its comparison. The local database keeps observed changes for 30 days. The first visit starts the comparison; events that appear and disappear between GitHub syncs may not be seen.

The notification bell shows observed GitHub changes and changes in configured OTRS attention queues, including new tickets, changes, and closure. Opening it clears the unread count. Browser alerts are optional and grouped; enable them in the bell menu and allow the browser permission. They work while the tab is open. Browser notification state is separate from the visit checklist, and the first visit does not alert on old history.

**Recently shipped** uses published GitHub Releases, including prereleases, from the selected period. Drafts and merged PRs are not releases.

### Weekly Statistics

Weeks run Monday through Sunday in Europe/Berlin time. Statistics counts created issues, currently closed issues by closure date, merged PRs, and published releases. It combines repositories without assigning activity to people.

By default, only configured repositories with a Project are included. Switch to **All configured repositories** to also include repositories without a Project; this does not include other repositories found through person assignments. Previous/next controls select a week.

When OTRS is enabled, its configured queues contribute created and closed ticket counts independently of the GitHub repository filter. `excluded_states` also defines which ticket statuses count as closed. Tickets without a closure date cannot be assigned to a closure week; the page reports these separately. Counts reflect the latest synced records, not an immutable audit log; reopened issues or changed source records can change past counts.

## Troubleshooting and local use

| Symptom | What to check |
| --- | --- |
| App will not start | Confirm Python 3.10+, `venv`, `gh auth status`, and a valid `config.yml` with at least one repository. Read the terminal error. |
| Repository or Project missing | Check account access, repository path, Project owner/number, and `read:project` scope. Hover the sidebar sync row and read repository warnings. |
| Workflow or priority missing | Check the exact field/option names and the priority source. An issue must belong to the configured Project for Project fields to apply. |
| OTRS or Zabbix says Account required | Open Settings → Accounts and verify an account for the configured server URL. |
| Integration connection fails | Check the frontend URL/path, network or VPN, account permissions, and sidebar error details. Zabbix password API login cannot use MFA. |
| A sync finished but the page looks unchanged | Click Show in Updates available. Regular syncs preserve your current view until then. |
| Personal tickets missing | Add the person to `team`, check `otrs_user` against owner/responsible, and confirm the ticket queue and status are included. |

To rebuild the local cache, stop the app, delete `instance/cockpit.sqlite`, and start again. This resets cached data and locally observed change history for all sources; it does not change upstream data or delete saved accounts. Browser visit and notification preferences are separate.

DevCockpit has no user login and listens on localhost by default. Do not expose it without access control. `config.yml` and `instance/` are ignored by Git. Dates use Europe/Berlin. Run the checks with `.venv/bin/python -m pytest -q`.

## Vulnerabilities

Explore → **Vulnerabilities** combines Dependabot, code scanning and secret scanning alerts for every configured repository. It defaults to open alerts. Filter by repository, alert type, status, severity or search text; sort by severity, update time or creation time. Fixed/resolved and dismissed alerts remain available through the status filter. Open the linked GitHub alert to investigate or change its state.

Alerts refresh with the regular GitHub sync. Each repository and source has an independent snapshot: an unavailable source retains its previous data. The Vulnerabilities page hides warning banners, including notices about disabled or inaccessible scanners. Missing sources can therefore make the displayed list incomplete. Click **Show** in **Updates available** to refresh the view.

The active `gh` account needs access to the repositories and security alerts. Fine-grained tokens need read permissions for **Dependabot alerts**, **Code scanning alerts**, and **Secret scanning alerts**; classic-token scopes and repository roles also depend on the source. See the [Dependabot API](https://docs.github.com/en/rest/dependabot/alerts), [code scanning API](https://docs.github.com/en/rest/code-scanning/code-scanning), and [secret scanning API](https://docs.github.com/en/rest/secret-scanning/secret-scanning). Disabled or unlicensed scanning sources may not be accessible.

Secret values and code snippets are not stored or displayed. Security response bodies bypass the raw API cache. Secret scanning has no severity assigned by this page; it displays **Unknown**. Code scanning uses security severity when available and otherwise its rule severity. The page lists one row per GitHub alert; it does not retrieve additional secret locations or code scanning instances.

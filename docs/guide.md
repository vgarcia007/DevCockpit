# Configuration and feature guide

Start with the short [README](../README.md) and copy [`config.example.yml`](../config.example.yml) to your private `config.yml`.

## GitHub repositories and Projects

Each `repositories` entry has a unique display `name` and a `url` in `/owner/repo` form. Set `project_number` to the number at the end of a GitHub Projects v2 URL, such as `https://github.com/orgs/example-org/projects/7`, or use `null` for a repository without a Project. To use a user-owned Project, set `project_owner` to the person's login and `project_owner_type: user`. Otherwise, DevCockpit uses `github.organization` or the repository owner.

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

Only Project status places an issue in a Board stage. Assigning someone does not mark it In Progress; opening a PR does not mark it In Review. Issues outside the Project show **Not in project**. Unreadable Projects or fields show **Unavailable**.

Set `priority.source: project` to read a Project field, or `priority.source: issue_field` to read an organization Issue Field. `priority.field` is its exact name. These sources are separate; choose the one your team actually uses. The priority signal supports Urgent and High issues under **Need attention**.

Add people under `team` to show their work in Team and Brief. Use their exact GitHub login. Team also lists issues assigned to them in other repositories visible to your `gh` account; you do not need to add those repositories to `repositories`. Those issues stay separate from the configured Project workflow and Board totals. `github.username` controls the personal quick filters; add yourself to `team` separately if you want a person section.

**My work** (`/work`) opens with `github.username` and can show any configured team member. It lists open assigned issues from configured and other accessible repositories, open PRs created by or assigned to the person, requested reviews, and matching OTRS tickets. To map tickets, add `otrs_user` to each relevant `team` entry. The value matches the OTRS **Besitzer** (owner), **Verantwortlicher** (responsible) name or email address, or the part of the responsible email before `@`. Add your own GitHub login to `team` with `otrs_user` if you want your tickets in My work.

## GitHub access and sync

DevCockpit uses your local `gh` login to read repositories, issues, pull requests, checks, releases, and configured Projects. It uses `gh api` and `gh api graphql`; there is no separate GitHub token in `config.yml`. A missing Project scope may require `gh auth refresh -s read:project`. Repository-specific sync errors appear in the UI.

GitHub sync runs every `sync.interval_seconds` after the previous sync completes (default: 300 seconds; minimum: 10). The sidebar shows one status row per enabled source, with its next automatic sync. Hover or focus a row for the last successful sync and error details. Syncs do not reload the page automatically. An **Updates available** hint lets you choose when to show the latest data, preserving the current URL, filters, and scroll position. The hint indicates a newer sync snapshot, which may contain unchanged data. Conditional REST requests reuse cached responses when GitHub reports no change. When GitHub reports a rate limit, DevCockpit waits until the retry time and shows that in the sidebar. Configuration changes require a restart.

To rebuild the local GitHub cache, stop the app, delete `instance/cockpit.sqlite`, and start it again. This also clears locally observed change history. It does not change GitHub data.

## Optional OTRS 5 tickets

Add the commented `otrs` block from the example configuration and replace every example value. `enabled`, `url`, and `queue_ids` configure the integration. Enter the login under **Settings → Accounts**; credentials are encrypted outside the checkout. The URL must use HTTPS. `queue_ids` is a nonempty list of numeric OTRS queue IDs. The integration uses an agent session and the AgentTicketSearch CSV export. It does not save a search profile.

`excluded_states` lists ticket status names to hide, ignoring case and surrounding spaces. Set this to the terminal states used by your installation. `attention_queue_ids` selects configured queues whose open tickets appear on the Overview and in notifications. `highlight_queue_ids` selects configured queues to emphasize in the ticket table. Both lists are empty by default. OTRS syncs independently every `otrs.interval_seconds` (default: 900 seconds; minimum: 60). Set `otrs.enabled: false` or remove the block to disable the integration; its cached tickets and OTRS notifications are cleared on restart.

When `otrs_user` mappings are configured, My work also reads the responsible agent for active tickets. If the OTRS CSV export has a **Verantwortlicher** or **Responsible** column, it uses that. Otherwise, it reads each active ticket's detail page. An OTRS administrator can add `Responsible` to `Ticket::Frontend::AgentTicketSearch###SearchCSVData` to avoid those extra detail requests.

The Tickets section is visible only when OTRS is enabled. It has its own table, search, filters, and ticket links. Ticket data is not included in the GitHub Issues or Board views.
Mapped OTRS tickets also appear as separate groups on Overview, Now, Team, My work, and Brief. Brief's copied text includes those tickets. Global Search adds a separate ticket result section. When OTRS is disabled, these ticket sections disappear and the GitHub views continue to work.

## Views and notifications

- **Briefing** (`/`) shows observed changes, Need attention, team work, Ready issues, recent releases, and repository state.
- **Brief** (`/brief`) provides a compact text summary. **Copy as prompt** copies [`brief_prompt.txt`](../brief_prompt.txt) followed by the summary to the clipboard; it does not send data to an AI service.
- **Now**, **Team**, **Issues**, **Pull Requests**, **Repositories**, **Releases**, and **Board** show their respective GitHub data. Board has repository and assignee filters. Releases has a timeline with clickable notes.
- **Search** searches cached issues and pull requests, plus tickets when OTRS is enabled. **Tickets** is the optional dedicated OTRS view.

**Since your last visit** shows changes observed between successful GitHub syncs. You can mark individual items done and restore them in Completed. Those choices and your last visit are stored in this browser. Observed changes are kept in the local database for 30 days. Events that appear and disappear between syncs may not be seen.

The notification bell shows observed GitHub changes and changes in configured OTRS attention queues. Opening it clears the unread count. Browser alerts are optional, grouped, and work while the tab is open. Notification state is stored in this browser; the first visit does not alert on old history.

**Recently shipped** uses published GitHub Releases, including prereleases, from the selected period. Drafts and merged PRs are not counted as releases.

## Local use

DevCockpit has no user login and listens on localhost by default. Use access control if you choose to expose it. `config.yml` and `instance/` are ignored by Git; keep the configuration and local database private. The app displays dates in the Europe/Berlin time zone.

On Windows, start from a Windows terminal with [`start.bat`](../start.bat). Windows and WSL have separate Python environments and `gh` sessions. Keep the terminal open while using the app.

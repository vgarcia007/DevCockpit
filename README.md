# DevCockpit

A local, read-only dashboard for a team's GitHub work. It brings Issues, Projects v2 status, pull requests, reviews, CI checks, and releases into one place. Optional OTRS 5 and Zabbix 7.4 integrations show tickets and application monitoring. DevCockpit does not edit these systems.

![Overview with example data](docs/screenshots/overview.png)
*Overview with fictional demo data.*

## What you need

- Python 3.10+ and `venv` on Linux or WSL
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and signed in with an account that can read your repositories `sudo apt update && sudo apt install -y gh`
- Read access to your GitHub Projects v2 if you want workflow status

No GitHub token or `.env` file is needed. OTRS and Zabbix are optional.

## Start on Linux or WSL

```bash
gh auth login
cp config.example.yml config.yml
# Edit config.yml: set your GitHub login and replace /example-org/my-app.
./start.sh
```

Open [http://127.0.0.1:7777](http://127.0.0.1:7777). The first sync runs in the background. `start.sh` creates a virtual environment and installs dependencies. Run it again after changing `config.yml`; it restarts this checkout's app.

## Configure GitHub

In your private `config.yml`, set:

| Setting | What to enter |
| --- | --- |
| `repositories[].url` | Each repository as `/owner/repo`. At least one is required. |
| `repositories[].name` | A unique name shown in DevCockpit. |
| `github.username` | Your GitHub login for the “My Issues” and “My Pull Requests” shortcuts. |
| `repositories[].project_number` | The number at the end of a Projects v2 URL, or `null` if this repository has no Project. |

If you use Projects, make the `workflow.values` match your Project's **exact** Status option names. Set `priority.source` to `project` for a Project field or `issue_field` for an organization Issue Field. Add people under `team` to show their work in Team and Brief. **My work** uses `github.username` and can switch to any configured team member. The supplied [`config.example.yml`](config.example.yml) contains these settings with one example repository.

If a Project is unavailable, GitHub may need the `read:project` scope: `gh auth refresh -s read:project`. DevCockpit shows sync errors in the UI.

## Optional OTRS tickets

Remove the comment markers from the `otrs` example in `config.yml`, then enter your own HTTPS URL, agent login, password, and queue IDs. `url` and `queue_ids` are required when OTRS is enabled. Tickets use a separate 15-minute sync and table.

Use `excluded_states` to hide closed statuses, `attention_queue_ids` for tickets shown under **Need attention** and in notifications, and `highlight_queue_ids` for emphasized table rows. These lists are empty unless configured. Set `otrs.enabled: false` to stop OTRS and delete its local cache on restart. Keep `config.yml` private.

To include a person's tickets in **My work**, Overview, Now, Team, and Brief, add `otrs_user` to their `team` entry. It must match the OTRS owner or responsible agent. Add yourself to `team` too if you want your own tickets there. Search shows ticket results separately when OTRS is enabled. With `otrs.enabled: false`, the GitHub views continue without ticket data.

![Ticket table with example data](docs/screenshots/tickets.png)
*Optional OTRS view with fictional demo data.*

## Optional Zabbix monitoring

Add `zabbix` to your private `config.yml` (an example is in `config.example.yml`):

```yaml
zabbix:
  enabled: true
  url: https://monitoring.example.com/zabbix/
  user: your-login
  password: your-password
  interval_seconds: 60
  attention_min_severity: 0
  history_days: 30
  hosts:
    - host: app.example.com
      environment: prod
    - host: staging.example.com
      environment: preprod
```

Use the frontend URL including its installation path, and the exact technical host names from Zabbix. Your account needs API access to `host.get`, `trigger.get`, and `event.get`. Password login requires an account without Zabbix MFA. Sessions are logged out after each sync.

**Monitoring → Applications** shows hosts and problem history, including acknowledged and suppressed problems. By default, it shows open and resolved problems from the last 30 days, plus older problems that are still open. Short incidents between syncs are retrieved from Zabbix too. Filter by status, application, environment or severity, or search the problem list. Resolved problems show their end time and total duration. Set `history_days` (1–365, default 30) to change the history window; available history depends on Zabbix retention and your account permissions. **Need attention** includes only open problems from both environments at or above `attention_min_severity`: `0` Not classified, `1` Information, `2` Warning, `3` Average, `4` High, `5` Disaster. The default `0` includes everything.

Zabbix syncs independently every 60 seconds. Failed syncs keep the last successful data and display an error. Set `zabbix.enabled: false` or remove the section to disable it. Restart with `./start.sh` after config changes. Keep credentials in the private config; they are never sent to the browser.

## Good to know

- GitHub syncs every 5 minutes by default. The sidebar shows the status and next sync for every enabled source; OTRS and Zabbix sync independently. New sync results show an **Updates available** hint. Choose **Show** to refresh the view while keeping its filters and scroll position.
- The notification bell reports observed GitHub changes and changes in configured OTRS attention queues. Browser alerts are optional and work while the tab is open.
- DevCockpit has no user login and binds to localhost by default. Do not expose it without access control.
- `config.yml` and `instance/` are ignored by Git. The SQLite database is a local cache.

See the [configuration and feature guide](docs/guide.md) for Project mapping, permissions, views, and troubleshooting.

Run checks with `.venv/bin/python -m pytest -q`. The code is [CC0](LICENSE); vendored Lucide icons have their own [license](app/static/LUCIDE-LICENSE.txt).

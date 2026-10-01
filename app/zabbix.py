"""Independent read-only Zabbix 7.4 API integration."""

import logging
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

import requests
from sqlalchemy import delete

from .models import ZabbixHost, ZabbixProblem, ZabbixSyncState

LOG = logging.getLogger(__name__)
SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")


def problem_duration(value, resolved_at=None):
    end = resolved_at.replace(tzinfo=timezone.utc) if resolved_at else datetime.now(timezone.utc)
    seconds = max(0, int((end - value.replace(tzinfo=timezone.utc)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    return (f"{days}d " if days else "") + (f"{hours}h " if hours else "") + f"{minutes}m"


class ZabbixError(Exception):
    pass


def problem_url(config, problem):
    return urljoin(config["url"].rstrip("/") + "/", "tr_events.php") + "?" + urlencode(
        {"triggerid": problem.triggerid, "eventid": problem.eventid})


def host_url(config, host):
    return urljoin(config["url"].rstrip("/") + "/", "zabbix.php") + "?" + urlencode(
        {"action": "problem.view", "filter_set": "1", "hostids[]": host.hostid})


class ZabbixClient:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.endpoint = urljoin(config["url"].rstrip("/") + "/", "api_jsonrpc.php")
        self.sequence = 0

    def call(self, method, params):
        self.sequence += 1
        try:
            response = self.session.post(self.endpoint, json={"jsonrpc": "2.0", "method": method,
                "params": params, "id": self.sequence}, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise ZabbixError(f"Zabbix {method} failed (HTTP {response.status_code}); check the frontend URL")
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ZabbixError("Zabbix connection failed or returned an invalid API response") from exc
        if not isinstance(payload, dict) or payload.get("id") != self.sequence:
            raise ZabbixError("Zabbix returned an invalid API response")
        if "error" in payload:
            raise ZabbixError(f"Zabbix {method} failed; check credentials and API permissions")
        if "result" not in payload:
            raise ZabbixError("Zabbix returned an invalid API response")
        return payload["result"]

    def fetch_all(self):
        logged_in = False
        try:
            token = self.call("user.login", {"username": self.config["user"], "password": self.config["password"]})
            if not isinstance(token, str) or not token:
                raise ZabbixError("Zabbix login failed")
            self.session.headers["Authorization"] = "Bearer " + token
            logged_in = True
            configured = {host["host"]: host for host in self.config["hosts"]}
            raw_hosts = self.call("host.get", {"filter": {"host": list(configured)},
                "output": ["hostid", "host", "name", "status"]})
            missing = set(configured) - {host["host"] for host in raw_hosts}
            if missing:
                raise ZabbixError("Configured Zabbix hosts are missing or inaccessible: " + ", ".join(sorted(missing)))
            hostids = {host["hostid"] for host in raw_hosts}
            triggers = self.call("trigger.get", {"hostids": sorted(hostids),
                "output": ["triggerid"], "selectHosts": ["hostid"]})
            trigger_hosts = {trigger["triggerid"]: sorted({host["hostid"] for host in trigger["hosts"]} & hostids)
                             for trigger in triggers}
            hosts = [dict(hostid=host["hostid"], host=host["host"], name=host["name"],
                environment=configured[host["host"]]["environment"], enabled=host["status"] == "0",
                trigger_count=sum(host["hostid"] in ids for ids in trigger_hosts.values())) for host in raw_hosts]
            now = datetime.now(timezone.utc)
            # Include events that overlapped the window, including older problems still open.
            params = {"hostids": sorted(hostids), "source": 0, "object": 0, "value": 1,
                "problem_time_from": int((now - timedelta(days=self.config["history_days"])).timestamp()),
                "problem_time_till": int(now.timestamp()),
                "output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged", "suppressed", "r_eventid"],
                "selectHosts": ["hostid"], "selectSuppressionData": "extend",
                "sortfield": "eventid", "sortorder": "ASC", "limit": 500}
            raw_problems = []
            while True:
                page = self.call("event.get", params)
                raw_problems.extend(page)
                if len(page) < params["limit"]:
                    break
                next_id = str(int(page[-1]["eventid"]) + 1)
                if int(next_id) <= int(params.get("eventid_from", "0")):
                    raise ZabbixError("Zabbix history pagination failed")
                params["eventid_from"] = next_id
            recovery_ids = sorted({p["r_eventid"] for p in raw_problems if p["r_eventid"] != "0"})
            recoveries = {}
            for offset in range(0, len(recovery_ids), 500):
                rows = self.call("event.get", {"eventids": recovery_ids[offset:offset + 500],
                                               "output": ["eventid", "clock"]})
                recoveries.update({row["eventid"]: datetime.fromtimestamp(int(row["clock"]), timezone.utc)
                                   for row in rows})
            if set(recovery_ids) - recoveries.keys():
                raise ZabbixError("Zabbix recovery events are missing or inaccessible; retry the sync")
            problems = {}
            for problem in raw_problems:
                ids = sorted({host["hostid"] for host in problem["hosts"]} & hostids)
                if not ids:
                    raise ZabbixError("Zabbix problem hosts could not be resolved; retry the sync")
                severity = int(problem["severity"])
                if severity not in range(len(SEVERITIES)):
                    raise ZabbixError("Zabbix returned an unknown problem severity")
                problems[problem["eventid"]] = dict(eventid=problem["eventid"], triggerid=problem["objectid"],
                    name=problem["name"], severity=severity,
                    started_at=datetime.fromtimestamp(int(problem["clock"]), timezone.utc), hostids=ids,
                    resolved_at=recoveries.get(problem["r_eventid"]),
                    acknowledged=problem["acknowledged"] == "1",
                    suppressed=problem.get("suppressed") == "1" or bool(problem.get("suppression_data")))
            return hosts, list(problems.values())
        finally:
            if logged_in:
                try:
                    self.call("user.logout", [])
                except Exception:
                    LOG.warning("Zabbix API logout failed")
            self.session.close()


class ZabbixSyncManager:
    def __init__(self, sessions, config):
        self.sessions = sessions
        self.config = config
        self.lock = threading.Lock()
        self.running = False
        self.scheduler_enabled = False
        self.timer = None

    def enable_scheduler(self):
        self.scheduler_enabled = True
        self.start()

    def start(self):
        if not self.lock.acquire(blocking=False):
            return False
        if self.timer:
            self.timer.cancel()
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="zabbix-sync").start()
        return True

    def run_sync(self):
        if not self.lock.acquire(blocking=False):
            return False
        self.running = True
        self._run()
        return True

    def _run(self):
        attempted = datetime.now(timezone.utc)
        try:
            hosts, problems = ZabbixClient(self.config).fetch_all()
            with self.sessions() as session:
                session.execute(delete(ZabbixProblem))
                session.execute(delete(ZabbixHost))
                session.add_all(ZabbixHost(**host) for host in hosts)
                session.add_all(ZabbixProblem(**problem) for problem in problems)
                state = session.get(ZabbixSyncState, "monitoring") or ZabbixSyncState(key="monitoring")
                state.last_attempt, state.last_success, state.error = attempted, datetime.now(timezone.utc), None
                session.add(state)
                session.commit()
            LOG.info("Zabbix sync completed: %s hosts, %s problems (%s open)", len(hosts), len(problems),
                     sum(problem.get("resolved_at") is None for problem in problems))
        except Exception as exc:
            LOG.warning("Zabbix sync failed: %s", type(exc).__name__)
            with self.sessions() as session:
                state = session.get(ZabbixSyncState, "monitoring") or ZabbixSyncState(key="monitoring")
                state.last_attempt = attempted
                state.error = str(exc) if isinstance(exc, ZabbixError) else "Zabbix sync failed"
                session.add(state)
                session.commit()
        finally:
            self.running = False
            self.lock.release()
            if self.scheduler_enabled:
                self.timer = threading.Timer(self.config["interval_seconds"], self.start)
                self.timer.daemon = True
                self.timer.start()

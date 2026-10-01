"""Independent, read-only OTRS AgentTicketSearch CSV importer."""

import csv
import io
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import delete, select

from .accounts import AccountSyncMixin
from .models import OTRSObservedChange, OTRSSyncState, OTRSTicket, OTRSTicketStat

LOG = logging.getLogger(__name__)
SEARCH_LIMIT = 2000  # Default OTRS 5 AgentTicketSearch limit.
CSV_FIELDS = {"Ticketnummer", "Erstellt", "Geschlossen", "Status", "Priorität", "Queue", "Besitzer", "Betreff"}


def excluded(state, config):
    return state.strip().casefold() in {value.strip().casefold() for value in config["excluded_states"]}


def detect_ticket_changes(previous, current, config, observed_at):
    """Return at most one event per ticket for a completed OTRS sync."""
    watched = set(config["attention_queue_ids"])
    base_url = urljoin(config["url"].rstrip("/") + "/", "index.pl")
    changes = []
    for number, row in current.items():
        old = previous.get(number)
        was_watched = old is not None and old.queue_id in watched
        is_watched = row["queue_id"] in watched
        is_closed = excluded(row["state"], config)
        if was_watched and is_closed:
            kind = "otrs_closed"
            detail = f"Status {row['state']}"
        elif is_watched and not is_closed and not was_watched:
            kind = "otrs_attention"
            detail = "Ticket entered an attention queue"
        elif is_watched and not is_closed and old:
            status_changed = old.state != row["state"]
            priority_changed = old.priority != row["priority"]
            if not status_changed and not priority_changed:
                continue
            kind = "otrs_changed"
            detail = " · ".join(part for part in (
                f"Status {old.state} → {row['state']}" if status_changed else "",
                f"Priority {old.priority} → {row['priority']}" if priority_changed else "") if part)
        else:
            continue
        changes.append(OTRSObservedChange(
            number=number, queue_id=old.queue_id if kind == "otrs_closed" else row["queue_id"],
            queue=old.queue if kind == "otrs_closed" else row["queue"],
            kind=kind, title=f"Ticket {number}: {row['subject']}",
            url=f"{base_url}?Action=AgentTicketZoom;TicketNumber={number}",
            detail=detail, observed_at=observed_at))
    return changes


class OTRSError(Exception):
    pass


class ChallengeTokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            attributes = dict(attrs)
            if attributes.get("name") == "ChallengeToken":
                self.token = attributes.get("value")


class ResponsibleParser(HTMLParser):
    """Read the responsible agent from the TicketInfo sidebar on AgentTicketZoom."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.in_label = False
        self.label_text = []
        self.in_popup_label = False
        self.popup_label_text = []
        self.expect_value = False
        self.expect_email = False
        self.value_depth = None
        self.value_text = []
        self.email_depth = None
        self.email_text = []
        self.responsible = None
        self.email = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, attributes))
        in_ticket_info = any(node.get("id") == "TicketInfo" for _, node in self.stack)
        in_popup = any(node.get("id") == "ResponsibleDetails" for _, node in self.stack)
        if in_popup and tag == "label":
            self.in_popup_label = True
            self.popup_label_text = []
        elif in_ticket_info and tag == "label":
            self.in_label = True
            self.label_text = []
        if in_popup and self.expect_email and tag == "p" and "Value" in attributes.get("class", "").split():
            self.email_depth = len(self.stack)
            self.email_text = []
        elif in_ticket_info and not in_popup and self.expect_value and tag == "p" and "Value" in attributes.get("class", "").split():
            self.value_depth = len(self.stack)
            self.value_text = []

    def handle_endtag(self, tag):
        if self.in_label and tag == "label":
            label = " ".join(self.label_text).strip().rstrip(":").casefold()
            self.expect_value = label in ("verantwortlicher", "responsible")
            self.in_label = False
        if self.in_popup_label and tag == "label":
            label = " ".join(self.popup_label_text).strip().rstrip(":").casefold()
            self.expect_email = label in ("e-mail", "email")
            self.in_popup_label = False
        if self.email_depth == len(self.stack) and tag == "p":
            self.email = " ".join(" ".join(self.email_text).split())
            self.email_depth = None
            self.expect_email = False
        if self.value_depth == len(self.stack) and tag == "p":
            self.responsible = " ".join(" ".join(self.value_text).split())
            self.value_depth = None
            self.expect_value = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.in_label:
            self.label_text.append(data)
        if self.in_popup_label:
            self.popup_label_text.append(data)
        if self.value_depth is not None and not any(node.get("id") == "ResponsibleDetails" for _, node in self.stack):
            self.value_text.append(data)
        if self.email_depth is not None:
            self.email_text.append(data)


def created_at(value):
    value = value.strip()
    if not value:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            local = datetime.strptime(value, pattern).replace(tzinfo=ZoneInfo("Europe/Berlin"))
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
    raise OTRSError("OTRS CSV contains an unknown creation date format")


def parse_csv(content):
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";")
        if not reader.fieldnames or not CSV_FIELDS.issubset(reader.fieldnames):
            raise OTRSError("OTRS CSV has unexpected columns")
        rows = {}
        for row in reader:
            number = (row.get("Ticketnummer") or "").strip()
            if not number or not number.isdecimal():
                raise OTRSError("OTRS CSV contains an invalid ticket number")
            rows[number] = dict(number=number, subject=row["Betreff"] or "",
                                queue=row["Queue"] or "", state=row["Status"] or "",
                                priority=row["Priorität"] or "", owner=row["Besitzer"] or "",
                                responsible=(row.get("Verantwortlicher") or row.get("Responsible") or "").strip()
                                if "Verantwortlicher" in row or "Responsible" in row else None,
                                responsible_email=None,
                                created_at=created_at(row["Erstellt"] or ""),
                                closed_at=created_at(row["Geschlossen"] or ""))
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise OTRSError("OTRS CSV could not be decoded") from exc


class OTRSClient:
    def __init__(self, config, session=None, include_responsible=False):
        self.config = config
        self.session = session or requests.Session()
        self.include_responsible = include_responsible
        self.url = urljoin(config["url"].rstrip("/") + "/", "index.pl")
        self.token = None

    def login(self):
        response = self.session.post(self.url, data={"Action": "Login", "User": self.config["user"],
                                                     "Password": self.config["password"]}, timeout=30)
        response.raise_for_status()
        response = self.session.get(self.url, params={"Action": "AgentTicketSearch"}, timeout=30)
        response.raise_for_status()
        parser = ChallengeTokenParser()
        parser.feed(response.text)
        if not parser.token:
            raise OTRSError("OTRS login failed or ticket search is unavailable")
        self.token = parser.token

    def search(self, queue_id, start=None, stop=None):
        if not self.token:
            self.login()
        data = [("Action", "AgentTicketSearch"), ("Subaction", "Search"),
                ("ChallengeToken", self.token), ("ResultForm", "CSV"),
                ("QueueIDs", str(queue_id))]
        if start is not None:
            data.append(("TimeSearchType", "TimeSlot"))
            for prefix, value in (("TicketCreateTimeStart", start), ("TicketCreateTimeStop", stop)):
                data.extend([(prefix + "Year", str(value.year)), (prefix + "Month", f"{value.month:02d}"),
                             (prefix + "Day", f"{value.day:02d}")])
        response = self.session.post(self.url, data=data, timeout=60)
        response.raise_for_status()
        if "text/csv" not in response.headers.get("Content-Type", "").lower():
            raise OTRSError("OTRS did not return a CSV export; the session may have expired")
        return parse_csv(response.content)

    def all_in_queue(self, queue_id, start=None, stop=None):
        rows = self.search(queue_id, start, stop)
        if len(rows) < SEARCH_LIMIT:
            return rows
        if start is None:
            start, stop = date(1970, 1, 1), datetime.now(ZoneInfo("Europe/Berlin")).date() + timedelta(days=1)
        if start >= stop:
            raise OTRSError("OTRS search limit reached for a single day; ticket list is incomplete")
        midpoint = start + (stop - start) // 2
        return {**self.all_in_queue(queue_id, start, midpoint),
                **self.all_in_queue(queue_id, midpoint + timedelta(days=1), stop)}

    def ticket_responsible(self, number):
        response = self.session.get(self.url, params={"Action": "AgentTicketZoom", "TicketNumber": number}, timeout=30)
        response.raise_for_status()
        parser = ResponsibleParser()
        parser.feed(response.text)
        if parser.responsible is None:
            raise OTRSError("OTRS ticket detail did not include a responsible agent")
        return parser.responsible, parser.email

    def fetch_all(self):
        try:
            self.login()
            rows = {}
            for queue_id in self.config["queue_ids"]:
                for number, row in self.all_in_queue(queue_id).items():
                    rows[number] = {**row, "queue_id": queue_id}
            if self.include_responsible:
                for number, row in rows.items():
                    if not excluded(row["state"], self.config) and row["responsible"] is None:
                        row["responsible"], row["responsible_email"] = self.ticket_responsible(number)
            return rows
        finally:
            self.session.close()


class OTRSSyncManager(AccountSyncMixin):
    def __init__(self, sessions, config, include_responsible=False, credential_provider=None):
        self.init_accounts(credential_provider)
        self.sessions = sessions
        self.config = config
        self.include_responsible = include_responsible
        self.lock = threading.Lock()
        self.timer = None
        self.scheduler_enabled = False
        self.running = False
        self.next_sync_at = None

    def enable_scheduler(self):
        self.scheduler_enabled = True
        self.start()

    def start(self):
        if not self.lock.acquire(blocking=False):
            return False
        if not self.credentials_available():
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.next_sync_at = None
            self.lock.release()
            return False
        if self.timer:
            self.timer.cancel()
        self.next_sync_at = None
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="otrs-sync").start()
        return True

    def run_sync(self):
        if not self.lock.acquire(blocking=False):
            return False
        if not self.credentials_available():
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.next_sync_at = None
            self.lock.release()
            return False
        if self.timer:
            self.timer.cancel()
        self.next_sync_at = None
        self.running = True
        self._run()
        return True

    def _run(self):
        attempted = datetime.now(timezone.utc)
        try:
            rows = OTRSClient(self.client_config(), include_responsible=self.include_responsible).fetch_all()
            active_rows = [row for row in rows.values() if not excluded(row["state"], self.config)]
            with self.sessions() as session:
                state = session.get(OTRSSyncState, "tickets") or OTRSSyncState(key="tickets")
                previous = {row.number: row for row in session.scalars(select(OTRSTicket)).all()}
                baseline_exists = state.last_success is not None and all(row.queue_id is not None for row in previous.values())
                if baseline_exists:
                    session.add_all(detect_ticket_changes(previous, rows, self.config, attempted))
                session.execute(delete(OTRSTicket))
                session.add_all(OTRSTicket(**{key: value for key, value in row.items() if key != "closed_at"})
                                for row in active_rows)
                session.execute(delete(OTRSTicketStat))
                session.add_all(OTRSTicketStat(**{key: row.get(key) for key in
                    ("number", "subject", "queue", "queue_id", "state", "created_at", "closed_at")})
                    for row in rows.values())
                session.execute(delete(OTRSObservedChange).where(
                    OTRSObservedChange.observed_at < attempted - timedelta(days=30)))
                state.last_attempt = attempted
                state.last_success = datetime.now(timezone.utc)
                state.error = None
                session.add(state)
                session.commit()
            LOG.info("OTRS sync completed: %s active tickets", len(active_rows))
        except Exception as exc:
            # Never log HTTP responses or request bodies: they may contain credentials or ticket data.
            LOG.warning("OTRS sync failed: %s", type(exc).__name__)
            with self.sessions() as session:
                state = session.get(OTRSSyncState, "tickets") or OTRSSyncState(key="tickets")
                state.last_attempt = attempted
                state.error = str(exc) if isinstance(exc, OTRSError) else "OTRS connection failed"
                session.add(state)
                session.commit()
        finally:
            try:
                if self.scheduler_enabled:
                    self.schedule_next()
            finally:
                self.running = False
                self.lock.release()

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc)


class Repository(Base):
    __tablename__ = "repositories"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    github_url: Mapped[str] = mapped_column(String, nullable=False)
    project_number: Mapped[int | None] = mapped_column(Integer)
    project_state: Mapped[str] = mapped_column(String, default="unavailable")
    error: Mapped[str | None] = mapped_column(Text)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issues: Mapped[list["Issue"]] = relationship(back_populates="repository", cascade="all, delete-orphan")
    pulls: Mapped[list["Pull"]] = relationship(back_populates="repository", cascade="all, delete-orphan")
    releases: Mapped[list["Release"]] = relationship(back_populates="repository", cascade="all, delete-orphan")


class Issue(Base):
    __tablename__ = "issues"
    github_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_name: Mapped[str] = mapped_column(ForeignKey("repositories.name"), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, index=True)
    author: Mapped[str | None] = mapped_column(String)
    assignees: Mapped[list] = mapped_column(JSON, default=list)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    milestone: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow: Mapped[str | None] = mapped_column(String, index=True)
    workflow_state: Mapped[str] = mapped_column(String, default="unavailable")
    priority: Mapped[str | None] = mapped_column(String, index=True)
    priority_state: Mapped[str] = mapped_column(String, default="unavailable")
    project_item_id: Mapped[str | None] = mapped_column(String)
    related_pulls: Mapped[list] = mapped_column(JSON, default=list)
    repository: Mapped[Repository] = relationship(back_populates="issues")


class Pull(Base):
    __tablename__ = "pulls"
    github_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_name: Mapped[str] = mapped_column(ForeignKey("repositories.name"), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String)
    author: Mapped[str | None] = mapped_column(String)
    assignees: Mapped[list] = mapped_column(JSON, default=list)
    requested_reviewers: Mapped[list] = mapped_column(JSON, default=list)
    reviews: Mapped[list] = mapped_column(JSON, default=list)
    draft: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String, index=True)
    merged: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_branch: Mapped[str] = mapped_column(String)
    head_branch: Mapped[str] = mapped_column(String)
    head_sha: Mapped[str] = mapped_column(String)
    mergeable: Mapped[bool | None] = mapped_column(Boolean)
    mergeable_state: Mapped[str | None] = mapped_column(String)
    ci_state: Mapped[str] = mapped_column(String, default="Unknown")
    review_state: Mapped[str] = mapped_column(String, default="Unknown")
    related_issues: Mapped[list] = mapped_column(JSON, default=list)
    repository: Mapped[Repository] = relationship(back_populates="pulls")


class Release(Base):
    __tablename__ = "releases"
    github_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_name: Mapped[str] = mapped_column(ForeignKey("repositories.name"), index=True)
    name: Mapped[str | None] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text)
    tag: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft: Mapped[bool] = mapped_column(Boolean)
    prerelease: Mapped[bool] = mapped_column(Boolean)
    author: Mapped[str | None] = mapped_column(String)
    repository: Mapped[Repository] = relationship(back_populates="releases")


class SyncMeta(Base):
    __tablename__ = "sync_meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ApiCache(Base):
    __tablename__ = "api_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    etag: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    last_used: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserAvatar(Base):
    __tablename__ = "user_avatars"
    login: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False)


class ObservedChange(Base):
    __tablename__ = "observed_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_name: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ExternalTeamIssue(Base):
    __tablename__ = "external_team_issues"
    login: Mapped[str] = mapped_column(String, primary_key=True)
    github_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_name: Mapped[str] = mapped_column(String)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalTeamSync(Base):
    __tablename__ = "external_team_sync"
    login: Mapped[str] = mapped_column(String, primary_key=True)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    incomplete: Mapped[bool] = mapped_column(Boolean, default=False)


class OTRSTicket(Base):
    __tablename__ = "otrs_tickets"
    number: Mapped[str] = mapped_column(String, primary_key=True)
    subject: Mapped[str] = mapped_column(Text)
    queue: Mapped[str] = mapped_column(String, index=True)
    queue_id: Mapped[int | None] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String, index=True)
    priority: Mapped[str] = mapped_column(String)
    owner: Mapped[str] = mapped_column(String)
    responsible: Mapped[str | None] = mapped_column(String)
    responsible_email: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class OTRSTicketStat(Base):
    """Latest complete OTRS queue snapshot for weekly statistics."""
    __tablename__ = "otrs_ticket_stats"
    number: Mapped[str] = mapped_column(String, primary_key=True)
    subject: Mapped[str] = mapped_column(Text)
    queue: Mapped[str] = mapped_column(String)
    queue_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class OTRSSyncState(Base):
    __tablename__ = "otrs_sync_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class OTRSObservedChange(Base):
    __tablename__ = "otrs_observed_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String)
    queue_id: Mapped[int] = mapped_column(Integer)
    queue: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ZabbixHost(Base):
    __tablename__ = "zabbix_hosts"
    hostid: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    environment: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean)
    trigger_count: Mapped[int] = mapped_column(Integer)


class ZabbixProblem(Base):
    __tablename__ = "zabbix_problems"
    eventid: Mapped[str] = mapped_column(String, primary_key=True)
    triggerid: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(Text)
    severity: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hostids: Mapped[list] = mapped_column(JSON)
    acknowledged: Mapped[bool] = mapped_column(Boolean)
    suppressed: Mapped[bool] = mapped_column(Boolean)


class ZabbixSyncState(Base):
    __tablename__ = "zabbix_sync_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


def make_session(database_path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        if "body" not in {column["name"] for column in inspect(connection).get_columns("releases")}:
            connection.execute(text("ALTER TABLE releases ADD COLUMN body TEXT"))
        if "queue_id" not in {column["name"] for column in inspect(connection).get_columns("otrs_tickets")}:
            connection.execute(text("ALTER TABLE otrs_tickets ADD COLUMN queue_id INTEGER"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_otrs_tickets_queue_id ON otrs_tickets (queue_id)"))
        if "responsible" not in {column["name"] for column in inspect(connection).get_columns("otrs_tickets")}:
            connection.execute(text("ALTER TABLE otrs_tickets ADD COLUMN responsible VARCHAR"))
        if "responsible_email" not in {column["name"] for column in inspect(connection).get_columns("otrs_tickets")}:
            connection.execute(text("ALTER TABLE otrs_tickets ADD COLUMN responsible_email VARCHAR"))
    return sessionmaker(bind=engine, expire_on_commit=False)

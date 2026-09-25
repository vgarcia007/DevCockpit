from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine
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


def make_session(database_path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)

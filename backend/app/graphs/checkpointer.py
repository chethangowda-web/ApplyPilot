import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import Connection
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("applypilot.checkpointer")

_SETUP_COMPLETED_DSNS: set[str] = set()


def _to_psycopg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://")


def _schema_already_current(dsn: str) -> bool:
    """Return True if all PostgresSaver migrations have already been applied."""
    expected_last_version = len(PostgresSaver.MIGRATIONS) - 1
    try:
        with Connection.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            reg = conn.execute("SELECT to_regclass('checkpoint_migrations')").fetchone()
            if not reg or reg[0] is None:
                return False
            row = conn.execute("SELECT MAX(v) FROM checkpoint_migrations").fetchone()
            return bool(row and row[0] is not None and int(row[0]) >= expected_last_version)
    except Exception:
        return False


def ensure_postgres_checkpointer_setup(database_url: str | None = None) -> None:
    """Idempotently initialize LangGraph PostgreSQL checkpointer tables outside request locks."""
    url = database_url or get_settings().database_url
    if not url.startswith("postgresql"):
        return
    dsn = _to_psycopg_dsn(url)
    if dsn in _SETUP_COMPLETED_DSNS:
        return
    if _schema_already_current(dsn):
        _SETUP_COMPLETED_DSNS.add(dsn)
        return
    with PostgresSaver.from_conn_string(dsn) as sync_saver:
        sync_saver.setup()
    _SETUP_COMPLETED_DSNS.add(dsn)


class _SyncPostgresAsyncBridge(BaseCheckpointSaver):
    """Bridges sync PostgresSaver for async LangGraph execution on Windows ProactorEventLoop."""

    def __init__(self, sync_saver: PostgresSaver) -> None:
        super().__init__(serde=sync_saver.serde)
        self._sync = sync_saver
        self._lock = asyncio.Lock()

    @property
    def config_specs(self):
        return self._sync.config_specs

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._sync.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self._sync.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._sync.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._sync.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._sync.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._sync.get_next_version(current, channel)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        async with self._lock:
            return await asyncio.to_thread(self._sync.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async with self._lock:
            items = await asyncio.to_thread(
                lambda: list(self._sync.list(config, filter=filter, before=before, limit=limit))
            )
        for item in items:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with self._lock:
            return await asyncio.to_thread(
                self._sync.put, config, checkpoint, metadata, new_versions
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._sync.put_writes, config, writes, task_id, task_path
            )

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._sync.delete_thread, thread_id)


@asynccontextmanager
async def managed_checkpointer(
    db: Session,
    *,
    override_checkpointer: Any | None = None,
) -> AsyncIterator[Any]:
    """Provide PostgresSaver (async native or sync bridge) on PostgreSQL and MemorySaver on SQLite."""
    if override_checkpointer is not None:
        yield override_checkpointer
        return

    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        url = str(db.bind.url) if db.bind is not None and db.bind.url else get_settings().database_url
        # Render full unmasked URL when Engine holds password
        if db.bind is not None:
            url = db.bind.url.render_as_string(hide_password=False)
        dsn = _to_psycopg_dsn(url)
        needs_setup = dsn not in _SETUP_COMPLETED_DSNS and not _schema_already_current(dsn)
        if needs_setup and db.in_transaction() and not (db.new or db.dirty or db.deleted):
            # Avoid PostgreSQL CREATE INDEX CONCURRENTLY self-deadlock against clean read tx
            db.commit()
        try:
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                if needs_setup:
                    await saver.setup()
                _SETUP_COMPLETED_DSNS.add(dsn)
                yield saver
                return
        except Exception as async_exc:
            try:
                with PostgresSaver.from_conn_string(dsn) as sync_saver:
                    if needs_setup:
                        await asyncio.to_thread(sync_saver.setup)
                    _SETUP_COMPLETED_DSNS.add(dsn)
                    yield _SyncPostgresAsyncBridge(sync_saver)
                    return
            except Exception as sync_exc:
                logger.warning(
                    "postgres_checkpointer_fallback",
                    extra={
                        "extra_fields": {
                            "async_error": type(async_exc).__name__,
                            "sync_error": type(sync_exc).__name__,
                        }
                    },
                )
    yield MemorySaver()

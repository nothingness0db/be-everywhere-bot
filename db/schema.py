from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

accounts = Table(
    "accounts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("network", String(32), nullable=False),
    Column("label", String(64), nullable=False),
    Column("remote_id", String(128), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("network", "label", name="uq_accounts_network_label"),
)

account_credentials = Table(
    "account_credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("key", String(64), nullable=False),
    Column("value", String(4096), nullable=False),
    UniqueConstraint("account_id", "key", name="uq_account_credentials_account_key"),
)

sync_mappings = Table(
    "sync_mappings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("source_post_id", String(64), nullable=False),
    Column("dest_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("dest_post_id", String(64), nullable=False),
    Column("synced_at", DateTime, nullable=False),
    UniqueConstraint(
        "source_account_id",
        "source_post_id",
        "dest_account_id",
        name="uq_sync_mappings_source_dest",
    ),
)

mirrored_posts = Table(
    "mirrored_posts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("post_id", String(64), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("account_id", "post_id", name="uq_mirrored_posts_account_post"),
)

account_sync_state = Table(
    "account_sync_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("last_synced_at", DateTime),
    UniqueConstraint("account_id", name="uq_account_sync_state_account"),
)

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", String(32), primary_key=True),
    Column("applied_at", DateTime, nullable=False),
)

# Dashboard settings override routing.toml and survive container replacement.
runtime_settings = Table(
    "runtime_settings",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

# Saved before acknowledging getUpdates so failed/delayed publishes survive restarts.
telegram_inbox = Table(
    "telegram_inbox",
    metadata,
    Column("account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("message_id", String(64), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("received_at", Float, nullable=False),
)

outbound_deliveries = Table(
    "outbound_deliveries",
    metadata,
    Column("source_account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("dest_account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("delivery_key", String(64), primary_key=True),
    Column("post_id", String(256), nullable=False),
    Column("reply_ref", Text),
)

account_health = Table(
    "account_health",
    metadata,
    Column("account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("detail", Text, nullable=False),
    Column("checked_at", Float, nullable=False),
)

sync_jobs = Table(
    "sync_jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_key", String(64), nullable=False, unique=True),
    Column("source_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("dest_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", Text, nullable=False, default=""),
    Column("dest_post_ids", Text, nullable=False, default="[]"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False, index=True),
)

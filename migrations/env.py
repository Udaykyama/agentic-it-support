from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from app.config import secret
from app.models import Base

import os

load_dotenv(override=False)
url = secret(os.environ, "MIGRATION_DATABASE_URL") or secret(os.environ, "DATABASE_URL")
if not url.startswith("postgresql+psycopg://"):
    raise ValueError("Set MIGRATION_DATABASE_URL to a PostgreSQL migration-owner connection")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()

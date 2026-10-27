from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.db.models import Base
from app.db.session import coerce_postgres_driver

target_metadata = Base.metadata
url = coerce_postgres_driver(get_settings().database_url)  # same driver normalisation as the app runtime


def run_migrations_online():
    with create_engine(url).connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()

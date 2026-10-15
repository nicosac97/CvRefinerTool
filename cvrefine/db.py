from collections.abc import Iterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from cvrefine import models  # noqa: F401  (registers tables)
from cvrefine.config import settings

CANDIDATE_KEY = "candidate_summary"

settings.data_dir.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})


def init_db(target: Engine = engine) -> None:
    SQLModel.metadata.create_all(target)
    _add_missing_columns(target)


def _add_missing_columns(target: Engine) -> None:
    """create_all never alters existing tables; add columns introduced after a database was created.

    SQLite cannot drop NOT NULL constraints, so columns are added nullable with the model default.
    """
    inspector = inspect(target)
    with target.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                column_type = column.type.compile(target.dialect)
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                clause = f" DEFAULT {_sql_literal(default)}" if default is not None else ""
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{clause}'))


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def get_setting(session: Session, key: str) -> str:
    row = session.get(models.AppSetting, key)
    return row.value if row else ""


def set_setting(session: Session, key: str, value: str) -> None:
    session.merge(models.AppSetting(key=key, value=value))
    session.commit()

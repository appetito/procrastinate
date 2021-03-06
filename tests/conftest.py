import contextlib
import functools
import os
import signal as stdlib_signal

import aiopg
import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from procrastinate import aiopg_connector
from procrastinate import app as app_module
from procrastinate import jobs, schema, testing

# Just ensuring the tests are not polluted by environment
for key in os.environ:
    if key.startswith("PROCRASTINATE_"):
        os.environ.pop(key)


def cursor_execute(cursor, query, *identifiers, format=True):
    if identifiers:
        query = sql.SQL(query).format(
            *(sql.Identifier(identifier) for identifier in identifiers)
        )
    cursor.execute(query)


@contextlib.contextmanager
def db_executor(dbname):
    with contextlib.closing(psycopg2.connect("", dbname=dbname)) as connection:
        connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with connection.cursor() as cursor:
            yield functools.partial(cursor_execute, cursor)


@pytest.fixture
def db_execute():
    return db_executor


def db_create(dbname, template=None):
    with db_executor("postgres") as execute:
        execute("DROP DATABASE IF EXISTS {}", dbname)
        if template:
            execute("CREATE DATABASE {} TEMPLATE {}", dbname, template)
        else:
            execute("CREATE DATABASE {}", dbname)


def db_drop(dbname):
    with db_executor("postgres") as execute:
        execute("DROP DATABASE IF EXISTS {}", dbname)


@pytest.fixture
def db_factory():
    dbs_to_drop = []

    def _(dbname, template=None):
        db_create(dbname=dbname, template=template)
        dbs_to_drop.append(dbname)

    yield _

    for dbname in dbs_to_drop:
        db_drop(dbname=dbname)


@pytest.fixture(scope="session")
def setup_db():

    dbname = "procrastinate_test_template"
    db_create(dbname=dbname)

    connector = aiopg_connector.AiopgConnector(dbname=dbname)
    schema_manager = schema.SchemaManager(connector=connector)
    schema_manager.apply_schema()
    # We need to close the psycopg2 underlying connection synchronously
    connector.close()

    yield dbname

    db_drop(dbname=dbname)


@pytest.fixture
def connection_params(setup_db, db_factory):
    db_factory(dbname="procrastinate_test", template=setup_db)

    yield {"dsn": "", "dbname": "procrastinate_test"}


@pytest.fixture
async def connection(connection_params):
    async with aiopg.connect(**connection_params) as connection:
        yield connection


@pytest.fixture
async def pg_connector(connection_params):
    connector = aiopg_connector.AiopgConnector(**connection_params)
    yield connector
    await connector.close_async()


@pytest.fixture
def kill_own_pid():
    def f(signal=stdlib_signal.SIGTERM):
        os.kill(os.getpid(), signal)

    return f


@pytest.fixture
def connector():
    return testing.InMemoryConnector()


@pytest.fixture
def app(connector):
    return app_module.App(connector=connector)


@pytest.fixture
def job_store(app):
    return app.job_store


@pytest.fixture
def job_factory():
    defaults = {
        "id": 42,
        "task_name": "bla",
        "task_kwargs": {},
        "lock": None,
        "queueing_lock": None,
        "queue": "queue",
    }

    def factory(**kwargs):
        final_kwargs = defaults.copy()
        final_kwargs.update(kwargs)
        return jobs.Job(**final_kwargs)

    return factory

import gc
import pytest
import weakref

import psycopg3

pytestmark = pytest.mark.asyncio


async def test_close(aconn):
    cur = aconn.cursor()
    assert not cur.closed
    await cur.close()
    assert cur.closed

    with pytest.raises(psycopg3.InterfaceError):
        await cur.execute("select 'foo'")

    await cur.close()
    assert cur.closed


async def test_context(aconn):
    async with aconn.cursor() as cur:
        assert not cur.closed

    assert cur.closed


async def test_weakref(aconn):
    cur = aconn.cursor()
    w = weakref.ref(cur)
    await cur.close()
    del cur
    gc.collect()
    assert w() is None


async def test_status(aconn):
    cur = aconn.cursor()
    assert cur.status is None
    await cur.execute("reset all")
    assert cur.status == cur.ExecStatus.COMMAND_OK
    await cur.execute("select 1")
    assert cur.status == cur.ExecStatus.TUPLES_OK
    await cur.close()
    assert cur.status is None


async def test_execute_many_results(aconn):
    cur = aconn.cursor()
    assert cur.nextset() is None

    rv = await cur.execute("select 'foo'; select generate_series(1,3)")
    assert rv is cur
    assert (await cur.fetchall()) == [("foo",)]
    assert cur.rowcount == 1
    assert cur.nextset()
    assert (await cur.fetchall()) == [(1,), (2,), (3,)]
    assert cur.rowcount == 3
    assert cur.nextset() is None

    await cur.close()
    assert cur.nextset() is None


async def test_execute_sequence(aconn):
    cur = aconn.cursor()
    cast = "::text" if aconn.pgconn.server_version < 100000 else ""
    rv = await cur.execute(f"select %s, %s, %s{cast}", [1, "foo", None])
    assert rv is cur
    assert len(cur._results) == 1
    assert cur.pgresult.get_value(0, 0) == b"1"
    assert cur.pgresult.get_value(0, 1) == b"foo"
    assert cur.pgresult.get_value(0, 2) is None
    assert cur.nextset() is None


@pytest.mark.parametrize("query", ["", " ", ";"])
async def test_execute_empty_query(aconn, query):
    cur = aconn.cursor()
    await cur.execute(query)
    assert cur.status == cur.ExecStatus.EMPTY_QUERY
    with pytest.raises(psycopg3.ProgrammingError):
        await cur.fetchone()


async def test_fetchone(aconn):
    cur = aconn.cursor()
    cast = "::text" if aconn.pgconn.server_version < 100000 else ""
    await cur.execute(f"select %s, %s, %s{cast}", [1, "foo", None])
    assert cur.pgresult.fformat(0) == 0

    row = await cur.fetchone()
    assert row[0] == 1
    assert row[1] == "foo"
    assert row[2] is None
    row = await cur.fetchone()
    assert row is None


async def test_execute_binary_result(aconn):
    cur = aconn.cursor(format=psycopg3.pq.Format.BINARY)
    cast = "::text" if aconn.pgconn.server_version < 100000 else ""
    await cur.execute(f"select %s, %s{cast}", ["foo", None])
    assert cur.pgresult.fformat(0) == 1

    row = await cur.fetchone()
    assert row[0] == "foo"
    assert row[1] is None
    row = await cur.fetchone()
    assert row is None


@pytest.mark.parametrize("encoding", ["utf8", "latin9"])
async def test_query_encode(aconn, encoding):
    await aconn.set_client_encoding(encoding)
    cur = aconn.cursor()
    await cur.execute("select '\u20ac'")
    (res,) = await cur.fetchone()
    assert res == "\u20ac"


async def test_query_badenc(aconn):
    await aconn.set_client_encoding("latin1")
    cur = aconn.cursor()
    with pytest.raises(UnicodeEncodeError):
        await cur.execute("select '\u20ac'")


@pytest.fixture(scope="function")
async def execmany(svcconn):
    cur = svcconn.cursor()
    cur.execute(
        """
        drop table if exists execmany;
        create table execmany (id serial primary key, num integer, data text)
        """
    )


async def test_executemany(aconn, execmany):
    cur = aconn.cursor()
    await cur.executemany(
        "insert into execmany(num, data) values (%s, %s)",
        [(10, "hello"), (20, "world")],
    )
    await cur.execute("select num, data from execmany order by 1")
    rv = await cur.fetchall()
    assert rv == [(10, "hello"), (20, "world")]


async def test_executemany_name(aconn, execmany):
    cur = aconn.cursor()
    await cur.executemany(
        "insert into execmany(num, data) values (%(num)s, %(data)s)",
        [{"num": 11, "data": "hello", "x": 1}, {"num": 21, "data": "world"}],
    )
    await cur.execute("select num, data from execmany order by 1")
    rv = await cur.fetchall()
    assert rv == [(11, "hello"), (21, "world")]


async def test_executemany_rowcount(aconn, execmany):
    cur = aconn.cursor()
    await cur.executemany(
        "insert into execmany(num, data) values (%s, %s)",
        [(10, "hello"), (20, "world")],
    )
    assert cur.rowcount == 2


@pytest.mark.parametrize(
    "query",
    [
        "insert into nosuchtable values (%s, %s)",
        "copy (select %s, %s) to stdout",
        "wat (%s, %s)",
    ],
)
async def test_executemany_badquery(aconn, query):
    cur = aconn.cursor()
    with pytest.raises(psycopg3.DatabaseError):
        await cur.executemany(query, [(10, "hello"), (20, "world")])


async def test_rowcount(aconn):
    cur = aconn.cursor()

    await cur.execute("select 1 from generate_series(1, 42)")
    assert cur.rowcount == 42

    await cur.execute(
        "create table test_rowcount_notuples (id int primary key)"
    )
    assert cur.rowcount == -1

    await cur.execute(
        "insert into test_rowcount_notuples select generate_series(1, 42)"
    )
    assert cur.rowcount == 42

    await cur.close()
    assert cur.rowcount == -1


async def test_iter(aconn):
    cur = aconn.cursor()
    await cur.execute("select generate_series(1, 3)")
    res = []
    async for rec in cur:
        res.append(rec)
    assert res == [(1,), (2,), (3,)]


async def test_iter_stop(aconn):
    cur = aconn.cursor()
    await cur.execute("select generate_series(1, 3)")
    async for rec in cur:
        assert rec == (1,)
        break

    async for rec in cur:
        assert rec == (2,)
        break

    assert (await cur.fetchone()) == (3,)
    async for rec in cur:
        assert False

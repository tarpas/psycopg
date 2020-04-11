"""
psycopg3 cursor objects
"""

# Copyright (C) 2020 The Psycopg Team

import codecs
from operator import attrgetter
from typing import Any, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

from . import errors as e
from .pq import ConnStatus, ExecStatus, PGresult, Format
from .utils.queries import query2pg, reorder_params
from .utils.typing import Query, Params

if TYPE_CHECKING:
    from .connection import BaseConnection, Connection, AsyncConnection
    from .connection import QueryGen
    from .adapt import DumpersMap, LoadersMap


class Column(Sequence[Any]):
    def __init__(
        self, pgresult: PGresult, index: int, codec: codecs.CodecInfo
    ):
        self._pgresult = pgresult
        self._index = index
        self._codec = codec

    _attrs = tuple(
        map(
            attrgetter,
            """
            name type_code display_size internal_size precision scale null_ok
            """.split(),
        )
    )

    def __len__(self) -> int:
        return 7

    def __getitem__(self, index: Any) -> Any:
        return self._attrs[index](self)

    @property
    def name(self) -> str:
        rv = self._pgresult.fname(self._index)
        if rv is not None:
            return self._codec.decode(rv)[0]
        else:
            raise e.InterfaceError(
                f"no name available for column {self._index}"
            )

    @property
    def type_code(self) -> int:
        return self._pgresult.ftype(self._index)


class BaseCursor:
    def __init__(self, conn: "BaseConnection", binary: bool = False):
        self.conn = conn
        self.binary = binary
        self.dumpers: DumpersMap = {}
        self.loaders: LoadersMap = {}
        self._reset()
        self.arraysize = 1

    def _reset(self) -> None:
        from .adapt import Transformer

        self._transformer = Transformer(self)
        self._results: List[PGresult] = []
        self.pgresult: Optional[PGresult] = None
        self._pos = 0
        self._iresult = 0

    @property
    def pgresult(self) -> Optional[PGresult]:
        return self._pgresult

    @pgresult.setter
    def pgresult(self, result: Optional[PGresult]) -> None:
        self._pgresult = result
        if result is not None and self._transformer is not None:
            self._transformer.set_row_types(
                (result.ftype(i), result.fformat(i))
                for i in range(result.nfields)
            )

    @property
    def description(self) -> Optional[List[Column]]:
        res = self.pgresult
        if res is None or res.status != ExecStatus.TUPLES_OK:
            return None
        return [Column(res, i, self.conn.codec) for i in range(res.nfields)]

    @property
    def rowcount(self) -> int:
        res = self.pgresult
        if res is None or res.status != ExecStatus.TUPLES_OK:
            return -1
        else:
            return res.ntuples

    def setinputsizes(self, sizes: Sequence[Any]) -> None:
        # no-op
        pass

    def setoutputsize(self, size: Any, column: Optional[int] = None) -> None:
        # no-op
        pass

    def _execute_send(
        self, query: Query, vars: Optional[Params]
    ) -> "QueryGen":
        # Implement part of execute() before waiting common to sync and async
        if self.conn.pgconn.status != ConnStatus.OK:
            if self.conn.pgconn.status == ConnStatus.BAD:
                raise e.InterfaceError(
                    "cannot execute operations: the connection is closed"
                )
            else:
                raise e.InterfaceError(
                    f"cannot execute operations: the connection is"
                    f" in status {self.conn.pgconn.status}"
                )

        self._reset()

        codec = self.conn.codec

        if isinstance(query, str):
            query = codec.encode(query)[0]

        # process %% -> % only if there are paramters, even if empty list
        if vars is not None:
            query, formats, order = query2pg(query, vars, codec)
        if vars:
            if order is not None:
                assert isinstance(vars, Mapping)
                vars = reorder_params(vars, order)
            assert isinstance(vars, Sequence)
            params, types = self._transformer.dump_sequence(vars, formats)
            self.conn.pgconn.send_query_params(
                query,
                params,
                param_formats=formats,
                param_types=types,
                result_format=Format(self.binary),
            )
        else:
            # if we don't have to, let's use exec_ as it can run more than
            # one query in one go
            if self.binary:
                self.conn.pgconn.send_query_params(
                    query, (), result_format=Format(self.binary)
                )
            else:
                self.conn.pgconn.send_query(query)

        return self.conn._exec_gen(self.conn.pgconn)

    def _execute_results(self, results: List[PGresult]) -> None:
        # Implement part of execute() after waiting common to sync and async
        if not results:
            raise e.InternalError("got no result from the query")

        badstats = {res.status for res in results} - {
            ExecStatus.TUPLES_OK,
            ExecStatus.COMMAND_OK,
            ExecStatus.EMPTY_QUERY,
        }
        if not badstats:
            self._results = results
            self.pgresult = results[0]
            return

        if results[-1].status == ExecStatus.FATAL_ERROR:
            raise e.error_from_result(results[-1])

        elif badstats & {
            ExecStatus.COPY_IN,
            ExecStatus.COPY_OUT,
            ExecStatus.COPY_BOTH,
        }:
            raise e.ProgrammingError(
                "COPY cannot be used with execute(); use copy() insead"
            )
        else:
            raise e.InternalError(
                f"got unexpected status from query:"
                f" {', '.join(sorted(s.name for s in sorted(badstats)))}"
            )

    def nextset(self) -> Optional[bool]:
        self._iresult += 1
        if self._iresult < len(self._results):
            self.pgresult = self._results[self._iresult]
            self._pos = 0
            return True
        else:
            return None

    def _load_row(self, n: int) -> Optional[Tuple[Any, ...]]:
        res = self.pgresult
        if res is None:
            raise e.ProgrammingError("no result available")
        elif res.status != ExecStatus.TUPLES_OK:
            raise e.ProgrammingError(
                "the last operation didn't produce a result"
            )

        if n >= res.ntuples:
            return None

        return tuple(
            self._transformer.load_sequence(
                res.get_value(n, i) for i in range(res.nfields)
            )
        )


class Cursor(BaseCursor):
    conn: "Connection"

    def __init__(self, conn: "Connection", binary: bool = False):
        super().__init__(conn, binary)

    def execute(self, query: Query, vars: Optional[Params] = None) -> "Cursor":
        with self.conn.lock:
            gen = self._execute_send(query, vars)
            results = self.conn.wait(gen)
            self._execute_results(results)
        return self

    def executemany(
        self, query: Query, vars_seq: Sequence[Params]
    ) -> "Cursor":
        with self.conn.lock:
            # TODO: trivial implementation; use prepare
            for vars in vars_seq:
                gen = self._execute_send(query, vars)
                results = self.conn.wait(gen)
                self._execute_results(results)
        return self

    def fetchone(self) -> Optional[Sequence[Any]]:
        rv = self._load_row(self._pos)
        if rv is not None:
            self._pos += 1
        return rv

    def fetchmany(self, size: Optional[int] = None) -> List[Sequence[Any]]:
        if size is None:
            size = self.arraysize

        rv: List[Sequence[Any]] = []
        while len(rv) < size:
            row = self._load_row(self._pos)
            if row is None:
                break
            self._pos += 1
            rv.append(row)

        return rv

    def fetchall(self) -> List[Sequence[Any]]:
        rv: List[Sequence[Any]] = []
        while 1:
            row = self._load_row(self._pos)
            if row is None:
                break
            self._pos += 1
            rv.append(row)

        return rv


class AsyncCursor(BaseCursor):
    conn: "AsyncConnection"

    def __init__(self, conn: "AsyncConnection", binary: bool = False):
        super().__init__(conn, binary)

    async def execute(
        self, query: Query, vars: Optional[Params] = None
    ) -> "AsyncCursor":
        async with self.conn.lock:
            gen = self._execute_send(query, vars)
            results = await self.conn.wait(gen)
            self._execute_results(results)
        return self

    async def fetchone(self) -> Optional[Sequence[Any]]:
        rv = self._load_row(self._pos)
        if rv is not None:
            self._pos += 1
        return rv


class NamedCursorMixin:
    pass


class NamedCursor(NamedCursorMixin, Cursor):
    pass


class AsyncNamedCursor(NamedCursorMixin, AsyncCursor):
    pass

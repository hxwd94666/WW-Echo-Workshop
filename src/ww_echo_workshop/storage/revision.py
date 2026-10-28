# 用独立零等待只读连接探测库存修订，避免界面轮询反复改写数据库超时参数。
import sqlite3


class RevisionProbe:
    """Owned by the repository mutex; no data cache or optimistic stale value."""

    def __init__(self, database):
        self.database = database
        self._connection = None

    def read(self, transaction=None):
        if transaction is not None:
            timeout = transaction.execute('PRAGMA busy_timeout').fetchone()[0]
            transaction.execute('PRAGMA busy_timeout=0')
            try:
                return self._read(transaction)
            finally:
                transaction.execute(f'PRAGMA busy_timeout={timeout}')
        if self._connection is None:
            self._connection = sqlite3.connect(
                self.database.as_uri() + '?mode=ro',
                uri=True,
                timeout=0,
                check_same_thread=False,
            )
        return self._read(self._connection)

    @staticmethod
    def _read(connection):
        try:
            cursor = connection.execute("SELECT value FROM metadata WHERE key='revision'")
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise ValueError('库存修订记录缺失')
            return row[0]
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', -1) & 255 in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                return None
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

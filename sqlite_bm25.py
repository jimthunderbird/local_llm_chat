"""Disk-backed BM25 retrieval for the chat corpus using SQLite FTS5.

The chat server used to keep a ``rank_bm25`` inverted index in memory for
every loaded corpus.  A learned corpus of tens of thousands of trunks costs
hundreds of megabytes there, and that memory is rebuilt whenever the corpus
or an explicit transcript/article scope changes.

This module stores the same tokenized documents in embedded SQLite FTS5
tables instead.  FTS5 keeps the postings on disk and computes BM25 itself, so
only the (much smaller) vocabulary and document-frequency metadata are held in
Python.  The database lives at::

    .chat/articles_cache/bm25_index_fts.db

One FTS5 table is kept per corpus (the global knowledge base, each explicit
transcript, each opened article, and each book).  Keeping them separate means
BM25's document count and average length stay correct for a scoped question
instead of being diluted by the whole global corpus.

The index is incremental: ``sync`` inserts or refreshes only the trunks that
are new or whose file changed, which lets ``learn_news_article`` persist an
article and its search index at the same time.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence


# FTS5's unicode61 tokenizer is used only to store an already-tokenized
# document.  The chat tokenizer has already lowercased, normalized plurals,
# and canonicalized times, so no stemming or accent folding is wanted here.
BASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS corpora (
        corpus_key TEXT PRIMARY KEY,
        fts_table  TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunk_documents (
        corpus_key TEXT NOT NULL,
        doc_id     INTEGER NOT NULL,
        chunk_key  TEXT NOT NULL,
        mtime_ns   INTEGER NOT NULL,
        size       INTEGER NOT NULL,
        doc_len    INTEGER NOT NULL,
        PRIMARY KEY (corpus_key, doc_id),
        UNIQUE (corpus_key, chunk_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS chunk_documents_corpus
    ON chunk_documents(corpus_key)
    """,
)

# Keep batches modest so a large first-time index does not hold a huge
# transaction (and its memory) open while the server is starting.
INSERT_BATCH_SIZE = 400

# Only candidates near the top can ever be selected by the chat retrieval
# code (the adaptive cap is 50 and the default depth is 20).  A single SQL
# LIMIT keeps FTS5 from scoring, sorting, and materializing tens of thousands
# of rows for every function word in a question.
DEFAULT_SCORE_LIMIT = 500


def _quote(token: str) -> str:
    """Quote one token so FTS5 treats it as a literal phrase, not syntax."""
    return '"' + token.replace('"', '""') + '"'


def _match_expression(tokens: Sequence[str], mode: str) -> str:
    """Build an FTS5 MATCH expression from already-tokenized words."""
    unique = list(dict.fromkeys(token for token in tokens if token))
    if not unique:
        return ""
    joiner = " AND " if mode.upper() == "AND" else " OR "
    return joiner.join(_quote(token) for token in unique)


def _weighted_expression(tokens: Sequence[str]) -> str:
    """Build an OR expression that preserves repeated query weights.

    FTS5's ``bm25()`` accumulates a contribution for each occurrence of a
    phrase in the MATCH expression.  Repeating a term (the old code weighted
    question words twice) therefore keeps the same effect as the in-memory
    index.
    """
    return " OR ".join(_quote(token) for token in tokens if token)


def _table_names(corpus_key: str) -> tuple[str, str]:
    suffix = hashlib.sha1(corpus_key.encode("utf-8")).hexdigest()[:16]
    return f"chunk_index_{suffix}", f"chunk_vocab_{suffix}"


class SQLiteBM25:
    """A thread-safe handle on one corpus in the FTS5 BM25 database."""

    def __init__(self, db_path: Path | str, corpus_key: str = "global") -> None:
        self.db_path = Path(db_path)
        self.corpus_key = corpus_key
        self.fts_table, self.vocab_table = _table_names(corpus_key)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        # rowid -> chunk_key, loaded lazily once per corpus.  The hot BM25
        # query can then select rowid only and skip the join against
        # chunk_documents (which measured about 33% overhead).
        self._doc_keys: dict[int, str] | None = None

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        # WAL keeps a reader (a chat question) from blocking an article that
        # is being learned/indexed by another request.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        self._ensure_schema(connection)
        self._conn = connection
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        # This database is a rebuildable cache.  A database created by the
        # first single-table prototype is discarded rather than migrated.
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'chunk_documents'"
        ).fetchone()
        if exists:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(chunk_documents)"
                )
            }
            if "corpus_key" not in columns:
                for name in ("chunk_vocab", "chunk_index", "chunk_documents"):
                    connection.execute(f'DROP TABLE IF EXISTS "{name}"')

        for statement in BASE_SCHEMA:
            connection.execute(statement)
        connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS "{self.fts_table}" USING fts5(
                content,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS "{self.vocab_table}"
            USING fts5vocab('{self.fts_table}', 'row')
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO corpora(corpus_key, fts_table, created_at) "
            "VALUES (?, ?, ?)",
            (self.corpus_key, self.fts_table, time.time()),
        )
        connection.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------- sync
    def sync(
        self,
        entries: Iterable[tuple[str, Sequence[str], int, int]],
    ) -> dict[str, object]:
        """Insert or refresh tokenized documents in this corpus.

        ``entries`` yields ``(chunk_key, tokens, mtime_ns, size)``.  A trunk
        whose key is already indexed is rewritten only when its file changed.
        """
        prepared: dict[str, tuple[list[str], int, int]] = {}
        for key, tokens, mtime_ns, size in entries:
            prepared[key] = (
                [token for token in tokens if token],
                int(mtime_ns),
                int(size),
            )
        if not prepared:
            return {"added": 0, "updated": 0, "unchanged": 0}

        added = updated = unchanged = 0
        with self._lock:
            connection = self._connect()
            existing = {
                row[1]: (int(row[0]), int(row[2]), int(row[3]))
                for row in connection.execute(
                    "SELECT doc_id, chunk_key, mtime_ns, size "
                    "FROM chunk_documents WHERE corpus_key = ?",
                    (self.corpus_key,),
                )
            }
            next_id = max((value[0] for value in existing.values()), default=0) + 1
            pending: list[tuple[int, str, str, int, int]] = []
            for key, (tokens, mtime_ns, size) in prepared.items():
                row = existing.get(key)
                if row is not None:
                    doc_id, old_mtime, old_size = row
                    if old_mtime == mtime_ns and old_size == size:
                        unchanged += 1
                        continue
                    updated += 1
                    pending.append(
                        (doc_id, key, " ".join(tokens), mtime_ns, size)
                    )
                else:
                    added += 1
                    pending.append(
                        (next_id, key, " ".join(tokens), mtime_ns, size)
                    )
                    next_id += 1

            for start in range(0, len(pending), INSERT_BATCH_SIZE):
                batch = pending[start : start + INSERT_BATCH_SIZE]
                for doc_id, key, content, mtime_ns, size in batch:
                    # A changing trunk (re-learned article, regenerated
                    # transcript) replaces its row instead of accumulating a
                    # second copy of the same chunk.
                    connection.execute(
                        f'DELETE FROM "{self.fts_table}" WHERE rowid = ?',
                        (doc_id,),
                    )
                    connection.execute(
                        f'INSERT INTO "{self.fts_table}"(rowid, content) '
                        "VALUES (?, ?)",
                        (doc_id, content),
                    )
                    connection.execute(
                        """
                        INSERT INTO chunk_documents(
                            corpus_key, doc_id, chunk_key,
                            mtime_ns, size, doc_len
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(corpus_key, doc_id) DO UPDATE SET
                            chunk_key = excluded.chunk_key,
                            mtime_ns  = excluded.mtime_ns,
                            size      = excluded.size,
                            doc_len   = excluded.doc_len
                        """,
                        (
                            self.corpus_key,
                            doc_id,
                            key,
                            mtime_ns,
                            size,
                            len(content.split()),
                        ),
                    )
                connection.commit()
            # A sync can change the rowid -> chunk_key mapping.
            self._doc_keys = None
        return {"added": added, "updated": updated, "unchanged": unchanged}

    def prune(self, valid_keys: Iterable[str]) -> int:
        """Delete this corpus's documents whose trunk no longer exists."""
        keys = set(valid_keys)
        removed = 0
        with self._lock:
            connection = self._connect()
            stale = [
                int(row[0])
                for row in connection.execute(
                    "SELECT doc_id, chunk_key FROM chunk_documents "
                    "WHERE corpus_key = ?",
                    (self.corpus_key,),
                )
                if row[1] not in keys
            ]
            for start in range(0, len(stale), INSERT_BATCH_SIZE):
                batch = stale[start : start + INSERT_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    f'DELETE FROM "{self.fts_table}" '
                    f"WHERE rowid IN ({placeholders})",
                    batch,
                )
                connection.execute(
                    "DELETE FROM chunk_documents "
                    f"WHERE corpus_key = ? AND doc_id IN ({placeholders})",
                    [self.corpus_key, *batch],
                )
                connection.commit()
                removed += len(batch)
            self._doc_keys = None
        return removed

    # -------------------------------------------------------------- optimize
    def optimize(self) -> None:
        """Merge FTS5 b-tree segments so MATCH/BM25 work stays minimal.

        FTS5 keeps several segments after incremental inserts.  ``optimize``
        merges them into one segment.  It is a write operation, so callers
        run it at startup or after a bulk learn, never inside a question.
        """
        with self._lock:
            connection = self._connect()
            connection.execute(
                f'INSERT INTO "{self.fts_table}"("{self.fts_table}") '
                "VALUES('optimize')"
            )
            connection.commit()

    def vacuum(self) -> None:
        """Return free pages to the filesystem after drops/optimize.

        ``optimize`` can leave a few thousand free pages behind, and dropping
        stale scopes does too.  A compact file stays in the OS page cache and
        needs less WAL work.  Like ``optimize`` this is maintenance-only.
        """
        with self._lock:
            connection = self._connect()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            # VACUUM writes the rebuilt pages through the WAL; checkpoint
            # again so an idle server does not leave a database-sized -wal.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ----------------------------------------------------------------- lookup
    def _document_keys(self) -> dict[int, str]:
        """Return the cached ``rowid -> chunk_key`` map for this corpus."""
        if self._doc_keys is None:
            connection = self._connect()
            rows = connection.execute(
                "SELECT doc_id, chunk_key FROM chunk_documents "
                "WHERE corpus_key = ?",
                (self.corpus_key,),
            ).fetchall()
            self._doc_keys = {
                int(doc_id): str(chunk_key) for doc_id, chunk_key in rows
            }
        return self._doc_keys

    def get_scores(
        self,
        query_tokens: Sequence[str],
        chunk_keys: Sequence[str],
        limit: int | None = DEFAULT_SCORE_LIMIT,
    ) -> list[float]:
        """Return BM25 scores aligned with ``chunk_keys`` (higher is better).

        When ``limit`` is set only the best-scoring rows are read from
        SQLite.  The returned list is still aligned with ``chunk_keys``;
        keys outside the top-k simply keep a zero score.
        """
        if not query_tokens or not chunk_keys:
            return [0.0] * len(chunk_keys)
        expression = _weighted_expression(query_tokens)
        if not expression:
            return [0.0] * len(chunk_keys)
        with self._lock:
            connection = self._connect()
            doc_keys = self._document_keys()
            sql = (
                f'SELECT rowid, bm25("{self.fts_table}") '
                f'FROM "{self.fts_table}" '
                f'WHERE "{self.fts_table}" MATCH ? '
                f'ORDER BY bm25("{self.fts_table}")'
            )
            params: list[object] = [expression]
            if limit is not None and limit > 0:
                sql += " LIMIT ?"
                params.append(int(limit))
            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return [0.0] * len(chunk_keys)
        # FTS5 returns a negative value for a better match (it sorts
        # ascending), so negate it to match the rest of the retrieval code.
        by_key = {
            doc_keys.get(int(rowid), ""): -float(score)
            for rowid, score in rows
            if score is not None
        }
        return [by_key.get(key, 0.0) for key in chunk_keys]

    def matching_keys(
        self, tokens: Sequence[str], mode: str = "AND"
    ) -> set[str]:
        """Return chunk keys whose text matches all/any of ``tokens``."""
        expression = _match_expression(tokens, mode)
        if not expression:
            return set()
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    f"""
                    SELECT d.chunk_key
                    FROM "{self.fts_table}"
                    JOIN chunk_documents AS d
                      ON d.corpus_key = ? AND d.doc_id = "{self.fts_table}".rowid
                    WHERE "{self.fts_table}" MATCH ?
                    """,
                    (self.corpus_key, expression),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row[0]) for row in rows}

    def count_matching(
        self, tokens: Sequence[str], mode: str = "AND"
    ) -> int:
        """Count chunks in this corpus matching all/any of ``tokens``."""
        expression = _match_expression(tokens, mode)
        if not expression:
            return 0
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    f'SELECT count(*) FROM "{self.fts_table}" '
                    f'WHERE "{self.fts_table}" MATCH ?',
                    (expression,),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0]) if row else 0

    def count_cooccurrence(
        self, token: str, anchors: Sequence[str]
    ) -> int:
        """Count chunks containing ``token`` and at least one anchor."""
        anchor_expression = _match_expression(anchors, "OR")
        if not token or not anchor_expression:
            return 0
        expression = f"{_quote(token)} AND ({anchor_expression})"
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    f'SELECT count(*) FROM "{self.fts_table}" '
                    f'WHERE "{self.fts_table}" MATCH ?',
                    (expression,),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0]) if row else 0

    def vocabulary(self) -> dict[str, int]:
        """Return ``{term: document_frequency}`` from the FTS5 vocabulary."""
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                f'SELECT term, doc FROM "{self.vocab_table}"'
            ).fetchall()
        return {str(term): int(doc) for term, doc in rows}

    def document_count(self) -> int:
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT count(*) FROM chunk_documents WHERE corpus_key = ?",
                (self.corpus_key,),
            ).fetchone()
        return int(row[0]) if row else 0

    def token_count(self) -> int:
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT coalesce(sum(doc_len), 0) FROM chunk_documents "
                "WHERE corpus_key = ?",
                (self.corpus_key,),
            ).fetchone()
        return int(row[0]) if row else 0

    def indexed_keys(self) -> set[str]:
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT chunk_key FROM chunk_documents WHERE corpus_key = ?",
                (self.corpus_key,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def file_signatures(self) -> dict[str, tuple[int, int]]:
        """Return ``{chunk_key: (mtime_ns, size)}`` for indexed documents."""
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT chunk_key, mtime_ns, size FROM chunk_documents "
                "WHERE corpus_key = ?",
                (self.corpus_key,),
            ).fetchall()
        return {
            str(key): (int(mtime_ns), int(size))
            for key, mtime_ns, size in rows
        }


# The chat app uses one database path for every corpus.  Keeping handles
# module-level means concurrent questions and article learning share the same
# SQLite connection and lock for a corpus.
_INDEXES: dict[tuple[str, str], SQLiteBM25] = {}
_INDEXES_LOCK = threading.Lock()


def get_index(db_path: Path | str, corpus_key: str = "global") -> SQLiteBM25:
    resolved = str(Path(db_path).resolve())
    key = (resolved, corpus_key)
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = SQLiteBM25(resolved, corpus_key)
            _INDEXES[key] = index
        return index


def drop_transient_corpora(
    db_path: Path | str,
    keep: Iterable[str] = ("global",),
    vacuum: bool = False,
) -> int:
    """Drop per-scope FTS tables, keeping the persistent global corpus.

    Explicit transcript/article/book corpora are cheap to rebuild and describe
    files that may no longer be open.  Running this once at server start keeps
    the database from growing without bound.  With ``vacuum=True`` the freed
    pages are also returned to the filesystem, which keeps the index small
    enough to stay in the OS page cache.
    """
    path = Path(db_path)
    if not path.is_file():
        return 0
    keep_keys = set(keep)
    resolved = str(path.resolve())
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'corpora'"
        ).fetchone()
        if not exists:
            return 0
        rows = connection.execute(
            "SELECT corpus_key, fts_table FROM corpora"
        ).fetchall()
        dropped = 0
        for corpus_key, fts_table in rows:
            if corpus_key in keep_keys:
                continue
            suffix = hashlib.sha1(str(corpus_key).encode("utf-8")).hexdigest()[:16]
            vocab_table = f"chunk_vocab_{suffix}"
            connection.execute(f'DROP TABLE IF EXISTS "{vocab_table}"')
            connection.execute(f'DROP TABLE IF EXISTS "{fts_table}"')
            connection.execute(
                "DELETE FROM chunk_documents WHERE corpus_key = ?",
                (corpus_key,),
            )
            connection.execute(
                "DELETE FROM corpora WHERE corpus_key = ?", (corpus_key,)
            )
            dropped += 1
        connection.commit()
        # Close handles for corpora that no longer exist so a later get_index
        # starts from a clean connection instead of a dropped table, and so
        # they cannot hold a read lock while VACUUM asks for an exclusive one.
        with _INDEXES_LOCK:
            for key, index in list(_INDEXES.items()):
                if key[0] == resolved and key[1] not in keep_keys:
                    index.close()
                    del _INDEXES[key]
        free_pages = int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        )
        if vacuum and (dropped or free_pages):
            # Release the dropped scope tables' pages.  VACUUM needs free
            # temporary space roughly equal to the database size, so it runs
            # only during startup/maintenance, never on a question.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return dropped
    finally:
        connection.close()

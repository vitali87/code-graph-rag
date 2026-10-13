# The `sqlite` vector store: a sqlite-vec vec0 table beside a plain one.
#
# vec0 keys a vector by an integer rowid alone, while a point is keyed by its
# symbol (vector_store.embedding_point_id), as Qdrant's are since issue #2447.
# The plain table holds that key and the payload the other backends keep
# beside each vector, and its integer primary key is the vector's rowid.

SQLITE_VEC_DEFAULT_DB_FILENAME = "vectors.sqlite"
# A writer waits this long for another process's write before failing, as
# the embedded graph's writers do.
SQLITE_VEC_BUSY_TIMEOUT_MS = 60_000
# vec0 refuses a KNN query for more neighbours than this
# (SQLITE_VEC_VEC0_K_MAX in sqlite-vec).
SQLITE_VEC_MAX_K = 4096
SQLITE_VEC_META_DIMENSION = "dimension"

SQLITE_VEC_PRAGMA_BUSY = "PRAGMA busy_timeout={ms}"
SQLITE_VEC_PRAGMA_WAL = "PRAGMA journal_mode=WAL"
# IMMEDIATE takes the write lock up front, so two processes writing at once
# queue on the busy timeout instead of one failing to upgrade a read lock.
SQLITE_VEC_BEGIN_WRITE = "BEGIN IMMEDIATE"

SQLITE_VEC_CREATE_META = (
    "CREATE TABLE IF NOT EXISTS cgr_vector_meta(name TEXT PRIMARY KEY, "
    "value TEXT NOT NULL)"
)
SQLITE_VEC_SELECT_META = "SELECT value FROM cgr_vector_meta WHERE name = ?"
SQLITE_VEC_UPSERT_META = (
    "INSERT INTO cgr_vector_meta(name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value"
)
SQLITE_VEC_DELETE_META = "DELETE FROM cgr_vector_meta WHERE name = ?"

SQLITE_VEC_CREATE_POINTS = """
CREATE TABLE cgr_vector_points(
    id INTEGER PRIMARY KEY,
    point_id TEXT NOT NULL UNIQUE,
    node_id INTEGER NOT NULL,
    qualified_name TEXT NOT NULL,
    project TEXT NOT NULL
)
"""
SQLITE_VEC_CREATE_POINTS_PROJECT_INDEX = (
    "CREATE INDEX cgr_vector_points_project ON cgr_vector_points(project)"
)
SQLITE_VEC_CREATE_VECTORS = (
    "CREATE VIRTUAL TABLE cgr_vectors USING "
    "vec0(embedding float[{dim}] distance_metric=cosine)"
)
SQLITE_VEC_DROP_POINTS = "DROP TABLE IF EXISTS cgr_vector_points"
SQLITE_VEC_DROP_VECTORS = "DROP TABLE IF EXISTS cgr_vectors"

SQLITE_VEC_UPSERT_POINT = (
    "INSERT INTO cgr_vector_points(point_id, node_id, qualified_name, project) "
    "VALUES (?, ?, ?, ?) ON CONFLICT(point_id) DO UPDATE SET "
    "node_id = excluded.node_id, qualified_name = excluded.qualified_name, "
    "project = excluded.project"
)
SQLITE_VEC_POINT_ROWID = "SELECT id FROM cgr_vector_points WHERE point_id = ?"
# vec0 has no upsert (INSERT OR REPLACE fails on its primary key), so a
# point's vector is deleted and written again.
SQLITE_VEC_DELETE_VECTOR = "DELETE FROM cgr_vectors WHERE rowid = ?"
SQLITE_VEC_INSERT_VECTOR = "INSERT INTO cgr_vectors(rowid, embedding) VALUES (?, ?)"

# The KNN query answers cosine distance, 1 - cosine similarity, nearest
# first. No WHERE on the distance here: SQLite pushes it into the CTE, and
# vec0 refuses any constraint on distance in a KNN query.
SQLITE_VEC_SEARCH = """
WITH knn AS (
    SELECT rowid, distance FROM cgr_vectors WHERE embedding MATCH ? AND k = ?
)
SELECT p.node_id, knn.distance, p.qualified_name
FROM knn JOIN cgr_vector_points AS p ON p.id = knn.rowid
ORDER BY knn.distance
"""
SQLITE_VEC_PROJECT_POINTS = (
    "SELECT point_id, node_id FROM cgr_vector_points WHERE project = ?"
)
# Point ids travel as one JSON array, so a batch of any size is one bound
# parameter, never past SQLite's limit on them.
SQLITE_VEC_POINTS_BY_ID = (
    "SELECT point_id, node_id FROM cgr_vector_points "
    "WHERE point_id IN (SELECT value FROM json_each(?))"
)
SQLITE_VEC_DELETE_VECTORS_BY_POINT = (
    "DELETE FROM cgr_vectors WHERE rowid IN (SELECT id FROM cgr_vector_points "
    "WHERE point_id IN (SELECT value FROM json_each(?)))"
)
SQLITE_VEC_DELETE_POINTS = (
    "DELETE FROM cgr_vector_points WHERE point_id IN (SELECT value FROM json_each(?))"
)
# The sqlite3 method a Python built without extension loading lacks.
SQLITE_LOAD_EXTENSION_METHOD = "enable_load_extension"

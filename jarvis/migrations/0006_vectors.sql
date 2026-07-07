-- Move vectors OUT of LanceDB into SQLite.
--
-- Why: LanceDB's native (Rust/Arrow) kernels require AVX, which pre-2011
-- CPUs (e.g. the 2010 Mac Mini's Core 2 Duo) lack — it SIGILLs on first use.
-- Storing normalized embedding vectors as float32 blobs and doing brute-force
-- cosine in pure Python runs on ANY CPU and, at personal scale (thousands of
-- chunks), is plenty fast. It also drops the heavy pyarrow/lancedb deps,
-- saving RAM on the 6 GB hub.
--
-- Vectors are just numbers (topic-leaky at worst); the CONTENT stays
-- Fernet-encrypted in memory_docs / memory_chunks.

CREATE TABLE memory_vectors (
    chunk_id TEXT PRIMARY KEY REFERENCES memory_chunks(id) ON DELETE CASCADE,
    doc_id   INTEGER NOT NULL,
    ts       REAL NOT NULL,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL          -- normalized float32 array (cosine == dot)
);
CREATE INDEX idx_memory_vectors_doc ON memory_vectors (doc_id);

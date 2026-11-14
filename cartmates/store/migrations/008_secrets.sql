-- Encrypted per-household secrets (Fernet), e.g. the Swiggy token.
CREATE TABLE secrets (
    name       TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

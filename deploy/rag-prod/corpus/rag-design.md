# RAG Retrieval Design

This trial combines deterministic hashing embeddings with a vector index.
The goal is a usable retrieval loop, not production-grade retrieval quality.

## Embedding Model

The local model uses tokenization plus hash projection: Latin words are
lowercased, Chinese text yields single characters and bigrams. Each token is
projected into a fixed-dimensional vector space through a cryptographic hash;
term frequency is log-weighted and the whole vector is L2 normalized. The same
text always yields the same vector and can be replayed offline.

The model digest is computed over the canonical serialization of the model
specification. Any change to the specification or its parameters changes the
digest and thereby invalidates the existing index.

## Chunking Strategy

Chunking splits on blank lines, merges small paragraphs toward a target
length, and re-splits oversized paragraphs. Every chunk records its start and
end line numbers and character offsets, so retrieval results can point back
to the exact source position.

## Index Versioning

Index rows carry a version number. On re-ingestion the previous version rows
are retained with a retention window of the two most recent versions. Queries
only match the currently active version, so switching versions realizes both
index invalidation and rollback drills.

## Citation Contract

Every retrieval hit must carry a complete citation: repository, branch, file
path, start and end line, document digest, chunk digest, model digest, and
index version. Rows missing citations are dropped and counted at query time;
they are never returned as results.

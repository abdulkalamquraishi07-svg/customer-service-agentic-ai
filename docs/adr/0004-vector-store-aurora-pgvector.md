# ADR 0004: Vector store on Aurora PostgreSQL Serverless v2 (pgvector), not OpenSearch

Status: Accepted

## Context
The Knowledge Agent needs a Bedrock Knowledge Base over policy documents in S3. The default
vector store, OpenSearch Serverless, bills a minimum capacity even when idle, which dominates
cost for a small corpus with bursty traffic.

## Options
- OpenSearch Serverless: managed by Bedrock quick-create; high idle floor cost.
- RDS PostgreSQL + pgvector: cheapest fixed instance, but Bedrock KB cannot use it (KB requires
  the RDS Data API, which only Aurora provides), so we would own chunking, embedding and sync.
- Aurora PostgreSQL Serverless v2 + pgvector: supported by Bedrock KB through the Data API;
  can scale to 0 ACU (auto-pause) when idle.

## Decision
Aurora PostgreSQL 16 Serverless v2 with pgvector, behind a Bedrock Knowledge Base.
- dev/staging: min 0 ACU with auto-pause (600 s / 1800 s). prod: min 0.5 ACU (no cold starts).
- Isolated subnets, no inbound security group rules; Bedrock and the setup Lambda use the Data
  API only. Storage encrypted with the platform KMS key; deletion protection + snapshot on
  delete outside dev.
- Bedrock uses a dedicated `bedrock_user` (generated alphanumeric password in Secrets Manager),
  never the master user. Schema (extension, table, HNSW + GIN indexes) is created by an
  idempotent custom resource.
- Titan Text Embeddings v2, 1024 dimensions; fixed-size chunks of 300 tokens, 15% overlap.
- KB role trust is scoped with aws:SourceAccount and aws:SourceArn (confused-deputy safe).

## Finding after deployment: score semantics
With this Aurora store, `Retrieve` returned the correct top-k passages but reported cosine
DISTANCE as `score` (lower = better; values above 1 observed) and listed them largest first.
Verified by recomputing distances directly in pgvector with the same Titan v2 query embedding.
Treating `score` as similarity would have trusted the worst match and accepted off-topic queries.
- The agent normalizes scores explicitly (`KB_SCORE_MODE=distance`, similarity = 1 - distance),
  re-sorts, and forces `overrideSearchType=SEMANTIC` (hybrid scores are rank-fused, ~0.5 for all).
- Cutoff `KB_MIN_SIMILARITY=0.15`, calibrated on labeled queries (on-topic best >= 0.114,
  off-topic best <= 0.093), chosen on the safe side: a miss leads to escalation, a false match
  would put irrelevant policy in front of the model.
- `evals/kb_canary.py` (15 labeled cases) runs against the live KB and fails if rankings invert,
  so a change in Bedrock's score semantics is caught rather than silently degrading answers.

## Consequences
- First query after an auto-pause waits for the cluster to resume (seconds). The setup Lambda
  and the knowledge tool retry only on "resuming" errors; anything else fails honestly.
- Policy documents must match the rules engine; a test asserts the return windows, approval
  threshold and approval expiry in the documents equal the backend configuration.
- Ingestion is a separate step after document changes (scripts/sync_knowledge.py).

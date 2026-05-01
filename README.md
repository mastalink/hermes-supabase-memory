# hermes-supabase-memory

Supabase-backed memory provider for [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.12+.

Drops MOSES's pgvector "OB1 brain" memory layer into Hermes as a `MemoryProvider` plugin. Cross-session recall, semantic search, per-agent identity scoping, audit log.

## Status

**Pre-alpha.** Built for Joseph's TurtleNet fleet migration (Leo / Raphael / Donatello / Michelangelo + Moses alter-ego). Each turtle gets its own `agent_identity` so memories scope per-soul while sharing one Supabase project.

## Install

Drop `pgvector_memory/` into `$HERMES_HOME/plugins/`:

```bash
git clone https://github.com/<your-org>/hermes-supabase-memory
cp -r hermes-supabase-memory/pgvector_memory $HERMES_HOME/plugins/pgvector_memory
```

Then in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: pgvector_memory
```

> Plugin directory is `pgvector_memory/` (not `supabase/`) to avoid shadowing
> the PyPI `supabase` Python client at import time.

Set environment:

```bash
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=sb_...
SUPABASE_AGENT_IDENTITY=raphael          # one of: leonardo, raphael, donatello, michelangelo, moses
SUPABASE_EMBEDDING_DIM=384               # must match thoughts table vector dim
```

## Schema requirements

Plugin assumes the MOSES OB1 schema is present in your Supabase project:

- `thoughts` — pgvector HNSW index, 384-dim embeddings, `agent_id` column for per-soul scoping
- `routing_decisions` — router learning (optional, used only if present)
- `memory_audit_log` — write trail (optional)

See `migrations/` for the SQL.

## Tools exposed to the LLM

| Tool | Purpose |
|---|---|
| `brain_search(query, k=5)` | semantic search over scoped thoughts |
| `brain_capture(content, importance=0.5, parent_id=null)` | persist a thought |
| `brain_recall(session_id)` | full thread for a session |
| `brain_stats()` | counts, agent_id summary, recent activity |

## License

MIT.

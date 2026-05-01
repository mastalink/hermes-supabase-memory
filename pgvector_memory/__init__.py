"""Supabase pgvector memory plugin for Hermes Agent v0.12+.

Single-file plugin: drop into ``$HERMES_HOME/plugins/pgvector_memory/`` and
configure ``memory.provider: pgvector_memory``.

The plugin is consolidated into one module so it works around the Hermes
user-plugin loader's missing ``_hermes_user_memory`` parent namespace
registration. (Bundled plugins like Honcho can be multi-file because
``plugins.memory`` is pre-registered; user-installed plugins cannot.)

Persists thoughts to a Supabase pgvector ``thoughts`` table, scoped per
``agent_identity`` so each turtle / agent gets its own soul-scoped memory.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Hermes runtime imports — guarded so the module imports cleanly outside Hermes
# ─────────────────────────────────────────────────────────────────────────────

try:
    from agent.memory_provider import MemoryProvider
    from tools.registry import tool_error
except ImportError:  # pragma: no cover — standalone testing
    class MemoryProvider:  # type: ignore[no-redef]
        pass

    def tool_error(msg: str) -> Dict[str, Any]:  # type: ignore[no-redef]
        return {"error": msg}


logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class SupabaseConfig:
    url: str
    key: str
    agent_identity: str = "hermes"
    embedding_dim: int = 384
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    table_thoughts: str = "thoughts"
    table_audit: str = "memory_audit_log"
    prefetch_k: int = 5
    importance_default: float = 0.5

    def is_valid(self) -> bool:
        return bool(self.url) and bool(self.key)


def load_config(hermes_home: Path | str | None = None) -> SupabaseConfig:
    """Load Supabase config: env vars, then $HERMES_HOME/pgvector_memory.json overrides."""
    cfg = SupabaseConfig(
        url=os.environ.get("SUPABASE_URL", ""),
        key=os.environ.get("SUPABASE_KEY", os.environ.get("SUPABASE_SERVICE_KEY", "")),
        agent_identity=os.environ.get("SUPABASE_AGENT_IDENTITY", "hermes"),
        embedding_dim=int(os.environ.get("SUPABASE_EMBEDDING_DIM", "384")),
        embedding_model=os.environ.get(
            "SUPABASE_EMBEDDING_MODEL",
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
    )
    if hermes_home:
        path = Path(hermes_home) / "pgvector_memory.json"
        if path.exists():
            try:
                file_cfg = json.loads(path.read_text(encoding="utf-8"))
                for k, v in file_cfg.items():
                    if hasattr(cfg, k) and v not in (None, ""):
                        setattr(cfg, k, v)
            except Exception as exc:
                logger.warning("Failed to read %s: %s", path, exc)
    return cfg


# ═════════════════════════════════════════════════════════════════════════════
# Tool schemas
# ═════════════════════════════════════════════════════════════════════════════


BRAIN_SEARCH_SCHEMA = {
    "name": "brain_search",
    "description": (
        "Semantic search over the agent's persistent memory. Returns past "
        "thoughts, observations, and conclusions ranked by relevance to the "
        "query. Use this when the user references prior work, when you need "
        "context from earlier sessions, or when checking what you already "
        "know about a topic before asking. Scoped to this agent's identity."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query."},
            "k": {"type": "integer", "description": "Max results (default 5, max 20)."},
            "min_importance": {
                "type": "number",
                "description": "Filter to thoughts with importance >= this value (0.0-1.0).",
            },
        },
        "required": ["query"],
    },
}

BRAIN_CAPTURE_SCHEMA = {
    "name": "brain_capture",
    "description": (
        "Persist a thought to long-term memory. Use this when you learn "
        "something the user wants remembered, when you reach a non-obvious "
        "conclusion worth keeping, or when the operator says 'remember' / "
        "'save this'. Don't capture trivia or content already on disk — "
        "only insights, decisions, and surprising observations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The thought to persist (1-2 sentences ideal).",
            },
            "importance": {
                "type": "number",
                "description": "Importance 0.0-1.0 (default 0.5). 0.8+ = always recall.",
            },
            "parent_id": {"type": "string", "description": "Thread parent thought ID."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Topical tags (optional).",
            },
        },
        "required": ["content"],
    },
}

BRAIN_RECALL_SCHEMA = {
    "name": "brain_recall",
    "description": (
        "Pull the full thread for a given session_id — every thought "
        "captured during that conversation, ordered. Use when the user "
        "references a past conversation by session or asks 'what did we "
        "decide last time?'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session identifier."},
            "limit": {"type": "integer", "description": "Max thoughts (default 50)."},
        },
        "required": ["session_id"],
    },
}

BRAIN_STATS_SCHEMA = {
    "name": "brain_stats",
    "description": (
        "Summary of this agent's memory: total thoughts, recent activity, "
        "top tags, average importance. Cheap — no LLM cost."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

ALL_SCHEMAS = [BRAIN_SEARCH_SCHEMA, BRAIN_CAPTURE_SCHEMA, BRAIN_RECALL_SCHEMA, BRAIN_STATS_SCHEMA]


# ═════════════════════════════════════════════════════════════════════════════
# Supabase pgvector adapter
# ═════════════════════════════════════════════════════════════════════════════

_EMBEDDER = None
_CLIENT = None


def _get_embedder(model_name: str):
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedder: %s", model_name)
        _EMBEDDER = SentenceTransformer(model_name)
    return _EMBEDDER


def _get_client(url: str, key: str):
    global _CLIENT
    if _CLIENT is None:
        from supabase import create_client

        _CLIENT = create_client(url, key)
    return _CLIENT


def _embed(text: str, model_name: str) -> list[float]:
    vec = _get_embedder(model_name).encode(text, normalize_embeddings=True)
    return vec.tolist() if hasattr(vec, "tolist") else list(vec)


def _adapter_capture(
    *,
    config: SupabaseConfig,
    content: str,
    session_id: str,
    importance: float = 0.5,
    parent_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    source: str = "hermes",
    metadata: Optional[dict] = None,
) -> dict[str, Any]:
    """Insert a thought row.

    Schema (per MOSES OB1 migrations through 2026-04-15):
      id, content, embedding, metadata jsonb, parent_id, version,
      source_session text, importance real, access_count, last_accessed,
      agent_id text, created_at, updated_at.

    There is NO `tags` or `source` column — those go inside `metadata`.
    `session_id` maps to the `source_session` column.
    """
    client = _get_client(config.url, config.key)
    vec = _embed(content, config.embedding_model)
    md: dict[str, Any] = {"source": source}
    if tags:
        md["tags"] = list(tags)
    if metadata:
        md.update(metadata)
    row: dict[str, Any] = {
        "content": content,
        "embedding": vec,
        "importance": float(max(0.0, min(1.0, importance))),
        "agent_id": config.agent_identity,
        "source_session": session_id,
        "metadata": md,
    }
    if parent_id:
        row["parent_id"] = parent_id
    resp = client.table(config.table_thoughts).insert(row).execute()
    if not resp.data:
        raise RuntimeError(f"Insert returned no data: {resp}")
    return resp.data[0]


def _adapter_search(
    *, config: SupabaseConfig, query: str, k: int = 5, min_importance: float = 0.0
) -> list[dict[str, Any]]:
    """Semantic search via match_thoughts RPC.

    RPC signature (per supabase/migrations/20260407000001):
      match_thoughts(query_embedding, match_threshold, match_count,
                     filter_metadata jsonb, filter_agent_id text)
    """
    client = _get_client(config.url, config.key)
    vec = _embed(query, config.embedding_model)
    try:
        resp = client.rpc(
            "match_thoughts",
            {
                "query_embedding": vec,
                "match_threshold": 0.5,
                "match_count": k,
                "filter_metadata": None,
                "filter_agent_id": config.agent_identity,
            },
        ).execute()
        rows = list(resp.data or [])
        if min_importance > 0:
            rows = [r for r in rows if (r.get("importance") or 0.0) >= min_importance]
        return rows
    except Exception as exc:
        logger.debug("match_thoughts RPC failed, falling back to recent rows: %s", exc)

    resp = (
        client.table(config.table_thoughts)
        .select("id,content,importance,source_session,parent_id,metadata,created_at")
        .eq("agent_id", config.agent_identity)
        .gte("importance", float(min_importance))
        .order("created_at", desc=True)
        .limit(k)
        .execute()
    )
    return list(resp.data or [])


def _adapter_thread(
    *, config: SupabaseConfig, session_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    client = _get_client(config.url, config.key)
    resp = (
        client.table(config.table_thoughts)
        .select("id,content,importance,parent_id,metadata,created_at,source_session")
        .eq("agent_id", config.agent_identity)
        .eq("source_session", session_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return list(resp.data or [])


def _adapter_stats(*, config: SupabaseConfig) -> dict[str, Any]:
    """Aggregate stats. `tags` and `source` live inside `metadata` JSONB."""
    client = _get_client(config.url, config.key)
    started = time.monotonic()
    resp = (
        client.table(config.table_thoughts)
        .select("id,importance,metadata,created_at", count="exact")
        .eq("agent_id", config.agent_identity)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    rows = resp.data or []
    total = getattr(resp, "count", len(rows)) or len(rows)

    tag_counts: dict[str, int] = {}
    importance_sum = 0.0
    for r in rows:
        importance_sum += float(r.get("importance") or 0.0)
        md = r.get("metadata") or {}
        for tag in md.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:8]
    avg_imp = importance_sum / max(1, len(rows))

    return {
        "agent_identity": config.agent_identity,
        "total_thoughts": total,
        "recent_window": len(rows),
        "avg_importance_recent": round(avg_imp, 3),
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }


def _adapter_audit(
    *,
    config: SupabaseConfig,
    operation: str,
    thought_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Best-effort audit write. Silently skipped if table absent."""
    try:
        client = _get_client(config.url, config.key)
        client.table(config.table_audit).insert(
            {
                "operation": operation,
                "thought_id": thought_id,
                "agent_id": config.agent_identity,
                "details": details or {},
            }
        ).execute()
    except Exception as exc:
        logger.debug("Audit write failed (table may be absent): %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# MemoryProvider
# ═════════════════════════════════════════════════════════════════════════════


class SupabaseMemoryProvider(MemoryProvider):
    """Pgvector-backed memory provider scoped per ``agent_identity``."""

    def __init__(self) -> None:
        self._config: Optional[SupabaseConfig] = None
        self._session_id: str = ""
        self._initialized: bool = False
        self._cron_skipped: bool = False

        self._prefetch_lock = threading.Lock()
        self._prefetch_cache: str = ""
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_query_pending: Optional[str] = None

        self._captures_this_session: int = 0
        self._searches_this_session: int = 0

    # ─────────────────────────────── ABC: required ────────────────────────

    @property
    def name(self) -> str:
        return "pgvector_memory"

    def is_available(self) -> bool:
        return load_config().is_valid()

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in ("cron", "flush") or platform == "cron":
            logger.debug(
                "pgvector memory skipped: cron/flush context (agent_context=%s, platform=%s)",
                agent_context,
                platform,
            )
            self._cron_skipped = True
            return

        cfg = load_config(hermes_home=kwargs.get("hermes_home"))
        agent_identity = kwargs.get("agent_identity")
        if agent_identity:
            cfg.agent_identity = str(agent_identity)
        if not cfg.is_valid():
            logger.warning("SUPABASE_URL or SUPABASE_KEY not set; provider disabled")
            return

        self._config = cfg
        self._session_id = session_id
        try:
            stats = _adapter_stats(config=cfg)
            logger.info(
                "pgvector memory initialized: agent=%s total=%s avg_importance=%s latency=%sms",
                stats["agent_identity"],
                stats["total_thoughts"],
                stats["avg_importance_recent"],
                stats["latency_ms"],
            )
            self._initialized = True
        except Exception as exc:
            logger.exception("pgvector memory init failed: %s", exc)
            self._config = None

    def system_prompt_block(self) -> str:
        if not self._initialized or not self._config:
            return ""
        return (
            f"## Memory\n"
            f"Persistent memory is active (Supabase pgvector, identity={self._config.agent_identity}). "
            f"Use `brain_search` before answering when prior context might exist. "
            f"Use `brain_capture` to save genuine insights — not trivia.\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._initialized or not self._config or not query.strip():
            return ""
        with self._prefetch_lock:
            if self._prefetch_cache and self._prefetch_query_pending == query:
                cached = self._prefetch_cache
                self._prefetch_cache = ""
                self._prefetch_query_pending = None
                return cached
        try:
            rows = _adapter_search(config=self._config, query=query, k=self._config.prefetch_k)
        except Exception as exc:
            logger.debug("prefetch search failed: %s", exc)
            return ""
        return self._format_recall(rows)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._initialized or not self._config or not query.strip():
            return

        def _run(q: str) -> None:
            try:
                rows = _adapter_search(config=self._config, query=q, k=self._config.prefetch_k)
                formatted = self._format_recall(rows)
                with self._prefetch_lock:
                    self._prefetch_cache = formatted
                    self._prefetch_query_pending = q
            except Exception as exc:
                logger.debug("background prefetch failed: %s", exc)

        self._prefetch_thread = threading.Thread(
            target=_run, args=(query,), daemon=True, name="pgvector-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        if not self._initialized or not self._config:
            return
        sid = session_id or self._session_id
        try:
            user_row = _adapter_capture(
                config=self._config,
                content=user_content,
                session_id=sid,
                importance=0.4,
                source="hermes:user",
            )
            _adapter_capture(
                config=self._config,
                content=assistant_content,
                session_id=sid,
                importance=0.5,
                parent_id=user_row.get("id"),
                source="hermes:assistant",
            )
            self._captures_this_session += 2
        except Exception as exc:
            logger.warning("sync_turn capture failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_SCHEMAS if self._initialized else []

    def handle_tool_call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self._initialized or not self._config:
            return tool_error("pgvector memory provider is not initialized")
        try:
            if name == "brain_search":
                return self._tool_search(args)
            if name == "brain_capture":
                return self._tool_capture(args)
            if name == "brain_recall":
                return self._tool_recall(args)
            if name == "brain_stats":
                return _adapter_stats(config=self._config)
            return tool_error(f"Unknown tool: {name}")
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return tool_error(f"{name} failed: {exc}")

    def shutdown(self) -> None:
        if self._initialized:
            logger.info(
                "pgvector memory shutdown: captures=%s searches=%s",
                self._captures_this_session,
                self._searches_this_session,
            )
        self._initialized = False
        self._config = None

    # ─────────────────────────────── Optional hooks ──────────────────────

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if not self._initialized or not self._config or not messages:
            return ""
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if not last_user:
            return ""
        try:
            _adapter_capture(
                config=self._config,
                content=f"Pre-compression checkpoint: {last_user[:500]}",
                session_id=self._session_id,
                importance=0.7,
                source="hermes:pre_compress",
            )
        except Exception as exc:
            logger.debug("on_pre_compress capture failed: %s", exc)
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._initialized or not self._config:
            return
        _adapter_audit(
            config=self._config,
            operation=f"memory_write:{action}",
            details={"target": target, "preview": content[:200], **(metadata or {})},
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._initialized or not self._config:
            return
        _adapter_audit(
            config=self._config,
            operation="session_end",
            details={
                "session_id": self._session_id,
                "turns": len(messages),
                "captures": self._captures_this_session,
            },
        )

    # ─────────────────────────────── Tool handlers ───────────────────────

    def _tool_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        k = min(int(args.get("k") or 5), 20)
        min_importance = float(args.get("min_importance") or 0.0)
        rows = _adapter_search(
            config=self._config, query=query, k=k, min_importance=min_importance
        )
        self._searches_this_session += 1
        return {
            "results": [
                {
                    "id": r.get("id"),
                    "content": r.get("content"),
                    "importance": r.get("importance"),
                    "session_id": r.get("source_session"),
                    "tags": (r.get("metadata") or {}).get("tags") or [],
                    "similarity": r.get("similarity"),
                    "created_at": str(r.get("created_at", "")),
                }
                for r in rows
            ],
            "count": len(rows),
        }

    def _tool_capture(self, args: Dict[str, Any]) -> Dict[str, Any]:
        content = (args.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        importance = float(args.get("importance") or self._config.importance_default)
        parent_id = args.get("parent_id") or None
        tags = args.get("tags") or []
        row = _adapter_capture(
            config=self._config,
            content=content,
            session_id=self._session_id,
            importance=importance,
            parent_id=parent_id,
            tags=tags,
            source="hermes:tool",
        )
        _adapter_audit(
            config=self._config,
            operation="brain_capture",
            thought_id=row.get("id"),
            details={"importance": importance, "tags": tags},
        )
        self._captures_this_session += 1
        return {"id": row.get("id"), "captured": True, "importance": row.get("importance")}

    def _tool_recall(self, args: Dict[str, Any]) -> Dict[str, Any]:
        session_id = (args.get("session_id") or "").strip()
        if not session_id:
            return tool_error("session_id is required")
        limit = min(int(args.get("limit") or 50), 200)
        rows = _adapter_thread(config=self._config, session_id=session_id, limit=limit)
        return {
            "session_id": session_id,
            "thoughts": [
                {
                    "id": r.get("id"),
                    "content": r.get("content"),
                    "importance": r.get("importance"),
                    "parent_id": r.get("parent_id"),
                    "source": (r.get("metadata") or {}).get("source"),
                    "created_at": str(r.get("created_at", "")),
                }
                for r in rows
            ],
            "count": len(rows),
        }

    @staticmethod
    def _format_recall(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return ""
        lines = ["## Memory recall"]
        for r in rows:
            content = (r.get("content") or "").strip().replace("\n", " ")
            if len(content) > 220:
                content = content[:217] + "..."
            imp = r.get("importance")
            imp_tag = f" [imp={imp:.2f}]" if isinstance(imp, (int, float)) else ""
            lines.append(f"- {content}{imp_tag}")
        return "\n".join(lines) + "\n"


# ═════════════════════════════════════════════════════════════════════════════
# Hermes plugin entry point
# ═════════════════════════════════════════════════════════════════════════════


def register(ctx) -> None:
    """Hermes plugin entry point — register the provider via the ctx callback."""
    ctx.register_memory_provider(SupabaseMemoryProvider())


__all__ = ["SupabaseMemoryProvider", "SupabaseConfig", "load_config", "register"]

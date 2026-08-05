"""sint-ua fact chain — local FACT block storage with hash chain integrity and MCP integration."""

import sqlite3
import hashlib
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("acp.sint_ua")

DEFAULT_DB = os.path.expanduser("~/.agent/knowledge.db")

# MCP server registry — available SINT MCP servers for knowledge enrichment
MCP_SERVERS = {
    "algo": "sint-algo-mcp",
    "crypto": "sint-crypto-mcp",
    "dso": "sint-dso-matcher",
    "router": "sint-dso-router",
    "swarm": "sint-dso-swarm",
    "validator": "sint-dso-validator",
    "embed": "sint-embed-mcp",
    "epistemic": "sint-epistemic-mcp",
    "forest": "sint-forest-mcp",
    "historian": "sint-historian-mcp",
    "liquid": "sint-liquid-mcp",
    "llm_swarm": "sint-llm-swarm",
    "prospective": "sint-prospective-mcp",
    "re_graph": "sint-re-graph-mcp",
    "resonance": "sint-resonance-mcp",
    "self": "sint-self-mcp",
    "spec": "sint-spec-mcp",
}


class FactChain:
    """Local FACT-chain storage with hash-chain integrity and MCP knowledge integration."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize the SQLite database."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    register TEXT NOT NULL DEFAULT 'FACT',
                    prev_hash TEXT,
                    hash TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    project TEXT DEFAULT 'ACP-MVP',
                    content TEXT NOT NULL,
                    source TEXT,
                    confidence REAL DEFAULT 1.0,
                    tags TEXT,
                    links TEXT,
                    mcp_sources TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_references (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    mcp_server TEXT NOT NULL,
                    mcp_tool TEXT NOT NULL,
                    mcp_query TEXT,
                    mcp_result_hash TEXT,
                    FOREIGN KEY (fact_id) REFERENCES facts(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_tags ON facts(tags);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_timestamp ON facts(timestamp);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mcp_refs_fact ON mcp_references(fact_id);
            """)
            conn.commit()

    def _compute_hash(self, block: dict) -> str:
        """Compute SHA256 hash of a fact block."""
        canonical = json.dumps(block, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _get_last_hash(self) -> str | None:
        """Get the hash of the last fact in the chain."""
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT hash FROM facts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def record(
        self,
        content: str,
        source: str = "",
        confidence: float = 1.0,
        tags: list[str] | None = None,
        links: list[int] | None = None,
        project: str = "ACP-MVP",
        mcp_sources: list[dict] | None = None,
    ) -> int:
        """Record a new FACT block in the chain.

        mcp_sources: optional list of dicts with keys:
          - server: MCP server name (e.g. 'sint-algo-mcp')
          - tool: tool called (e.g. 'search_algorithm')
          - query: the query string
          - result_hash: hash of the MCP result for integrity
        """
        prev_hash = self._get_last_hash()

        block = {
            "register": "FACT",
            "prev_hash": prev_hash or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project": project,
            "content": content,
            "source": source,
            "confidence": confidence,
            "tags": tags or [],
            "links": links or [],
        }

        block_hash = self._compute_hash(block)
        block["hash"] = block_hash

        tags_str = ",".join(tags or [])
        links_str = ",".join(str(l) for l in (links or []))
        mcp_sources_str = json.dumps(mcp_sources or [])

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                """INSERT INTO facts (register, prev_hash, hash, timestamp, project, content, source, confidence, tags, links, mcp_sources)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    block["register"],
                    block["prev_hash"],
                    block["hash"],
                    block["timestamp"],
                    block["project"],
                    block["content"],
                    block["source"],
                    block["confidence"],
                    tags_str,
                    links_str,
                    mcp_sources_str,
                ),
            )
            fact_id = cursor.lastrowid

            # Store MCP references
            if mcp_sources:
                for mcp in mcp_sources:
                    conn.execute(
                        """INSERT INTO mcp_references (fact_id, mcp_server, mcp_tool, mcp_query, mcp_result_hash)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            fact_id,
                            mcp.get("server", ""),
                            mcp.get("tool", ""),
                            mcp.get("query", ""),
                            mcp.get("result_hash", ""),
                        ),
                    )

            conn.commit()

        logger.info(f"Recorded FACT #{fact_id}: {content[:80]}...")
        return fact_id

    def query(self, tag: str | None = None, min_confidence: float = 0.0, limit: int = 50) -> list[dict]:
        """Query facts by tag and/or minimum confidence."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM facts WHERE 1=1"
            params = []

            if tag:
                query += " AND (tags LIKE ? OR tags LIKE ?)"
                params.append(f"%{tag}%")
                params.append(f"{tag},%")

            query += " AND confidence >= ?"
            params.append(min_confidence)

            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def query_mcp_enriched(self, tag: str, mcp_server: str | None = None) -> list[dict]:
        """Query facts enriched with MCP references.

        Returns facts with their MCP knowledge sources attached.
        """
        facts = self.query(tag=tag, min_confidence=0.0, limit=50)

        with sqlite3.connect(str(self.db_path)) as conn:
            for fact in facts:
                fact_id = fact["id"]
                refs = conn.execute(
                    "SELECT mcp_server, mcp_tool, mcp_query FROM mcp_references WHERE fact_id = ?",
                    (fact_id,),
                ).fetchall()
                fact["mcp_references"] = [
                    {"server": r[0], "tool": r[1], "query": r[2]}
                    for r in refs
                ]

        return facts

    def cross_reference(self, content: str, domain: str = "general") -> list[dict]:
        """Cross-reference a fact against MCP knowledge bases.

        Queries relevant MCP servers and returns matching knowledge
        that can be linked to the fact.
        """
        results = []

        # Query algo-mcp for algorithm/domain knowledge
        try:
            from sint_algo_mcp import search_algorithm, describe_middleware
            algo_results = search_algorithm(query=content, limit=3)
            for r in algo_results:
                results.append({
                    "server": "sint-algo-mcp",
                    "tool": "search_algorithm",
                    "query": content,
                    "result": r.get("title", ""),
                    "result_hash": hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(),
                })
        except Exception as e:
            logger.debug(f"MCP algo query skipped: {e}")

        # Query crypto-mcp for crypto-related content
        if "crypto" in content.lower() or "cipher" in content.lower() or "encrypt" in content.lower():
            try:
                from sint_crypto_mcp import list_ciphers, crypto_help
                crypto_results = list_ciphers()
                for r in crypto_results[:3]:
                    results.append({
                        "server": "sint-crypto-mcp",
                        "tool": "list_ciphers",
                        "query": content,
                        "result": r.get("name", ""),
                        "result_hash": hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(),
                    })
            except Exception as e:
                logger.debug(f"MCP crypto query skipped: {e}")

        # Query dso-matcher for pattern matching
        try:
            from sint_dso_matcher import dso_match
            match_results = dso_match(query=content, domain=domain, k=3)
            for r in match_results:
                results.append({
                    "server": "sint-dso-matcher",
                    "tool": "dso_match",
                    "query": content,
                    "result": str(r),
                    "result_hash": hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(),
                })
        except Exception as e:
            logger.debug(f"MCP dso query skipped: {e}")

        # Query resonance for cross-domain insight
        try:
            from sint_resonance_mcp import resonance_find
            resonance_results = resonance_find(query=content, k=3)
            for r in resonance_results:
                results.append({
                    "server": "sint-resonance-mcp",
                    "tool": "resonance_find",
                    "query": content,
                    "result": str(r),
                    "result_hash": hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(),
                })
        except Exception as e:
            logger.debug(f"MCP resonance query skipped: {e}")

        return results

    def verify_chain(self) -> dict:
        """Verify the integrity of the hash chain."""
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, prev_hash, hash, content, source, confidence, tags, links, timestamp, project, mcp_sources FROM facts ORDER BY id ASC"
            ).fetchall()

        broken = []
        for row in rows:
            (row_id, prev_hash, stored_hash, content, source, confidence,
             tags_str, links_str, timestamp, project, mcp_sources_str) = row
            tags = tags_str.split(",") if tags_str else []
            links = [int(x) for x in links_str.split(",") if x] if links_str else []

            block = {
                "register": "FACT",
                "prev_hash": prev_hash or "",
                "timestamp": timestamp or "",
                "project": project or "",
                "content": content,
                "source": source or "",
                "confidence": confidence or 1.0,
                "tags": tags,
                "links": links,
            }
            expected_hash = self._compute_hash(block)
            if expected_hash != stored_hash:
                broken.append({"id": row_id, "expected": expected_hash, "actual": stored_hash})

        return {
            "total": len(rows),
            "broken": len(broken),
            "valid": len(broken) == 0,
            "broken_links": broken,
        }

    def stats(self) -> dict:
        """Get chain statistics."""
        with sqlite3.connect(str(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            by_tag = conn.execute(
                "SELECT tags, COUNT(*) as cnt FROM facts GROUP BY tags ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
            mcp_refs = conn.execute(
                "SELECT COUNT(*) FROM mcp_references"
            ).fetchone()[0]

        return {
            "total_facts": total,
            "mcp_references": mcp_refs,
            "top_tags": [{"tags": r[0], "count": r[1]} for r in by_tag],
        }

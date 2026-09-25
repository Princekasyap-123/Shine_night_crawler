"""
Redis key builders for control/.

Everything else control needs — backlog depth, pause state — lives in
Postgres via CandidateRecord/CrawlSession, read live rather than
cached, since a stale backlog count is exactly the kind of bug that
would let the crawler outrun submission/Ollama processing. This module
holds only the one piece of state that's genuinely ephemeral and has
no natural home in a relational table: which search term goes next in
tonight's round-robin rotation.
"""

ROTATION_CURSOR_KEY_TEMPLATE = "control:rotation:cursor:{session_id}"
# Generously covers a full 10pm-5am night; the key is scoped to one
# session's id so a stale TTL past sunrise is harmless either way.
ROTATION_CURSOR_TTL_SECONDS = 12 * 60 * 60


def rotation_cursor_key(session_id: int) -> str:
    return ROTATION_CURSOR_KEY_TEMPLATE.format(session_id=session_id)

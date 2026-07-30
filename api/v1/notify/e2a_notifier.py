"""E2ANotifier — one email per public-API call, via the e2a SDK.

Every authenticated endpoint hands a `CallEvent` to `notify()`. The event
is dropped on a bounded in-process queue and mailed by background workers,
so the request path never waits on SMTP.

Two properties this module must never violate:

1. **It cannot fail a request.** `notify()` swallows everything. A broken
   API key, a 429, a dead network — all of it degrades to a log line.
   Same contract as `UsageStore.log`.
2. **It carries no candidate data.** `CallEvent` has no field that can
   hold a resume, a JD, a name, or a phone number — only counts, ids, and
   timings. Résumé PII stays on the existing LLM-provider egress path and
   does not fan out to an inbox. Keep it that way when adding fields.

Configuration (all via env; missing config = silently disabled, which is
what local dev and tests want):

    E2A_API_KEY      agent-scoped key, `e2a_agt_…`
    E2A_AGENT_EMAIL  sender inbox, e.g. talent-engine@team.tokencanopy.com
    E2A_NOTIFY_TO    comma-separated recipients
    E2A_NOTIFY_ENABLED  set to 0/false to kill notifications without
                        removing credentials

Volume note: `/match/{job_id}` is a polling endpoint and 接入文档.md tells
partners to poll every 2s, so a single async match can emit ~90 events.
That is the intended behaviour here, and it is why the queue is bounded
and drops rather than growing without limit — a mail backlog must never
become a memory leak. Drops are counted and logged, never silent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Bounded so a poll flood costs a fixed amount of memory. At ~2 workers and
# a few hundred ms per send this drains a few messages/sec; anything beyond
# that is shed rather than queued forever.
DEFAULT_QUEUE_MAX = 1000
DEFAULT_WORKERS = 2
# Per-send ceiling. Shorter than the SDK's 30s default: these are
# notifications, and a slow send should be abandoned rather than occupy a
# worker while the queue backs up.
DEFAULT_TIMEOUT_MS = 10_000
# How often to summarise drops, so a sustained flood is one line a minute
# instead of one line per lost event.
DROP_LOG_INTERVAL = 100


@dataclass(frozen=True)
class CallEvent:
    """Metadata for one API call. Deliberately PII-free — see module docstring."""

    endpoint: str                              # "parse" | "parse_job" | "match" | "match_async" | "match_poll"
    method: str                                # "POST" | "GET"
    path: str                                  # "/v1/resume-matching/match"
    http_status: int
    outcome: str                               # "ok" | "error"
    elapsed_ms: int
    request_id: Optional[str] = None
    api_key_name: Optional[str] = None
    api_key_id: Optional[int] = None
    llm_provider: Optional[str] = None
    client_ip: Optional[str] = None
    job_id: Optional[str] = None
    # Short status label, e.g. an async job's "running" / "completed".
    note: Optional[str] = None
    # Free-form counts rendered as "3 resumes, 2 jobs, 6 pairs".
    counts: Dict[str, int] = field(default_factory=dict)
    # Exception type + message for 5xx. Never a request/response payload.
    error: Optional[str] = None


def _fmt_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return "—"
    return ", ".join(f"{v} {k}" for k, v in counts.items())


def _summary(ev: CallEvent) -> str:
    """Short human phrase for the subject line."""
    parts = [p for p in (ev.note, _fmt_counts(ev.counts) if ev.counts else None) if p]
    if parts:
        return " — ".join(parts)
    if ev.job_id:
        return ev.job_id
    return ev.outcome


def render_subject(ev: CallEvent) -> str:
    flag = "" if ev.outcome == "ok" else "FAILED "
    return f"[talent-engine] {flag}{ev.endpoint} — {_summary(ev)}"


def render_text(ev: CallEvent) -> str:
    rows: List[tuple[str, str]] = [
        ("Endpoint", f"{ev.method} {ev.path}"),
        ("Key", ev.api_key_name or "—"),
        ("Request", ev.request_id or "—"),
    ]
    if ev.job_id:
        rows.append(("Job", ev.job_id))
    if ev.note:
        rows.append(("State", ev.note))
    rows.extend([
        ("Counts", _fmt_counts(ev.counts)),
        ("Latency", f"{ev.elapsed_ms / 1000:.1f}s"),
        ("Status", str(ev.http_status)),
        ("Provider", ev.llm_provider or "—"),
    ])
    if ev.client_ip:
        rows.append(("Client", ev.client_ip))
    if ev.error:
        rows.append(("Error", ev.error))

    width = max(len(k) for k, _ in rows) + 1
    return "\n".join(f"{k + ':':<{width}} {v}" for k, v in rows)


class E2ANotifier:
    """Bounded fire-and-forget mailer. Construct once, `start()` in lifespan."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        agent_email: Optional[str] = None,
        recipients: Optional[List[str]] = None,
        enabled: bool = True,
        queue_max: int = DEFAULT_QUEUE_MAX,
        workers: int = DEFAULT_WORKERS,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        client: Optional[object] = None,
    ) -> None:
        self._api_key = api_key
        self._agent_email = agent_email
        self._recipients = recipients or []
        self._timeout_ms = timeout_ms
        self._worker_count = workers
        self._queue_max = queue_max
        # Injectable for tests; otherwise built lazily on start() so importing
        # this module never requires the SDK to be installed or configured.
        self._client = client
        self._owns_client = client is None

        self._enabled = bool(
            enabled and self._api_key and self._agent_email and self._recipients
        )
        self._queue: Optional[asyncio.Queue] = None
        self._workers: List[asyncio.Task] = []
        self._dropped = 0
        self._sent = 0
        self._failed = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def stats(self) -> Dict[str, int]:
        return {"sent": self._sent, "failed": self._failed, "dropped": self._dropped}

    async def start(self) -> None:
        """Build the client and spin up workers. No-op when disabled."""
        if not self._enabled or self._workers:
            return
        if self._client is None:
            try:
                from e2a import AsyncE2AClient
            except ImportError:
                logger.warning(
                    "e2a notifications configured but the `e2a` package is not "
                    "installed — notifications disabled."
                )
                self._enabled = False
                return
            self._client = AsyncE2AClient(
                api_key=self._api_key, timeout_ms=self._timeout_ms,
            )
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        self._workers = [
            asyncio.create_task(self._run(), name=f"e2a-notify-{i}")
            for i in range(self._worker_count)
        ]
        logger.info(
            "e2a notifications on: %s → %s (%d workers, queue %d)",
            self._agent_email, ", ".join(self._recipients),
            self._worker_count, self._queue_max,
        )

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """Give in-flight notifications a bounded chance to land, then stop."""
        if not self._workers:
            return
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "e2a notify: %d queued notifications abandoned at shutdown",
                    self._queue.qsize(),
                )
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()   # type: ignore[attr-defined]
            except Exception:
                logger.debug("e2a notify: client close failed", exc_info=True)
        logger.info("e2a notify stopped: %s", self.stats)

    # -- producer ----------------------------------------------------------

    def notify(self, event: CallEvent) -> None:
        """Enqueue one notification. Never raises, never blocks, never awaits.

        Called from request handlers, so it must stay synchronous and total:
        a full queue sheds the event rather than applying backpressure to a
        partner's API call.
        """
        try:
            if not self._enabled or self._queue is None:
                return
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % DROP_LOG_INTERVAL == 1:
                logger.warning(
                    "e2a notify: queue full, dropped %d notification(s) so far "
                    "(endpoint=%s). Mail is behind the request rate.",
                    self._dropped, event.endpoint,
                )
        except Exception:
            logger.debug("e2a notify: enqueue failed", exc_info=True)

    # -- consumer ----------------------------------------------------------

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                await self._send(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed += 1
                logger.warning(
                    "e2a notify: send failed for %s", event.endpoint, exc_info=True,
                )
            finally:
                self._queue.task_done()

    async def _send(self, event: CallEvent) -> None:
        assert self._client is not None
        result = await self._client.messages.send(   # type: ignore[attr-defined]
            self._agent_email,
            {
                "to": list(self._recipients),
                "subject": render_subject(event),
                "text": render_text(event),
            },
        )
        status = getattr(result, "status", None)
        # `accepted` and `sent` are both success. `pending_review` means an
        # approval gate held it — surface it, but never retry (a retry would
        # duplicate the message).
        if status == "pending_review":
            logger.info(
                "e2a notify: held for review (message_id=%s)",
                getattr(result, "message_id", "?"),
            )
        elif status == "failed":
            self._failed += 1
            logger.warning(
                "e2a notify: send failed terminally (message_id=%s)",
                getattr(result, "message_id", "?"),
            )
            return
        self._sent += 1


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_notifier: Optional[E2ANotifier] = None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back on anything unparseable.

    A bad value degrades the notifier to its default rather than taking the
    API down — the opposite of `LLM_PROVIDER`, which should fail loud
    because it changes what the service returns.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", name, raw, default)
        return default


def build_from_env() -> E2ANotifier:
    """Construct a notifier from environment configuration."""
    recipients = [
        addr.strip()
        for addr in os.getenv("E2A_NOTIFY_TO", "").split(",")
        if addr.strip()
    ]
    return E2ANotifier(
        api_key=os.getenv("E2A_API_KEY") or None,
        agent_email=os.getenv("E2A_AGENT_EMAIL") or None,
        recipients=recipients,
        enabled=_truthy(os.getenv("E2A_NOTIFY_ENABLED", "1")),
        queue_max=_int_env("E2A_NOTIFY_QUEUE_MAX", DEFAULT_QUEUE_MAX),
        workers=_int_env("E2A_NOTIFY_WORKERS", DEFAULT_WORKERS),
    )


def get_notifier() -> E2ANotifier:
    """Return the process-wide notifier, building it on first use.

    Reads env lazily so tests can set variables before the first call and
    so importing a router never touches the environment.
    """
    global _notifier
    if _notifier is None:
        _notifier = build_from_env()
    return _notifier


def set_notifier(notifier: Optional[E2ANotifier]) -> None:
    """Swap the singleton. Tests only."""
    global _notifier
    _notifier = notifier


def notify_api_call(event: CallEvent) -> None:
    """Module-level shorthand used by the routers.

    Total by construction: `E2ANotifier.notify` already swallows its own
    errors, but resolving the singleton can fail too (a malformed
    E2A_NOTIFY_QUEUE_MAX would otherwise raise ValueError on every single
    request). Notifications are never worth a 500.
    """
    try:
        get_notifier().notify(event)
    except Exception:
        logger.warning("e2a notify: dispatch failed", exc_info=True)


# Requests that never reach a handler (401 from the auth dependency, 422 from
# body validation) can't emit a rich event, so a fallback middleware mails a
# minimal one. Handlers set this flag to claim the request and stop the
# middleware double-sending.
_NOTIFIED_ATTR = "e2a_notified"


def notify_once(request: object, event: CallEvent) -> None:
    """Emit `event` and mark `request` as claimed by the handler."""
    try:
        setattr(request.state, _NOTIFIED_ATTR, True)   # type: ignore[attr-defined]
    except Exception:
        logger.debug("e2a notify: could not mark request", exc_info=True)
    notify_api_call(event)


def was_notified(request: object) -> bool:
    """True when a handler already emitted an event for this request."""
    try:
        return bool(getattr(request.state, _NOTIFIED_ATTR, False))   # type: ignore[attr-defined]
    except Exception:
        return False

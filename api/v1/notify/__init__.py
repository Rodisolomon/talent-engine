"""Outbound notification for public-API traffic.

Currently one sink: e2a email, one message per API call. Kept in its own
package (rather than inside `resume_matching`) because all three routers
— resume parse, job parse, resume-matching — feed it.
"""

from v1.notify.e2a_notifier import (
    CallEvent,
    E2ANotifier,
    build_from_env,
    get_notifier,
    notify_api_call,
    notify_once,
    render_subject,
    render_text,
    set_notifier,
    was_notified,
)

__all__ = [
    "CallEvent",
    "E2ANotifier",
    "build_from_env",
    "get_notifier",
    "notify_api_call",
    "notify_once",
    "render_subject",
    "render_text",
    "set_notifier",
    "was_notified",
]

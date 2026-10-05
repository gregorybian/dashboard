from dataclasses import dataclass, field

# Priority levels used for sorting and badges.
LOW, NORMAL, HIGH, URGENT = 0, 1, 2, 3


@dataclass
class Item:
    id: str
    source: str
    kind: str
    title: str
    body: str = ""
    url: str = ""
    author: str = ""
    ts: float = 0.0          # when the thing started (first unreplied msg, PR opened, ...)
    updated_ts: float = 0.0  # latest activity; a "done" item reopens when this increases
    priority: int = NORMAL
    meta: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    items: list
    # Ids the source didn't check this round; their stored state is left alone.
    preserve: set = field(default_factory=set)
    # Extra data for the page (kanban, full PR list), stored as JSON.
    snapshot: dict | None = None

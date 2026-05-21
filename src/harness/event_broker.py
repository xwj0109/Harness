from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from harness.models import EventStreamType, StoredEventRecord


EventStreamKey = tuple[str, str]


class EventSubscription:
    def __init__(
        self,
        *,
        stream_key: EventStreamKey | None = None,
        on_close: Callable[[EventSubscription], None] | None = None,
    ) -> None:
        self.stream_key = stream_key
        self._on_close = on_close
        self._condition = threading.Condition()
        self._events: list[StoredEventRecord] = []
        self._seen_ids: set[str] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def enqueue(self, event: StoredEventRecord) -> None:
        with self._condition:
            if self._closed or event.id in self._seen_ids:
                return
            self._seen_ids.add(event.id)
            self._events.append(event)
            self._events.sort(key=self._event_sort_key)
            self._condition.notify()

    def next(self, timeout: float | None = None) -> StoredEventRecord | None:
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while not self._events and not self._closed:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._events:
                return self._events.pop(0)
            return None

    def drain(self, *, limit: int | None = None) -> list[StoredEventRecord]:
        with self._condition:
            if limit is None:
                events = list(self._events)
                self._events.clear()
                return events
            events = self._events[:limit]
            del self._events[:limit]
            return events

    def close(self) -> None:
        on_close = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            on_close = self._on_close
            self._condition.notify_all()
        if on_close is not None:
            on_close(self)

    def _event_sort_key(self, event: StoredEventRecord) -> tuple[Any, ...]:
        if self.stream_key is not None:
            return (event.seq, event.created_at, event.id)
        return (event.created_at, event.stream_type.value, event.stream_id, event.seq, event.id)


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stream_subscribers: dict[EventStreamKey, set[EventSubscription]] = {}
        self._global_subscribers: set[EventSubscription] = set()

    def publish(self, event: StoredEventRecord) -> None:
        stream_key = (event.stream_type.value, event.stream_id)
        with self._lock:
            subscribers = list(self._stream_subscribers.get(stream_key, set()))
            subscribers.extend(self._global_subscribers)
        for subscription in subscribers:
            subscription.enqueue(event)

    def subscribe(
        self,
        stream_type: EventStreamType | str,
        stream_id: str,
        *,
        replay: Iterable[StoredEventRecord] = (),
    ) -> EventSubscription:
        stream_value = EventStreamType(stream_type.value if isinstance(stream_type, EventStreamType) else stream_type)
        stream_key = (stream_value.value, stream_id)
        subscription = EventSubscription(stream_key=stream_key, on_close=self._unsubscribe)
        with self._lock:
            self._stream_subscribers.setdefault(stream_key, set()).add(subscription)
        for event in replay:
            subscription.enqueue(event)
        return subscription

    def subscribe_all(self, *, replay: Iterable[StoredEventRecord] = ()) -> EventSubscription:
        subscription = EventSubscription(on_close=self._unsubscribe)
        with self._lock:
            self._global_subscribers.add(subscription)
        for event in replay:
            subscription.enqueue(event)
        return subscription

    def close_all(self) -> None:
        with self._lock:
            subscriptions = list(self._global_subscribers)
            for subscribers in self._stream_subscribers.values():
                subscriptions.extend(subscribers)
            self._global_subscribers.clear()
            self._stream_subscribers.clear()
        for subscription in set(subscriptions):
            subscription.close()

    def _unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            if subscription.stream_key is None:
                self._global_subscribers.discard(subscription)
                return
            subscribers = self._stream_subscribers.get(subscription.stream_key)
            if subscribers is None:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._stream_subscribers.pop(subscription.stream_key, None)


_BROKERS: dict[Path, EventBroker] = {}
_BROKERS_LOCK = threading.RLock()


def get_event_broker(project_root: Path | str) -> EventBroker:
    root = Path(project_root).resolve()
    with _BROKERS_LOCK:
        broker = _BROKERS.get(root)
        if broker is None:
            broker = EventBroker()
            _BROKERS[root] = broker
        return broker


def reset_event_broker(project_root: Path | str | None = None) -> None:
    with _BROKERS_LOCK:
        if project_root is None:
            brokers = list(_BROKERS.values())
            _BROKERS.clear()
        else:
            broker = _BROKERS.pop(Path(project_root).resolve(), None)
            brokers = [broker] if broker is not None else []
    for broker in brokers:
        broker.close_all()


def subscribe_store_events(
    store: Any,
    stream_type: EventStreamType | str,
    stream_id: str,
    *,
    after_seq: int | None = None,
    limit: int | None = None,
) -> EventSubscription:
    broker = get_event_broker(store.project_root)
    subscription = broker.subscribe(stream_type, stream_id)
    for event in store.list_store_events(stream_type, stream_id, after_seq=after_seq, limit=limit):
        subscription.enqueue(event)
    return subscription


def subscribe_global_events(
    project_root: Path | str,
    *,
    replay: Iterable[StoredEventRecord] = (),
) -> EventSubscription:
    return get_event_broker(project_root).subscribe_all(replay=replay)

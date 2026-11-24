"""Offline-first sync: push queued changes, then pull what changed on the server."""

import threading
import time
from datetime import datetime, timezone

from .api import Api, ApiError, AuthError, OfflineError
from .store import Store

PUSH_BATCH = 200
PULL_PAGE = 500


class SyncResult:
    def __init__(self):
        self.pushed = 0
        self.rejected = []  # (voter_id, reason) the server refused permanently
        self.pulled = 0
        self.offline = False
        self.auth_failed = False
        self.error = None

    @property
    def ok(self):
        return not (self.offline or self.auth_failed or self.error)


class SyncEngine:
    def __init__(self, store: Store, api: Api):
        self.store = store
        self.api = api
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.listeners = []  # callables(SyncResult)

    # -- one round -----------------------------------------------------------

    def sync_once(self, booth_ids) -> SyncResult:
        result = SyncResult()
        if not self._lock.acquire(blocking=False):
            return result  # a round is already running
        try:
            self._push(result)
            for booth in booth_ids:
                self._pull(booth, result)
            self.store.set("last_sync_ok", datetime.now(timezone.utc).isoformat())
        except OfflineError:
            result.offline = True
        except AuthError:
            result.auth_failed = True
        except ApiError as e:
            result.error = e.message
        finally:
            self._lock.release()
        for fn in list(self.listeners):
            try:
                fn(result)
            except Exception:
                pass
        return result

    def _push(self, result):
        while True:
            batch = self.store.outbox_batch(PUSH_BATCH)
            if not batch:
                return
            response = self.api.sync(batch)
            done, fresh = [], []
            for item in response.get("results", []):
                done.append(item.get("event_id"))
                if item.get("result") == "error":
                    match = next((c for c in batch if c["event_id"] == item.get("event_id")), {})
                    result.rejected.append((match.get("voter_id"), item.get("detail")))
                elif item.get("voter"):
                    fresh.append(item["voter"])
                    result.pushed += item.get("result") == "applied"
            done = [e for e in done if e]
            if not done:
                result.error = "Server did not accept the queued changes"
                return
            self.store.outbox_remove(done)
            # Server copy is now the truth for these voters (unless the agent
            # made yet another change meanwhile; upsert skips those).
            self.store.upsert_voters(fresh)
            if len(batch) < PUSH_BATCH:
                return

    def _pull(self, booth, result):
        key = f"last_pull:{booth}"
        since = self.store.get(key)
        offset, newest = 0, since
        while True:
            page = self.api.voters(booth=booth, since=since, offset=offset, limit=PULL_PAGE)
            rows = page.get("results", [])
            result.pulled += self.store.upsert_voters(rows)
            for r in rows:
                if r.get("updated_at") and (newest is None or r["updated_at"] > newest):
                    newest = r["updated_at"]
            offset += len(rows)
            if not page.get("next") or not rows:
                break
        if newest:
            self.store.set(key, newest)

    def full_reload(self, booth):
        """Forget the pull bookmark and download the whole booth again."""
        self.store.set(f"last_pull:{booth}", None)
        return self.sync_once([booth])

    # -- background loop -------------------------------------------------------

    def start(self, booth_ids_fn, interval=30):
        self.stop()
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                self.sync_once(booth_ids_fn())

        self._thread = threading.Thread(target=loop, daemon=True, name="boothpulse-sync")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None


def minutes_since(iso):
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - then).total_seconds() // 60)


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False

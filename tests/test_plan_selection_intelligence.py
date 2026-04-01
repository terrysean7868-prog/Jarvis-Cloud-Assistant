from datetime import datetime, timezone


class _FakeCursor(list):
    def sort(self, key, direction):
        reverse = int(direction) < 0
        return _FakeCursor(sorted(self, key=lambda r: r.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=reverse))

    def limit(self, n):
        return _FakeCursor(self[: int(n)])


class _FakeCollection:
    def __init__(self):
        self.rows = {}

    def create_index(self, *args, **kwargs):
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        out = []
        for row in self.rows.values():
            if not self._matches(row, query):
                continue
            out.append(self._project(row, projection))
        return _FakeCursor(out)

    def find_one(self, query, projection=None, sort=None):
        rows = [r for r in self.rows.values() if self._matches(r, query or {})]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda r: r.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=int(direction) < 0)
        if not rows:
            return None
        return self._project(rows[0], projection)

    def update_one(self, query, update, upsert=False):
        sig = str((query or {}).get("signature") or "")
        if not sig:
            return None
        row = self.rows.get(sig)
        if row is None:
            if not upsert:
                return None
            row = {"signature": sig}
            self.rows[sig] = row

        set_on_insert = update.get("$setOnInsert") if isinstance(update.get("$setOnInsert"), dict) else {}
        if len(row.keys()) <= 1:
            for k, v in set_on_insert.items():
                row[k] = v

        for k, v in (update.get("$set") or {}).items():
            row[k] = v

        for k, inc in (update.get("$inc") or {}).items():
            row[k] = int(row.get(k) or 0) + int(inc)

        return None

    @staticmethod
    def _project(row, projection):
        if not isinstance(projection, dict) or not projection:
            return dict(row)
        out = {}
        for k, include in projection.items():
            if include and k in row:
                out[k] = row[k]
        return out

    @staticmethod
    def _matches(row, query):
        for key, expected in (query or {}).items():
            if key == "$or" and isinstance(expected, list):
                if not any(_FakeCollection._matches(row, cond) for cond in expected):
                    return False
                continue
            if isinstance(expected, dict) and "$in" in expected:
                if row.get(key) not in expected.get("$in"):
                    return False
                continue
            if row.get(key) != expected:
                return False
        return True


def _step(step_id, action, depends_on=None, params=None):
    return {
        "step": 1,
        "step_id": step_id,
        "depends_on": depends_on,
        "action": action,
        "params": params or {},
        "retry_once": True,
    }


def test_same_query_prefers_more_successful_plan(monkeypatch):
    from apps.web import app as jarvis_app

    col = _FakeCollection()
    monkeypatch.setattr(jarvis_app, "_plan_learning_collection", lambda: col)
    monkeypatch.setattr(jarvis_app, "_delegated_tasks_collection", lambda: None)

    source = "open browser and search for python docs"
    plan_a = [_step("step_1", "open_url", params={"url": "https://docs.python.org"})]
    plan_b = [
        _step("step_1", "open_app", params={"app_name": "chrome"}),
        _step("step_2", "open_url", depends_on="step_1", params={"url": "https://docs.python.org"}),
    ]

    for _ in range(4):
        jarvis_app._save_plan_learning(source, plan_a, success=True, execution_time_ms=300, retries_used=0)
    for _ in range(3):
        jarvis_app._save_plan_learning(source, plan_b, success=False, execution_time_ms=900, retries_used=1)

    selected, scored = jarvis_app._select_best_plan_option(
        source_text=source,
        options=[
            {"id": "A", "source": "direct", "steps": plan_a, "metadata": {}},
            {"id": "B", "source": "generated", "steps": plan_b, "metadata": {}},
        ],
        device_id="primary",
        username="tester",
        agent_online=True,
        agent_temporarily_unavailable=False,
    )

    assert selected is not None
    assert selected.get("id") == "A"
    assert len(scored) == 2


def test_recent_failures_are_penalized(monkeypatch):
    from apps.web import app as jarvis_app

    col = _FakeCollection()
    monkeypatch.setattr(jarvis_app, "_plan_learning_collection", lambda: col)
    monkeypatch.setattr(jarvis_app, "_delegated_tasks_collection", lambda: None)

    source = "open dashboard"
    unstable = [_step("step_1", "open_app", params={"app_name": "dashboard"})]
    stable = [_step("step_1", "open_url", params={"url": "https://example.com/dashboard"})]

    for _ in range(3):
        jarvis_app._save_plan_learning(source, unstable, success=False, execution_time_ms=400, retries_used=1)
    for _ in range(2):
        jarvis_app._save_plan_learning(source, stable, success=True, execution_time_ms=350, retries_used=0)

    selected, _ = jarvis_app._select_best_plan_option(
        source_text=source,
        options=[
            {"id": "A", "source": "generated", "steps": unstable, "metadata": {}},
            {"id": "B", "source": "direct", "steps": stable, "metadata": {}},
        ],
        device_id="primary",
        username="tester",
        agent_online=True,
        agent_temporarily_unavailable=False,
    )

    assert selected is not None
    assert selected.get("id") == "B"


def test_offline_context_prefers_single_step(monkeypatch):
    from apps.web import app as jarvis_app

    col = _FakeCollection()
    monkeypatch.setattr(jarvis_app, "_plan_learning_collection", lambda: col)
    monkeypatch.setattr(jarvis_app, "_delegated_tasks_collection", lambda: None)

    source = "open chrome and search weather"
    multi_step = [
        _step("step_1", "open_app", params={"app_name": "chrome"}),
        _step("step_2", "open_url", depends_on="step_1", params={"url": "https://www.google.com/search?q=weather"}),
    ]
    single_step = [_step("step_1", "open_url", params={"url": "https://www.google.com/search?q=weather"})]

    selected, _ = jarvis_app._select_best_plan_option(
        source_text=source,
        options=[
            {"id": "A", "source": "generated", "steps": multi_step, "metadata": {}},
            {"id": "B", "source": "fallback_single_step", "steps": single_step, "metadata": {}},
        ],
        device_id="primary",
        username="tester",
        agent_online=False,
        agent_temporarily_unavailable=False,
    )

    assert selected is not None
    assert selected.get("id") == "B"

"""Unit tests for main.py - app factory and /healthz."""

from __future__ import annotations

import base64
import os
import time

import pytest
from fastapi.testclient import TestClient

import gatekey.main as main_module
from gatekey.config import Settings
from gatekey.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )


def test_model_policy_bootstrap_failure_fails_open_permissive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1.3 design doc section 2.2/ADR-3: a bootstrap load failure
    (DB unreachable, timeout, unexpected row shape - anything caught by the
    lifespan's broad `except Exception`) must NOT prevent the app from
    starting, and must leave `model_policy_cache` at its safe, zero-I/O
    `unconfigured`/permissive default.

    This is deliberately *not* the same thing as "every other gateway unit
    test's fake DSN happens to fail fast and get caught" (design doc
    section 6) - this test forces `load_policy_snapshot` itself to raise,
    which is what actually exercises the lifespan's `except Exception`
    branch and its `logger.warning("model_policy_bootstrap_failed", ...)`
    call, rather than relying on a connection-refused side effect.

    Asserts the warning via a monkeypatched spy on `main_module.logger`
    directly, rather than pytest's `caplog` fixture: `alembic/env.py` calls
    `logging.config.fileConfig()` (default `disable_existing_loggers=True`)
    whenever any integration test runs a migration in the same process,
    which permanently sets `logging.getLogger("gatekey").disabled = True`
    for the remainder of that pytest session - `caplog` reliably observes
    this test in isolation but silently observes zero records when the
    integration suite has run first in the same `pytest` invocation
    (alphabetical collection puts `tests/integration` before `tests/unit`).
    A direct spy on the logger instance bypasses that disabled-flag check
    entirely and is robust to test collection order.
    """

    async def _raise(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("simulated DB outage during model-policy bootstrap")

    monkeypatch.setattr(main_module, "load_policy_snapshot", _raise)

    warnings_logged: list[tuple[str, dict]] = []

    def _fake_warning(msg, *args, **kwargs):  # noqa: ANN001
        warnings_logged.append((msg, kwargs))

    monkeypatch.setattr(main_module.logger, "warning", _fake_warning)

    app = create_app(settings=_settings())
    with TestClient(app) as client:
        # The app must still come up and serve traffic despite the
        # bootstrap failure (fail-open, bounded - not a startup crash).
        response = client.get("/healthz")
        assert response.status_code == 200

        snapshot = app.state.model_policy_cache.get()
        assert snapshot.mode == "unconfigured"
        assert snapshot.is_allowed("gpt-4o") is True
        assert snapshot.is_allowed("literally-anything") is True

    assert any(msg == "model_policy_bootstrap_failed" for msg, _kwargs in warnings_logged)


def test_model_policy_bootstrap_self_heals_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security review finding on Phase 1.3 (design doc section 2.2/ADR-3
    addendum): a bootstrap failure must not permanently latch the cache
    onto the permissive `unconfigured` default for the process's whole
    life. If `load_policy_snapshot` fails on the initial attempt but then
    succeeds on a later self-heal retry (e.g. the DB was merely still
    starting up), the cache must eventually reflect the real policy - with
    no restart and no incidental admin PUT required.

    Shortens the self-heal backoff via monkeypatched module constants (see
    `main_module`'s docstring for why these are module-level) so the retry
    loop completes quickly in a unit test rather than waiting out the
    production backoff schedule.
    """
    real_snapshot = main_module.ModelPolicySnapshot(
        mode="denylist", models=frozenset({"gpt-4o"})
    )

    call_count = 0

    async def _fail_once_then_succeed(*args, **kwargs):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated transient DB outage during bootstrap")
        return real_snapshot

    monkeypatch.setattr(main_module, "load_policy_snapshot", _fail_once_then_succeed)
    # Fast, deterministic backoff for the test - production defaults are
    # tuned for a real DB startup delay, not a unit test's patience.
    monkeypatch.setattr(main_module, "_MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "_MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS", 0.01)

    app = create_app(settings=_settings())
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200

        # Deliberately not asserting "cache is still unconfigured immediately
        # after startup" here: with the backoff shortened for test speed, the
        # self-heal task can win that race and heal before this thread gets
        # to check, which would make the assertion itself flaky rather than
        # the code under test. `call_count == 2` (checked below, after the
        # cache has settled) is what actually proves the first attempt failed
        # and a *retry* (not the original bootstrap) is what succeeded.
        self_heal_task = app.state.model_policy_self_heal_task
        assert self_heal_task is not None

        # The self-heal task runs on the TestClient's own portal event loop
        # (a separate thread from this test) - poll the plain,
        # GIL-protected attribute read of `cache.get()` from here rather
        # than trying to `await` a task that belongs to another loop.
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and app.state.model_policy_cache.get().mode == "unconfigured"
        ):
            time.sleep(0.01)
        if app.state.model_policy_cache.get().mode == "unconfigured":
            pytest.fail("model policy cache did not self-heal within 5s")

        healed_snapshot = app.state.model_policy_cache.get()
        assert healed_snapshot.mode == "denylist"
        assert healed_snapshot.models == frozenset({"gpt-4o"})
        assert healed_snapshot.is_allowed("gpt-4o") is False
        assert healed_snapshot.is_allowed("gpt-4o-mini") is True

    # Self-heal task completed (returned) on its own after success, well
    # before shutdown - the shutdown path finding it already done is
    # itself part of what's being exercised (no cancel-of-a-live-task race
    # in the success case). Read only after the `with` block has exited
    # (portal thread has stopped), so this is a safe plain attribute read.
    assert self_heal_task.done()
    assert call_count == 2


def test_model_policy_self_heal_task_cancelled_cleanly_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-heal background task must not leak or hang app shutdown
    when the DB never recovers within the test's lifetime - it should be
    cancelled promptly by `_lifespan`'s `finally` block while still asleep
    between retries, per design doc section 2.2/ADR-3 addendum and section
    6 (fake, unreachable DSN used by the whole gateway unit test harness).
    """

    async def _always_fail(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("simulated permanent DB outage")

    monkeypatch.setattr(main_module, "load_policy_snapshot", _always_fail)
    # Backoff long enough that the task is still asleep (not mid-retry) by
    # the time the `with` block below exits and shutdown runs.
    monkeypatch.setattr(main_module, "_MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS", 10.0)

    app = create_app(settings=_settings())
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        self_heal_task = app.state.model_policy_self_heal_task
        assert self_heal_task is not None
        assert not self_heal_task.done()

    # Shutdown (end of the `with` block) must have cancelled the task
    # promptly rather than leaving it running or hanging shutdown for the
    # full 10s backoff.
    assert self_heal_task.cancelled()


def test_healthz_returns_ok():
    app = create_app(settings=_settings())
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unhandled_exception_returns_structured_envelope_without_leaking_details():
    app = create_app(settings=_settings())

    @app.get("/boom")
    async def boom():
        raise RuntimeError("some internal detail that should not leak")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "some internal detail" not in response.text


# --- M-1: session-cookie CSRF origin guard -----------------------------------


class _NoRowsResult:
    def one_or_none(self):
        return None

    def scalar_one_or_none(self):
        return None


class _NoRowsSession:
    """DB-session stand-in whose every query finds nothing - lets a request
    carrying a (necessarily invalid) session cookie travel past the CSRF
    middleware and die a clean 401 at the auth dependency, no real DB."""

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        return _NoRowsResult()

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _csrf_app():
    from gatekey.api.deps import get_db_session
    from gatekey.db.session import get_db_session as db_session_get_db_session

    app = create_app(settings=_settings())

    async def _override():
        yield _NoRowsSession()

    app.dependency_overrides[get_db_session] = _override
    app.dependency_overrides[db_session_get_db_session] = _override
    return app


_SESSION_COOKIE = {"gatekey_session": "some-cookie-value"}


def test_cross_origin_post_with_session_cookie_rejected_403():
    client = TestClient(_csrf_app())
    response = client.post(
        "/v1/auth/logout",
        cookies=_SESSION_COOKIE,
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_cross_origin_referer_fallback_rejected_403():
    client = TestClient(_csrf_app())
    response = client.post(
        "/v1/auth/logout",
        cookies=_SESSION_COOKIE,
        headers={"Referer": "http://evil.example/page"},
    )
    assert response.status_code == 403


def test_allowed_frontend_origin_passes_guard():
    # Passes the middleware, then fails auth (invalid cookie) - a 401, not
    # the guard's 403, proves the request reached the route stack.
    client = TestClient(_csrf_app())
    response = client.post(
        "/v1/auth/logout",
        cookies=_SESSION_COOKIE,
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 401


def test_no_origin_and_no_referer_passes_guard():
    # curl/scripts/non-browser clients - the attack requires a browser,
    # and browsers always send Origin on cross-origin POSTs.
    client = TestClient(_csrf_app())
    response = client.post("/v1/auth/logout", cookies=_SESSION_COOKIE)
    assert response.status_code == 401


def test_get_with_session_cookie_never_blocked_by_guard():
    client = TestClient(_csrf_app())
    response = client.get(
        "/v1/auth/me",
        cookies=_SESSION_COOKIE,
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 401  # auth failure, not the guard's 403


def test_bearer_only_requests_unaffected_by_guard():
    # No session cookie -> the guard never engages, even on a hostile
    # origin; the bearer trust boundary handles the request as before.
    client = TestClient(_csrf_app())
    response = client.post(
        "/v1/admin/users",
        json={"name": "x"},
        headers={"Origin": "http://localhost:5173", "Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401

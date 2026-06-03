"""Tests for markbot.schedule module (cron, job management)."""


import pytest

from markbot.schedule.cron import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronService,
    _compute_next_run,
    _now_ms,
    _validate_schedule_for_add,
)


class TestCronSchedule:
    def test_at_schedule(self):
        cs = CronSchedule(kind="at", at_ms=1000)
        assert cs.kind == "at"
        assert cs.at_ms == 1000
        assert cs.every_ms is None
        assert cs.expr is None

    def test_every_schedule(self):
        cs = CronSchedule(kind="every", every_ms=60000)
        assert cs.kind == "every"
        assert cs.every_ms == 60000

    def test_cron_schedule(self):
        cs = CronSchedule(kind="cron", expr="0 9 * * *", tz="UTC")
        assert cs.kind == "cron"
        assert cs.expr == "0 9 * * *"
        assert cs.tz == "UTC"


class TestCronPayload:
    def test_defaults(self):
        cp = CronPayload()
        assert cp.kind == "agent_turn"
        assert cp.message == ""
        assert cp.deliver is False
        assert cp.channel is None
        assert cp.to is None

    def test_custom(self):
        cp = CronPayload(kind="system_event", message="hello", deliver=True, channel="cli")
        assert cp.kind == "system_event"
        assert cp.message == "hello"
        assert cp.deliver is True
        assert cp.channel == "cli"


class TestCronJobState:
    def test_defaults(self):
        state = CronJobState()
        assert state.next_run_at_ms is None
        assert state.last_run_at_ms is None
        assert state.last_status is None
        assert state.last_error is None
        assert state.run_history == []


class TestCronJob:
    def test_create_job(self):
        job = CronJob(
            id="j1",
            name="Test Job",
            schedule=CronSchedule(kind="every", every_ms=60000),
            payload=CronPayload(message="hello"),
        )
        assert job.id == "j1"
        assert job.name == "Test Job"
        assert job.enabled is True
        assert job.delete_after_run is False

    def test_job_with_at_schedule(self):
        at_ms = _now_ms() + 60000
        job = CronJob(
            id="j2",
            name="One-shot",
            schedule=CronSchedule(kind="at", at_ms=at_ms),
            payload=CronPayload(message="fire once"),
            delete_after_run=True,
        )
        assert job.schedule.kind == "at"
        assert job.delete_after_run is True


class TestComputeNextRun:
    def test_at_schedule_future(self):
        now = _now_ms()
        future = now + 60000
        result = _compute_next_run(CronSchedule(kind="at", at_ms=future), now)
        assert result == future

    def test_at_schedule_past(self):
        now = _now_ms()
        past = now - 60000
        result = _compute_next_run(CronSchedule(kind="at", at_ms=past), now)
        assert result is None

    def test_every_schedule(self):
        now = _now_ms()
        result = _compute_next_run(CronSchedule(kind="every", every_ms=60000), now)
        assert result is not None
        assert result >= now

    def test_every_schedule_invalid(self):
        now = _now_ms()
        result = _compute_next_run(CronSchedule(kind="every", every_ms=0), now)
        assert result is None

    def test_cron_schedule(self):
        now = _now_ms()
        result = _compute_next_run(CronSchedule(kind="cron", expr="0 9 * * *"), now)
        assert result is not None
        assert result > now

    def test_unknown_kind(self):
        now = _now_ms()
        result = _compute_next_run(CronSchedule(kind="unknown"), now)
        assert result is None


class TestValidateSchedule:
    def test_valid_every(self):
        _validate_schedule_for_add(CronSchedule(kind="every", every_ms=60000))

    def test_valid_cron(self):
        _validate_schedule_for_add(CronSchedule(kind="cron", expr="0 9 * * *"))

    def test_tz_with_non_cron_raises(self):
        with pytest.raises(ValueError, match="tz can only be used with cron"):
            _validate_schedule_for_add(CronSchedule(kind="every", every_ms=60000, tz="UTC"))

    def test_invalid_tz_raises(self):
        with pytest.raises(ValueError, match="unknown timezone"):
            _validate_schedule_for_add(CronSchedule(kind="cron", expr="0 9 * * *", tz="Invalid/Zone"))


class TestCronService:
    @pytest.mark.asyncio
    async def test_add_job(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        job = svc.add_job(
            name="Test",
            schedule=CronSchedule(kind="every", every_ms=60000),
            message="hello",
        )
        assert job.id
        assert job.name == "Test"
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_list_jobs(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        svc.add_job(name="J1", schedule=CronSchedule(kind="every", every_ms=60000), message="m1")
        svc.add_job(name="J2", schedule=CronSchedule(kind="every", every_ms=120000), message="m2")
        jobs = svc.list_jobs()
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_remove_job(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        job = svc.add_job(name="R1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")
        removed = svc.remove_job(job.id)
        assert removed is True
        assert len(svc.list_jobs()) == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        assert svc.remove_job("no-such-id") is False

    @pytest.mark.asyncio
    async def test_enable_disable_job(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        job = svc.add_job(name="E1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")
        svc.enable_job(job.id, enabled=False)
        assert job.enabled is False
        jobs = svc.list_jobs(include_disabled=True)
        assert len(jobs) == 1
        jobs_active = svc.list_jobs(include_disabled=False)
        assert len(jobs_active) == 0

    @pytest.mark.asyncio
    async def test_get_job(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        job = svc.add_job(name="G1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")
        found = svc.get_job(job.id)
        assert found is not None
        assert found.name == "G1"
        assert svc.get_job("nonexistent") is None

    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        svc.add_job(name="S1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")
        status = svc.status()
        assert "jobs" in status
        assert status["jobs"] == 1

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc1 = CronService(store_path)
        svc1.add_job(name="P1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")

        svc2 = CronService(store_path)
        jobs = svc2.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].name == "P1"

    @pytest.mark.asyncio
    async def test_run_job(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        executed = []

        async def on_job(job):
            executed.append(job.id)
            return "ok"

        svc = CronService(store_path, on_job=on_job)
        job = svc.add_job(name="Run1", schedule=CronSchedule(kind="every", every_ms=60000), message="m")
        result = await svc.run_job(job.id, force=True)
        assert result is True
        assert job.id in executed

    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path)
        await svc.start()
        assert svc._running is True
        svc.stop()
        assert svc._running is False

    @pytest.mark.asyncio
    async def test_delete_after_run(self, tmp_path):
        store_path = tmp_path / "jobs.json"
        executed = []

        async def on_job(job):
            executed.append(job.id)
            return "ok"

        svc = CronService(store_path, on_job=on_job)
        job = svc.add_job(
            name="OneShot",
            schedule=CronSchedule(kind="every", every_ms=60000),
            message="m",
            delete_after_run=True,
        )
        await svc.run_job(job.id, force=True)
        assert len(svc.list_jobs(include_disabled=True)) == 0

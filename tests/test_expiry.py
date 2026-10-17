"""Test the independent two-hour deadline using HA's actual event scheduler."""

import asyncio
from datetime import timedelta

import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mansion_security.api import MansionAuthError, MansionConnectionError, RoomState, Snapshot
from custom_components.mansion_security.const import (
    AUTO_OFF_RETRY_INTERVAL, AUTO_OFF_TIMEOUT, CONF_ALLOW_UNLOCK_MODES,
    CONF_MODE_DEADLINE, CONF_TRACKED_MODE, DOMAIN,
)


@pytest.fixture(autouse=True)
def slow_polling(monkeypatch):
    # A one-day interval proves that timeout closure is independent of polling.
    monkeypatch.setattr("custom_components.mansion_security.coordinator.DEFAULT_UPDATE_INTERVAL", timedelta(days=1))


@pytest.fixture
def cloud(mock_client, presets):
    state = {"mode": "0", "read_error": None, "write_error": None}

    def snapshot():
        return Snapshot(RoomState(state["mode"] != "0", state["mode"] if state["mode"] != "0" else ""), presets)

    async def read():
        if state["read_error"]:
            raise state["read_error"]
        return snapshot()

    async def write(mode, *, allow_unlock=False):
        if state["write_error"]:
            raise state["write_error"]
        state["mode"] = mode
        return snapshot()

    mock_client.async_get_snapshot.side_effect = read
    mock_client.async_set_mode.side_effect = write
    state.update(client=mock_client, snapshot=snapshot, write=write)
    return state


async def setup(hass, session_data, **extra):
    entry = MockConfigEntry(domain=DOMAIN, title="Mansion Security", unique_id="test-user",
                            data={**session_data, **extra}, options={CONF_ALLOW_UNLOCK_MODES: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("select", DOMAIN, "test-user_auto_response")
    return entry, entity_id


async def advance(hass, freezer, delta):
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_closes_at_two_hours_without_waiting_for_poll(hass, session_data, cloud, freezer):
    entry, entity_id = await setup(hass, session_data)
    started = dt_util.utcnow()
    await entry.runtime_data.async_set_mode("24")
    assert entry.runtime_data.auto_off_at == (started + AUTO_OFF_TIMEOUT).isoformat()
    assert hass.states.get(entity_id).attributes["auto_off_at"] == entry.runtime_data.auto_off_at
    reads = cloud["client"].async_get_snapshot.await_count
    await advance(hass, freezer, AUTO_OFF_TIMEOUT - timedelta(milliseconds=1))
    assert cloud["mode"] == "24"
    assert cloud["client"].async_get_snapshot.await_count == reads
    await advance(hass, freezer, timedelta(milliseconds=1))
    assert cloud["mode"] == "0"
    assert hass.states.get(entity_id).state == "off"
    assert entry.runtime_data.auto_off_at is None
    assert CONF_MODE_DEADLINE not in entry.data
    cloud["client"].async_set_mode.assert_awaited_with("0", allow_unlock=False)


async def test_identical_polls_and_repeated_selection_do_not_extend_deadline(hass, session_data, cloud, freezer):
    entry, _ = await setup(hass, session_data)
    coordinator = entry.runtime_data
    await coordinator.async_set_mode("24")
    deadline = coordinator.auto_off_at
    await advance(hass, freezer, timedelta(hours=1))
    await coordinator.async_refresh()
    await coordinator.async_set_mode("24")
    assert coordinator.auto_off_at == deadline
    assert entry.data[CONF_MODE_DEADLINE] == deadline
    await advance(hass, freezer, timedelta(hours=1))
    assert cloud["mode"] == "0"


async def test_mode_change_restarts_deadline_and_cancels_old_timer(hass, session_data, cloud, freezer):
    entry, _ = await setup(hass, session_data)
    coordinator = entry.runtime_data
    await coordinator.async_set_mode("24")
    await advance(hass, freezer, timedelta(hours=1))
    changed = dt_util.utcnow()
    await coordinator.async_set_mode("23")
    assert coordinator.auto_off_at == (changed + AUTO_OFF_TIMEOUT).isoformat()
    await advance(hass, freezer, timedelta(hours=1))
    assert cloud["mode"] == "23"
    await advance(hass, freezer, timedelta(hours=1))
    assert cloud["mode"] == "0"


async def test_manual_off_cancels_timeout(hass, session_data, cloud, freezer):
    entry, _ = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    await entry.runtime_data.async_set_mode("0")
    reads = cloud["client"].async_get_snapshot.await_count
    writes = cloud["client"].async_set_mode.await_count
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert cloud["client"].async_get_snapshot.await_count == reads
    assert cloud["client"].async_set_mode.await_count == writes


async def test_reload_preserves_remaining_time(hass, session_data, cloud, freezer):
    entry, entity_id = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    deadline = entry.runtime_data.auto_off_at
    await advance(hass, freezer, timedelta(minutes=50))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.auto_off_at == deadline
    await advance(hass, freezer, timedelta(minutes=70))
    assert hass.states.get(entity_id).state == "off"
    assert cloud["client"].async_set_mode.await_count == 2


async def test_startup_closes_overdue_unchanged_mode_immediately(hass, session_data, cloud, freezer):
    cloud["mode"] = "24"
    entry, entity_id = await setup(hass, session_data, **{
        CONF_TRACKED_MODE: "24", CONF_MODE_DEADLINE: (dt_util.utcnow() - timedelta(minutes=5)).isoformat(),
    })
    assert cloud["mode"] == "0" and hass.states.get(entity_id).state == "off"
    cloud["client"].async_set_mode.assert_awaited_once_with("0", allow_unlock=False)
    assert entry.runtime_data.auto_off_at is None


async def test_restart_observes_different_mode_before_enforcing_old_deadline(hass, session_data, cloud, freezer):
    cloud["mode"] = "23"
    entry, _ = await setup(hass, session_data, **{
        CONF_TRACKED_MODE: "24", CONF_MODE_DEADLINE: (dt_util.utcnow() - timedelta(minutes=5)).isoformat(),
    })
    assert entry.runtime_data.auto_off_at == (dt_util.utcnow() + AUTO_OFF_TIMEOUT).isoformat()
    assert cloud["mode"] == "23"
    cloud["client"].async_set_mode.assert_not_awaited()


@pytest.mark.parametrize("fail_stage", ["read", "write"])
async def test_expiry_failure_is_unavailable_and_retries_without_faking_off(hass, session_data, cloud, freezer, fail_stage):
    entry, entity_id = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    deadline = entry.runtime_data.auto_off_at
    cloud[fail_stage + "_error"] = MansionConnectionError("Network unavailable")
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert cloud["mode"] == "24"
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert entry.runtime_data.auto_off_at == deadline
    cloud[fail_stage + "_error"] = None
    await advance(hass, freezer, AUTO_OFF_RETRY_INTERVAL - timedelta(seconds=1))
    assert cloud["mode"] == "24"
    await advance(hass, freezer, timedelta(seconds=1))
    assert cloud["mode"] == "0" and hass.states.get(entity_id).state == "off"


async def test_auth_failure_keeps_deadline_and_starts_reauth(hass, session_data, cloud, freezer):
    entry, entity_id = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    deadline = entry.runtime_data.auto_off_at
    cloud["read_error"] = MansionAuthError(400, "invalidUser")
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert entry.data[CONF_MODE_DEADLINE] == deadline
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows)
    reads = cloud["client"].async_get_snapshot.await_count
    await advance(hass, freezer, AUTO_OFF_RETRY_INTERVAL)
    assert cloud["client"].async_get_snapshot.await_count == reads


async def test_cloud_change_at_deadline_is_not_disabled_by_stale_timer(hass, session_data, cloud, freezer):
    entry, entity_id = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    cloud["mode"] = "23"
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert cloud["mode"] == "23"
    assert hass.states.get(entity_id).state.endswith("（自动开门）")
    assert entry.runtime_data.auto_off_at == (dt_util.utcnow() + AUTO_OFF_TIMEOUT).isoformat()
    assert cloud["client"].async_set_mode.await_count == 1


async def test_mode_change_in_flight_wins_over_queued_expiry(hass, session_data, cloud, freezer):
    entry, _ = await setup(hass, session_data)
    coordinator = entry.runtime_data
    await coordinator.async_set_mode("24")
    await advance(hass, freezer, AUTO_OFF_TIMEOUT - timedelta(seconds=1))
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(mode, *, allow_unlock=False):
        if mode == "23":
            started.set()
            await release.wait()
        return await cloud["write"](mode, allow_unlock=allow_unlock)

    cloud["client"].async_set_mode.side_effect = delayed_write
    task = hass.async_create_task(coordinator.async_set_mode("23"))
    await started.wait()
    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.sleep(0)
    release.set()
    await task
    await hass.async_block_till_done()
    assert cloud["mode"] == "23"
    assert all(call.args != ("0",) for call in cloud["client"].async_set_mode.await_args_list)


async def test_unload_cancels_timer_but_keeps_deadline_for_restart(hass, session_data, cloud, freezer):
    entry, _ = await setup(hass, session_data)
    await entry.runtime_data.async_set_mode("24")
    deadline = entry.data[CONF_MODE_DEADLINE]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert entry.data[CONF_MODE_DEADLINE] == deadline
    assert cloud["mode"] == "24"
    assert cloud["client"].async_set_mode.await_count == 1


@pytest.mark.parametrize("mode", ["24", "23", "unknown-preset"])
async def test_first_observation_of_enabled_mode_starts_deadline(hass, session_data, cloud, freezer, mode):
    cloud["mode"] = mode
    entry, _ = await setup(hass, session_data)
    assert entry.runtime_data.auto_off_at == (dt_util.utcnow() + AUTO_OFF_TIMEOUT).isoformat()
    await advance(hass, freezer, AUTO_OFF_TIMEOUT)
    assert cloud["mode"] == "0"

"""Receive AI-confirmed Blue Iris alerts and expose an authenticated timeline."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from aiohttp import web
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.components.mqtt.client import async_subscribe
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .store import AlertStore, CATEGORIES, DEFAULT_LABELS, MAX_PAYLOAD, RejectedAlert

DOMAIN = "blue_iris_timeline"
_LOGGER = logging.getLogger(__name__)


def publish_topic(value):
    value = cv.string(value).strip("/")
    if not value or any(c in value for c in "+#\x00"):
        raise vol.Invalid("Use a topic without MQTT wildcards")
    return value


CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({
    vol.Optional("directory", default="/media/blue_iris_timeline"): cv.string,
    vol.Optional("mqtt_topic"): publish_topic,
    # v1.0.0 compatibility: only used when mqtt_topic is not configured.
    vol.Optional("mqtt_topic_prefix"): publish_topic,
    vol.Optional("retention_days", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
    vol.Optional("max_storage_mb", default=2048): vol.All(vol.Coerce(int), vol.Range(min=16, max=1048576)),
    vol.Optional("camera_names", default={}): {cv.string: cv.string},
    vol.Optional("labels", default={}): {
        vol.In(CATEGORIES): vol.All(cv.ensure_list, [cv.string]),
    },
})}, extra=vol.ALLOW_EXTRA)


class IndexView(HomeAssistantView):
    url = "/api/blue_iris_timeline"
    name = "api:blue_iris_timeline:index"
    requires_auth = True

    def __init__(self, hass, store, runtime):
        self.hass, self.store, self.runtime = hass, store, runtime

    async def get(self, request):
        q = request.query
        try:
            result = await self.hass.async_add_executor_job(
                self.store.list_events, q.get("category", "people"), q.get("date"),
                q.get("camera", ""), q.get("before"), int(q.get("limit", "80")),
            )
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        result["status"].update(self.runtime)
        return web.json_response(result, headers={"Cache-Control": "no-store"})


class ImageView(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass, store, small=False):
        self.hass, self.store, self.small = hass, store, small
        kind = "thumbnail" if small else "image"
        self.url = f"/api/blue_iris_timeline/{kind}/{{ident}}"
        self.name = f"api:blue_iris_timeline:{kind}"

    async def get(self, request, ident):
        path = await self.hass.async_add_executor_job(self.store.image_path, ident, self.small)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, no-store",
                                              "X-Content-Type-Options": "nosniff"})


async def async_setup(hass, config):
    cfg = config[DOMAIN]
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.error("Set up the Home Assistant MQTT integration before Blue Iris AI Timeline")
        return False
    labels = {**DEFAULT_LABELS, **cfg["labels"]}
    store = AlertStore(cfg["directory"], cfg["retention_days"], cfg["max_storage_mb"], cfg["camera_names"], labels)
    await hass.async_add_executor_job(store.initialize)
    runtime = {"received": 0, "saved": 0, "duplicates": 0, "rejected": 0,
               "retained_ignored": 0, "dropped": 0, "queue_error": None}
    queue = asyncio.Queue(maxsize=8)
    pending_bytes = 0
    legacy_topic = "mqtt_topic" not in cfg and "mqtt_topic_prefix" in cfg
    topic = (f"{cfg['mqtt_topic_prefix']}/+" if legacy_topic
             else cfg.get("mqtt_topic", "blueiris/timeline"))
    if legacy_topic:
        _LOGGER.warning("mqtt_topic_prefix is deprecated; switch HA to mqtt_topic: blueiris/timeline "
                        "and publish each camera's alert to blueiris/timeline")

    @callback
    def receive(message):
        nonlocal pending_bytes
        # Never replay the broker's single retained snapshot as a new alert.
        if message.retain:
            runtime["retained_ignored"] += 1
            return
        runtime["received"] += 1
        payload = message.payload
        size = len(payload)
        if size > MAX_PAYLOAD or queue.full() or pending_bytes + size > 32 * 1024 * 1024:
            runtime["dropped"] += 1
            runtime["queue_error"] = "An alert was dropped: input exceeded the 12 MiB message / 32 MiB queue limit."
            _LOGGER.error("Blue Iris alert dropped: MQTT message/queue size limit reached")
            return
        pending_bytes += size
        # All cameras publish to one topic; the JSON camera field identifies them.
        # A legacy per-camera topic still has to agree with the payload camera.
        topic_camera = message.topic.rsplit("/", 1)[-1] if legacy_topic else None
        queue.put_nowait((payload, topic_camera, size))

    async def worker():
        nonlocal pending_bytes
        while True:
            payload, camera, size = await queue.get()
            try:
                outcome = await hass.async_add_executor_job(
                    store.ingest, payload, camera, hass.config.time_zone, labels)
                runtime["saved" if outcome == "saved" else "duplicates"] += 1
            except RejectedAlert as err:
                runtime["rejected"] += 1
                # Log reason only; never print an image payload or private snapshot.
                _LOGGER.warning("Blue Iris timeline skipped an event: %s", err)
                try:
                    await hass.async_add_executor_job(lambda reason=str(err): store.record_status(last_error=reason))
                except Exception:
                    _LOGGER.exception("Could not write timeline diagnostics")
            except Exception:
                runtime["rejected"] += 1
                _LOGGER.exception("Could not save a Blue Iris timeline alert")
                try:
                    await hass.async_add_executor_job(
                        lambda: store.record_status(last_error="Could not write alert. Check HA logs and free disk space."))
                except Exception:
                    _LOGGER.exception("Could not write timeline diagnostics")
            finally:
                pending_bytes -= size
                queue.task_done()

    task = hass.async_create_background_task(worker(), "Blue Iris timeline writer")
    try:
        unsubscribe = await async_subscribe(hass, topic, receive, qos=1, encoding=None)
    except Exception:
        task.cancel()
        raise
    hass.http.register_view(IndexView(hass, store, runtime))
    hass.http.register_view(ImageView(hass, store))
    hass.http.register_view(ImageView(hass, store, True))

    async def cleanup(_now):
        await hass.async_add_executor_job(store.cleanup)
    remove_timer = async_track_time_interval(hass, cleanup, timedelta(hours=1))

    async def stop(_event):
        unsubscribe()
        remove_timer()
        # Give accepted messages time to finish before the application stops.
        try:
            async with asyncio.timeout(15):
                await queue.join()
        finally:
            task.cancel()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    hass.data[DOMAIN] = {"store": store, "runtime": runtime, "worker": task}
    _LOGGER.info("Blue Iris AI Timeline ready; subscribed to %s", topic)
    return True

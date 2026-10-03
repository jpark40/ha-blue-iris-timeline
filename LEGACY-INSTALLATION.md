# Blue Iris AI Timeline for Home Assistant — v1.1.0

Four tabs: **People · Animals · Cars · Delivery**. Based on the layout of your Front Door Timeline v1.0.4: date navigation, large selected image, horizontal thumbnails, and full-size image view. The full-screen viewer supports swiping between snapshots, pinch-to-zoom, and drag-to-pan while zoomed. Adds all-camera history, camera filtering, AI labels/confidence, and small thumbnails for tablets.

This is an installation package, not a change already deployed to your HA. It collects **new alerts after setup** from every camera with the MQTT action below. It does not backfill the existing Blue Iris database or record video. Your Blue Iris Windows VM's address is not needed.

Follow **UPGRADE.md** to update an existing installation. This v1.1.0 update keeps your saved history and adds the Delivery tab. MQTT status sensors and the earlier connection errors are covered in **MQTT-TROUBLESHOOTING.md**.

## 1. Copy two items into Home Assistant

Extract this ZIP on your computer. Copy the contents as follows using your usual HA file editor or Samba share:

| Item inside ZIP | Destination inside HA |
|---|---|
| `custom_components/blue_iris_timeline/` (the whole folder) | `/config/custom_components/blue_iris_timeline/` |
| `www/blue-iris-timeline-card.js` | `/config/www/blue-iris-timeline-card.js` |

The final Python file must be `/config/custom_components/blue_iris_timeline/__init__.py`, without an extra enclosing ZIP folder. This component has a separate name and data folder from `front_door_timeline`.

## 2. Enable the receiver

Your Home Assistant MQTT integration must already connect to the broker used by Blue Iris. Your previous setup used the broker at **192.168.1.149**. Reuse that connection and its credentials.

Merge this block at the top level of `/config/configuration.yaml`:

```yaml
blue_iris_timeline:
  directory: /media/blue_iris_timeline
  mqtt_topic: blueiris/timeline
  retention_days: 30
  max_storage_mb: 2048
```

Also merge `examples/blueiris-sensors.yaml` under your existing `mqtt:` configuration. It adds **Blue Iris Status** from `blueiris/app` and **Blue Iris MQTT Heartbeat** from `blueiris/keepalive`. In BI MQTT server settings, enable LWT and set Publish / keepalive to **20 seconds**. The heartbeat turns off after 90 seconds without another message. These sensors monitor status; they do not fix broker connection failures. See `UPGRADE.md` for troubleshooting. The combined receiver-and-sensors example is `examples/configuration.yaml`; do not duplicate the sensor entries.

Check your HA configuration, then **restart Home Assistant**. The integration creates its storage directory. This is YAML setup; there is no separate Add Integration/config-flow step. No MQTT camera entities, snapshot automations, LLM provider, or extra Docker container are required.

For Home Assistant Container, persist `/media` in a volume. HA OS already provides persistent `/media`. Snapshots and the SQLite index are written to disk, not `/tmp`. If using another directory, choose a writable persistent disk path.

Defaults keep images for up to 30 days or 2048 MiB, whichever limit is reached first. The oldest timeline images are removed when either limit is reached. The small SQLite index is additional to the image limit. Cleanup is confined to this integration's own archived files and does not delete Blue Iris recordings.

## 3. Add the Blue Iris MQTT action on each camera

In **Blue Iris**, confirm its MQTT server is your existing HA broker. For every camera you want in the timeline, open the camera settings and locate its AI/alert-confirmation configuration and **Alerts → On alert** action list. UI wording varies between BI versions.

1. Keep AI confirmation enabled for the objects you want the camera to confirm. Configure alert actions to fire **only when confirmed**. Do not attach the timeline message to immediate trigger, canceled-alert, or reset actions.
2. Add a new **MQTT** action to **On alert**. Use the topic and payload below.
3. Turn **Retain off**. Use **QoS 1** if your BI version offers it. The receiver de-duplicates repeated deliveries of the same camera, alert locator, and timestamp.
4. Make this particular action apply to every desired AI object. Do not inherit a delivery-only or cat-only action filter if you want other categories. Enable it in each camera profile in which you want history.
5. Repeat for all cameras. `&CAM` supplies the camera short name automatically.

**Topic** — copy exactly:

```text
blueiris/timeline
```

Every camera publishes to that exact topic. `&CAM` stays in the JSON payload; there is no camera suffix on the topic.

**Post/payload** — copy as one message:

```json
{"confirmed":true,"camera":"&CAM","memo":"&MEMO","alert_id":"&ALERT_DB","timestamp":"&ALERT_TIME","image_b64":"&ALERT_JPEG"}
```

`confirmed: true` declares that this action came from your confirmed-alert action list; it does not query or independently prove Blue Iris's confirmation state. The BI confirmation gate in step 1 is essential. The receiver also requires a scored, recognized AI label in the memo and a valid alert image. It rejects plain motion, empty AI results, malformed images, and retained-message replays.

The image and metadata travel in the same MQTT message, avoiding mismatches between separate latest-image and latest-event topics. The stored picture is the BI alert-list JPEG. It is not a newly captured live camera frame. Its resolution and any detection boxes are whatever Blue Iris supplies.

Your existing delivery and cat notification MQTT actions can remain in the same action list. Add this as its own action. A camera configured to confirm only cats will still only produce cat-confirmed events until its BI confirmation configuration includes other desired objects. This dashboard cannot recover alerts that BI canceled or never exported.

## 4. Register the dashboard resource

In Home Assistant's dashboard **Resources** screen (usually Settings → Dashboards → menu → Resources; enable Advanced Mode in your profile if needed), add:

- URL: `/local/blue-iris-timeline-card.js?v=1.1.0`
- Type: **JavaScript module**

If you manage resources in YAML, add the equivalent item under your existing `lovelace.resources` list:

```yaml
- url: /local/blue-iris-timeline-card.js?v=1.1.0
  type: module
```

Use the resource method already used by your installation. Refresh the browser after adding it. The card is added through Manual YAML, matching the approach in your working front-door card.

## 5. Add the Timeline page

**New dashboard:** create an empty dashboard named **Blue Iris AI Timeline**, open its **Raw configuration editor**, and paste `examples/dashboard.yaml`. That creates one full-width Timeline page containing the four category tabs.

**Existing Timeline page:** add a **Manual** card and paste `examples/card.yaml`:

```yaml
type: custom:blue-iris-timeline
title: Blue Iris AI Timeline
initial_category: people
refresh_seconds: 15
thumbnail_width: 170
page_size: 80
```

Use a Panel view for the same full-width layout as the front-door timeline. Do not replace the entire raw configuration of an existing dashboard with the new-dashboard example; add the card or view to it.

## Using the timeline

- **People / Animals / Cars / Delivery:** the badges count that day's matching alerts for the selected camera filter. One event can count in multiple tabs; its JPEG is stored only once.
- **Camera:** all cameras initially; names appear as their first alert is received.
- **Date / left and right arrows:** browse dates with saved alerts. Dates and times use HA's configured time zone, so keep it set to `America/Los_Angeles` for your home.
- **Latest:** jump to the newest date and resume following new alerts, including after midnight. Browsing an older date pauses that behavior. A selected older snapshot stays selected during refresh.
- **Thumbnail strip:** oldest to newest from left to right; starts at the newest image. Tap any thumbnail or use snapshot arrows. Only the thumbnail strip scrolls.
- **Older alerts / Newer alerts:** browse busy days in pages of 80. The page size is a display limit, not a daily storage limit.
- **Full-screen image:** tap or press Enter to enlarge. Swipe left/right or use the arrow buttons/keys to change snapshots; navigation also crosses alert pages. Pinch to zoom on a tablet, then drag to pan. Use Reset zoom or press 0 to return to the full image. Close with the button or Escape.

The card requests small thumbnails and loads the full-size image only when selected. It pauses periodic refresh in a hidden browser tab and while the full-size viewer is open. All index and image endpoints require HA authentication. No BI password or HA access token is embedded in the JavaScript. Images are accessible to authenticated HA users; there are no per-camera user permissions in this custom component.

## Categories and custom names

The default map uses the exact scored labels in Blue Iris's memo. No second confidence threshold or new AI analysis is applied.

| Tab | Common included labels |
|---|---|
| People | person, people, human |
| Animals | cat, dog, bird, deer, bear, fox, raccoon, squirrel, skunk, opossum, coyote, rabbit, horse, cow, and other labels listed in `store.py` |
| Cars | car, truck, bus, vehicle, van, SUV, pickup, motorcycle, motorbike, delivery truck/van, Amazon, FedEx, UPS, USPS, DHL |
| Delivery | delivery, delivery person/truck/van, courier, package, parcel, Amazon, FedEx/Fed Ex, UPS, USPS, DHL |

Delivery-brand labels appear in both Cars and Delivery. A generic person, car, or truck label alone does not imply a delivery; customize the Delivery labels if your model uses other names. A historical database migration applies the configured Delivery labels to saved scored memos when HA starts after this upgrade, while preserving each alert and image. If your model uses different labels, override the relevant list in `configuration.yaml`. An overridden category replaces that category's whole default list; other categories keep their defaults:

```yaml
blue_iris_timeline:
  directory: /media/blue_iris_timeline
  retention_days: 30
  max_storage_mb: 2048
  camera_names:
    FrontRight: Front Right
    BackYard: Back Yard
  labels:
    animals: [cat, dog, bird, deer, raccoon, skunk, coyote, rabbit, squirrel]
    delivery: [delivery, courier, package, parcel, amazon, fedex, ups, usps, dhl]
```

Replace `FrontRight` and `BackYard` with your actual BI short names. These are examples, not discovered camera identifiers. Merge these options into your one `blue_iris_timeline` block and restart HA. Category changes affect subsequently received alerts. Short names and custom labels inserted by BI into the JSON payload should not contain literal double quotes, backslashes, or newlines, as BI macro substitution does not JSON-escape those characters.

## Check the first real alert

Generate a real AI-confirmed event (for example, walk through a camera view). Within roughly 15 seconds after BI publishes it, the relevant tab should show the event. A generic BI action-list Test button may lack the alert locator, image, or AI memo; it does not establish that a real confirmed-alert action works.

If empty, use your HA MQTT integration's **Listen to a topic** with `blueiris/timeline`. Confirm a real event arrives with `camera`, scored `memo`, expanded `alert_id`, ISO `timestamp`, and a long base64 `image_b64` value. Stop the listener afterward; image messages are large. You do not need to paste the base64 data anywhere.

| Symptom | Check |
|---|---|
| Custom element doesn't exist | Check the JS location, resource type, and browser cache; reload HA in the browser. |
| Timeline unavailable / HTTP 404 | Confirm the component folder and YAML block, restart HA, and inspect Settings → System → Logs for `blue_iris_timeline`. |
| No MQTT messages | Check BI broker connection, active camera profile, AI confirmation, and the new On alert action. |
| Only delivery or cats appear | Check the camera's AI confirmation labels and this action's object/profile filters. |
| Receiver says no recognized scored AI label | Check actual memo text and category label mapping. Expected examples: `person:91%`, `cat:86%, car:88%`. |
| Receiver says invalid JPEG / locator | Ensure macros expand on a real saved alert. Confirm the full JSON payload reaches MQTT without truncation. BI must provide an alert-list image. |
| Several cameras missing | Add the action to each camera, in the active profiles. The front-door Eufy image entity is not this receiver's source. |
| Date looks wrong | Check HA time zone and BI Windows clock. Future-dated events beyond five minutes are rejected. |

MQTT is an event stream, not a historical database sync. Alerts published while HA/the broker is unavailable, suppressed by BI action limits, or dropped under an excessive input burst cannot be reconstructed by this package. The receiver reports input/storage failures rather than filling gaps with a live image. Keep Retain off; retained MQTT stores only one last message per topic and is not a history mechanism.

## Validation and maintenance

The package includes parser/storage tests: `python3 -m unittest discover -s tests -v` from the extracted folder, with Pillow installed. These cover category matching, mixed events, invalid inputs, local dates, de-duplication across restarts, history migration, paging, image bounds, disk limits, and cleanup isolation. JavaScript checks run with `node --test tests/test_card.js`. See `VALIDATION.md` for actual checks completed for this release. Live BI delivery and your exact HA version still need the first real alert check above.

To remove the dashboard, remove its card/view and resource. To disable collection, remove the YAML block and restart HA. Stored history remains in the configured directory until you explicitly remove it. Include that directory in whatever backup scope you use if you want this history backed up.

Reference documentation checked for this package:

- Blue Iris user manual (confirmation behavior and alert macros): https://blueirissoftware.com/blueiris.pdf
- Home Assistant custom cards and resources: https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card/
- Home Assistant MQTT client API: https://github.com/home-assistant/core/blob/dev/homeassistant/components/mqtt/client.py
- Home Assistant MQTT sensors: https://www.home-assistant.io/integrations/sensor.mqtt/
- Home Assistant MQTT binary sensors: https://www.home-assistant.io/integrations/binary_sensor.mqtt/
- Home Assistant integration dependencies: https://developers.home-assistant.io/docs/creating_integration_manifest/

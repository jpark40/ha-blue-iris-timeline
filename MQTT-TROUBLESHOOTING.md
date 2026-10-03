# MQTT status sensors and troubleshooting

## Added sensors

| Expected entity ID | Topic | Meaning |
|---|---|---|
| `sensor.blue_iris_status` | `blueiris/app` | Last published app state, such as `running`, `stopped`, or `unexpected stop`. |
| `binary_sensor.blue_iris_mqtt_heartbeat` | `blueiris/keepalive` | Connected after a heartbeat; Disconnected after 90 seconds without another. |

Entity IDs may have a numeric suffix if the names are already in use. The
heartbeat may start as Unknown until its first message. A retained app state
alone does not prove the server is currently reachable; use the heartbeat
alongside it. A heartbeat shows recent message delivery, not that every camera
or AI job is healthy. HA's own broker connection also affects availability.

Blue Iris documents an empty keepalive payload. The heartbeat template converts
any received payload into `ON`. If it stays Unknown, listen to `blueiris/#` and
check the actual keepalive topic and publish interval. If your server publishes
`BlueIris/...` instead, change the sensor topics to that exact capitalization.

## The errors in your screenshot

Adding an HA sensor subscribes to messages. It cannot repair a failed MQTT
connection or publish. MQTT does not require a subscriber for a publish to
succeed. Your screenshot shows connection failures at **06:26**, followed by
**Connect: OK at 06:33:21** and a successful delivery-event publish at
**06:35:13**. That sequence shows recovery by the time of the screenshot.

- Windows socket error **10053** means an established connection was aborted
  by software on the host, possibly because of a timeout or protocol error.
- Mosquitto return code **7** means a lost connection. The screenshot alone
  does not establish what caused the disconnect or how BI combines its codes.
- The paired value **126** does not establish a root cause from this log alone.

If failures recur, check the Mosquitto log for the same timestamps. Confirm
that Blue Iris has a unique MQTT client ID, its broker address/port and
credentials match HA's working broker, and the VM can still reach it. If the
client ID must change, recheck the emitted app/keepalive topics afterward.
The screenshot does not justify changing credentials or deleting existing
working delivery/cat actions. The keepalive setting can help with idle
disconnects; it is not a guaranteed repair for every connection failure.

References:

- [Blue Iris manual: MQTT app, keepalive, and LWT](https://blueirissoftware.com/BlueIris.PDF)
- [Home Assistant MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/)
- [Home Assistant MQTT binary sensor](https://www.home-assistant.io/integrations/binary_sensor.mqtt/)
- [Windows socket error codes](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2)
- [Mosquitto maintainer explanation of return code 7](https://www.eclipse.org/lists/mosquitto-dev/msg00423.html)

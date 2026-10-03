# Upgrade to v1.1.0

This update adds a **Delivery** tab, swipe navigation and pinch-to-zoom in the
full-screen image viewer. Your existing alert archive remains in place. On the
first HA startup, the integration adds a Delivery flag and classifies existing
saved AI memo labels into the new tab; saved image files and alert IDs stay the
same. New alerts use the same `blueiris/timeline` MQTT topic and your current
confirmation action.

## Apply the update

Keep a backup of your integration files and configured archive directory before
upgrading. Rolling back to v1.0.x requires restoring its archive index too.

1. Copy the new `custom_components/blue_iris_timeline/` folder over the one at
   `/config/custom_components/blue_iris_timeline/` and the new
   `www/blue-iris-timeline-card.js` to `/config/www/`.
2. Restart Home Assistant. Leave `/media/blue_iris_timeline` and your existing
   configuration alone. The delivery history migration runs once at startup.
3. In Dashboard **Resources**, edit the existing resource (JavaScript module) to
   `/local/blue-iris-timeline-card.js?v=1.1.0`. Reload the HA page in Fully Kiosk.
   If it still shows three tabs, clear Fully Kiosk's web cache and reload.
4. Open the Delivery tab and test a saved alert with a delivery-related scored
   AI label. In the full-screen viewer, swipe horizontally between snapshots;
   pinch to zoom and drag the zoomed image. Arrow buttons and keys also navigate.

Delivery brand labels (Amazon, FedEx, UPS, USPS, DHL) appear under **Cars** and
**Delivery**. Explicit delivery, courier, package, and parcel labels appear
under **Delivery**. A person, car, or truck by itself does not count as a
delivery. Custom `labels.delivery` replaces the Delivery defaults and is also
used to classify the existing history during migration.

If you skipped the v1.0.1 topic or sensor update, follow the appropriate MQTT
setup sections in `README.md`. This v1.1.0 update itself does not change your
MQTT topic, sensors, or camera action.

Full-screen navigation follows the current date, camera filter, and category.
Swipe left for the next/newer snapshot and right for the previous/older one.
At the end of an alert page it loads the neighboring page automatically; it does
not jump to a different date. Zoom ranges from 1× to 8×. At more than 1×, dragging
pans the image; use the arrow buttons or reset zoom before swiping to another
snapshot. Each new image starts at 1×. Desktop mouse-wheel zoom is also supported.

Only the selected full-resolution image is kept in the browser cache alongside
the current page's thumbnails. Zoom operates on that image on the tablet; it
does not create another HA image archive or run another AI model.

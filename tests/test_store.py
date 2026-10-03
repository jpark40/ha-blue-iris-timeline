"""Run: python3 -m unittest discover -s tests -v (Pillow required)."""
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

spec = importlib.util.spec_from_file_location("bi_store", Path(__file__).resolve().parents[1] / "custom_components/blue_iris_timeline/store.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def image_bytes():
    buf = BytesIO()
    Image.new("RGB", (640, 360), "navy").save(buf, format="JPEG")
    return buf.getvalue()


def message(**overrides):
    values = dict(confirmed=True, camera="Front", memo="person:91%, car:87%",
                  alert_id="@1234", timestamp=datetime.now(timezone.utc).isoformat(),
                  image_b64=base64.b64encode(image_bytes()).decode())
    values.update(overrides)
    return json.dumps(values).encode()


class ClassifierTests(unittest.TestCase):
    def test_multicategory_and_case(self):
        self.assertEqual(mod.classify("Person:91%, CAT:86%, truck:88%")[0], ["people", "animals", "cars"])
        self.assertEqual(mod.classify("Person:91%, CAT:86%, truck:88%, UPS:74%")[0], list(mod.CATEGORIES))

    def test_exact_label_boundaries(self):
        self.assertEqual(mod.classify("carpet:99%, hot dog:90%, personal:99%")[0], [])

    def test_delivery_label(self):
        for label in ["Amazon", "FedEx", "UPS", "USPS", "DHL", "delivery-truck", "delivery_van"]:
            with self.subTest(label=label):
                self.assertEqual(mod.classify(f"{label}:91%")[0], ["cars", "delivery"])
        for label in ["delivery", "delivery person", "courier", "package", "parcel", "fed ex"]:
            with self.subTest(label=label):
                self.assertEqual(mod.classify(f"{label}:91%")[0], ["delivery"])

    def test_delivery_requires_scored_exact_label(self):
        for memo in ["delivery", "UPS:0%", "package:110%", "not ups:90%", "upsized:80%",
                     "person:91%, car:90%, truck:85%"]:
            with self.subTest(memo=memo):
                self.assertNotIn("delivery", mod.classify(memo)[0])

    def test_delivery_custom_labels_and_delivery_only_alert(self):
        labels = {**mod.DEFAULT_LABELS, "delivery": ["postal worker"]}
        meta, _ = mod.parse_alert(message(memo="Postal_Worker:88%"), None, "UTC", labels)
        self.assertEqual(meta["categories"], ["delivery"])
        self.assertNotIn("delivery", mod.classify("UPS:91%", labels)[0])

    def test_bad_scores_and_plain_motion(self):
        for memo in ["motion", "person", "person:0%", "person:110%", "Nothing found"]:
            self.assertEqual(mod.classify(memo)[0], [])

    def test_custom_labels(self):
        self.assertEqual(mod.classify("wild-turkey:80%", {**mod.DEFAULT_LABELS, "animals": ["wild turkey"]})[0], ["animals"])

    def test_timezone_local_day_and_offset(self):
        meta, _ = mod.parse_alert(message(timestamp="2026-09-25T02:15:00Z"), "Front", "America/Los_Angeles", now=1e12)
        self.assertEqual(meta["date"], "2026-09-24")
        self.assertEqual(meta["time"], "19:15:00")
        self.assertTrue(meta["timestamp"].endswith("-07:00"))

    def test_input_rejections(self):
        for override in [{"confirmed": False}, {"confirmed": "true"}, {"camera": "Back"},
                         {"image_b64": "&ALERT_JPEG"}, {"alert_id": "@-1"},
                         {"timestamp": "&ALERT_TIME"}, {"memo": "Nothing found"}]:
            with self.subTest(override=override), self.assertRaises(mod.RejectedAlert):
                mod.parse_alert(message(**override), "Front", "America/Los_Angeles")

    def test_future_time(self):
        with self.assertRaises(mod.RejectedAlert):
            mod.parse_alert(message(timestamp=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()), "Front", "UTC")

    def test_shared_topic_requires_valid_payload_camera(self):
        for camera in [None, "", " ", "&CAM", "front/back", "+", "#", "front\x00"]:
            with self.subTest(camera=camera), self.assertRaises(mod.RejectedAlert):
                mod.parse_alert(message(camera=camera), None, "UTC")

    def test_invalid_json_and_corrupt_image(self):
        for payload in [b"not-json", b"[]"]:
            with self.assertRaises(mod.RejectedAlert): mod.parse_alert(payload, "Front", "UTC")
        with self.assertRaises(mod.RejectedAlert): mod.thumbnail(b"\xff\xd8badjpeg")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = mod.AlertStore(self.tmp.name, camera_names={"Front": "Front Yard"})
        self.store.initialize()

    def save(self, **values):
        payload = message(**values)
        return self.store.ingest(payload, values.get("camera", "Front"), "America/Los_Angeles", mod.DEFAULT_LABELS)

    def test_multicategory_and_dedupe_survives_restart(self):
        data = message()
        self.store.ingest(data, "Front", "America/Los_Angeles", mod.DEFAULT_LABELS)
        restarted = mod.AlertStore(self.tmp.name)
        restarted.initialize()
        self.assertEqual(restarted.ingest(data, "Front", "America/Los_Angeles", mod.DEFAULT_LABELS), "duplicate")
        p = self.store.list_events("people")
        c = self.store.list_events("cars")
        self.assertEqual(p["images"][0]["id"], c["images"][0]["id"])
        self.assertEqual(p["total_count"], 1)
        self.assertEqual(p["counts"], {"people": 1, "animals": 0, "cars": 1, "delivery": 0})
        self.assertEqual(p["images"][0]["camera_name"], "Front Yard")

    def test_camera_filter_and_thumbnail(self):
        self.save()
        self.save(camera="Back", memo="cat:86%")
        p = self.store.list_events("people", camera="Back")
        self.assertEqual(p["images"], [])
        self.assertEqual(p["counts"]["animals"], 1)
        meta = self.store.list_events("animals")["images"][0]
        with Image.open(self.store.image_path(meta["id"], True)) as im:
            self.assertLessEqual(im.width, 400)

    def test_delivery_tab_counts_and_single_image_storage(self):
        self.save(memo="ups:74%,car:95%")
        self.save(camera="Back", memo="package:86%", alert_id="@package")
        delivery = self.store.list_events("delivery")
        self.assertEqual(delivery["counts"]["delivery"], 2)
        self.assertEqual(delivery["total_count"], 2)
        self.assertEqual(len(list((Path(self.tmp.name) / "images").glob("*.jpg"))), 2)
        cars = self.store.list_events("cars")["images"]
        self.assertEqual(cars[0]["id"], next(i["id"] for i in delivery["images"] if i["camera"] == "Front"))
        self.assertEqual(len(self.store.list_events("delivery", camera="Back")["images"]), 1)

    def test_shared_topic_distinguishes_cameras_with_same_alert_locator(self):
        stamp = datetime.now(timezone.utc).isoformat()
        for camera in ["fr", "fl", "bl"]:
            self.assertEqual(self.store.ingest(message(camera=camera, timestamp=stamp), None,
                                              "America/Los_Angeles", mod.DEFAULT_LABELS), "saved")
        result = self.store.list_events("people")
        self.assertEqual(result["total_count"], 3)
        self.assertEqual({row["camera"] for row in result["images"]}, {"fr", "fl", "bl"})
        self.assertEqual(len({row["id"] for row in result["images"]}), 3)

    def test_migration_to_shared_topic_deduplicates_existing_history(self):
        data = message(camera="fr")
        self.assertEqual(self.store.ingest(data, "fr", "UTC", mod.DEFAULT_LABELS), "saved")
        self.assertEqual(self.store.ingest(data, None, "UTC", mod.DEFAULT_LABELS), "duplicate")
        self.assertEqual(self.store.list_events("people")["total_count"], 1)

    def test_pagination_tied_timestamps(self):
        stamp = datetime.now(timezone.utc).isoformat()
        for i in range(7): self.save(alert_id=f"@{i}", timestamp=stamp)
        seen, before = [], None
        while True:
            page = self.store.list_events("people", before=before, limit=2)
            seen += [x["id"] for x in page["images"]]
            if not page["has_more"]: break
            before = page["next_before"]
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)

    def test_path_traversal_and_sql_injection_rejected(self):
        self.assertIsNone(self.store.image_path("../../etc/passwd"))
        self.assertIsNone(self.store.image_path("0" * 64))
        for kwargs in [{"category": "people; DROP TABLE alerts"}, {"category": "people", "before": "x' OR 1=1"}, {"category": "people", "day": "bad"}, {"category": "people", "limit": 201}]:
            with self.assertRaises(ValueError): self.store.list_events(**kwargs)
        self.assertEqual(self.store.list_events("people", camera="' OR 1=1")["images"], [])

    def test_retention_removes_only_own_files(self):
        unrelated = Path(self.tmp.name) / "images" / "family.jpg"
        unrelated.write_bytes(b"keep me")
        self.save()
        self.assertEqual(self.store.list_events("people")["total_count"], 1)
        with patch.object(mod.time, "time", return_value=mod.time.time() + 31 * 86400):
            self.assertEqual(self.store.cleanup(), 1)
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.store.list_events("people")["total_count"], 0)

    def test_capacity_evicts_oldest(self):
        self.save(alert_id="@old", timestamp=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        size = self.store.list_events("people")["storage_bytes"]
        self.store.max_bytes = size + 10
        self.save(alert_id="@new")
        p = self.store.list_events("people")
        self.assertEqual(p["total_count"], 1)
        self.assertEqual(p["images"][0]["alert_id"], "@new")

    def test_corrupt_image_leaves_no_db_entry(self):
        with self.assertRaises(mod.RejectedAlert): self.save(image_b64=base64.b64encode(b"\xff\xd8badjpeg").decode())
        self.assertEqual(self.store.list_events("people")["total_count"], 0)

    def test_interrupted_write_orphan_cleanup(self):
        (Path(self.tmp.name) / "images" / ("a" * 64 + ".jpg")).write_bytes(b"orphan")
        self.store.initialize()
        self.assertFalse((Path(self.tmp.name) / "images" / ("a" * 64 + ".jpg")).exists())


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "images").mkdir()
        (self.root / "thumbs").mkdir()
        with sqlite3.connect(self.root / "timeline.sqlite3") as db:
            db.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY,epoch REAL NOT NULL,day TEXT NOT NULL,"
                       "camera TEXT NOT NULL,people INTEGER NOT NULL,animals INTEGER NOT NULL,"
                       "cars INTEGER NOT NULL,metadata TEXT NOT NULL,bytes INTEGER NOT NULL)")

    def legacy_save(self, memo="ups:74%,car:95%", alert_id="@legacy", categories=None):
        payload = message(memo=memo, alert_id=alert_id)
        meta, image = mod.parse_alert(payload, None, "UTC")
        meta["categories"] = categories or [cat for cat in meta["categories"] if cat != "delivery"]
        # Simulate an old custom-map alert even if current defaults no longer classify it.
        for folder in ["images", "thumbs"]:
            (self.root / folder / f'{meta["id"]}.jpg').write_bytes(image)
        with sqlite3.connect(self.root / "timeline.sqlite3") as db:
            db.execute("INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,?)", (
                meta["id"], meta["epoch"], meta["date"], meta["camera"],
                *[int(cat in meta["categories"]) for cat in ["people", "animals", "cars"]],
                json.dumps(meta), len(image) * 2))
        return meta, payload

    def test_backfill_preserves_images_categories_and_ids(self):
        old, payload = self.legacy_save(categories=["people", "cars"])
        ordinary, _ = self.legacy_save(memo="person:85%", alert_id="@person")
        store = mod.AlertStore(self.root)
        store.initialize()
        rows = store.list_events("delivery")["images"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], old["id"])
        self.assertEqual(rows[0]["categories"], ["people", "cars", "delivery"])
        self.assertEqual(store.image_path(old["id"]).read_bytes(), image_bytes())
        self.assertNotIn("delivery", next(i for i in store.list_events("people")["images"] if i["id"] == ordinary["id"])["categories"])
        self.assertEqual(store.ingest(payload, None, "UTC", mod.DEFAULT_LABELS), "duplicate")
        self.assertEqual(store.ingest(message(memo="package:80%", alert_id="@new"), None,
                                      "UTC", mod.DEFAULT_LABELS), "saved")
        self.assertEqual(store.list_events("delivery")["counts"]["delivery"], 2)
        with patch.object(store, "_migrate_delivery", side_effect=AssertionError("migration ran twice")):
            store.initialize()

    def test_configured_labels_used_for_history(self):
        old, _ = self.legacy_save(memo="package:90%,car:88%")
        store = mod.AlertStore(self.root, labels={"delivery": ["car"]})
        store.initialize()
        self.assertEqual(store.list_events("delivery")["images"][0]["id"], old["id"])

    def test_failed_migration_rolls_back_column_and_metadata(self):
        old, _ = self.legacy_save()
        store = mod.AlertStore(self.root)
        with patch.object(mod, "classify", side_effect=RuntimeError("failed backfill")):
            with self.assertRaises(RuntimeError):
                store.initialize()
        with sqlite3.connect(self.root / "timeline.sqlite3") as db:
            self.assertNotIn("delivery", {r[1] for r in db.execute("PRAGMA table_info(alerts)")})
            self.assertEqual(json.loads(db.execute("SELECT metadata FROM alerts").fetchone()[0]), old)
        store.initialize()
        self.assertEqual(store.list_events("delivery")["counts"]["delivery"], 1)

    def test_more_than_one_migration_batch(self):
        # Small metadata fixtures exercise batching without allocating hundreds of images.
        with sqlite3.connect(self.root / "timeline.sqlite3") as db:
            now = datetime.now(timezone.utc)
            for i in range(270):
                ident = f"{i:064x}"
                meta = {"id": ident, "camera": "Front", "memo": "UPS:85%", "categories": ["cars"]}
                db.execute("INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,?)", (
                    ident, now.timestamp(), now.date().isoformat(), "Front", 0, 0, 1, json.dumps(meta), 0))
        store = mod.AlertStore(self.root)
        store.initialize()
        self.assertEqual(store.list_events("delivery")["counts"]["delivery"], 270)


if __name__ == "__main__": unittest.main()

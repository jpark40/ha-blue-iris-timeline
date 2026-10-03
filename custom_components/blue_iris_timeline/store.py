"""Disk-backed Blue Iris alert archive. No Home Assistant imports or network I/O."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

CATEGORIES = ("people", "animals", "cars", "delivery")
DEFAULT_LABELS = {
    "people": ["person", "people", "human"],
    "animals": ["animal", "bird", "cat", "dog", "horse", "sheep", "cow", "cattle",
                "elephant", "bear", "zebra", "giraffe", "deer", "fox", "rabbit",
                "raccoon", "squirrel", "skunk", "opossum", "possum", "coyote",
                "bobcat", "mountain lion", "mouse", "rat", "chicken", "duck"],
    "cars": ["car", "truck", "bus", "vehicle", "van", "suv", "pickup",
             "motorcycle", "motorbike", "delivery truck", "delivery van",
             "amazon", "fedex", "ups", "usps", "dhl"],
    "delivery": ["delivery", "delivery person", "delivery truck", "delivery van",
                 "courier", "package", "parcel", "amazon", "fedex", "fed ex",
                 "ups", "usps", "dhl"],
}
MAX_PAYLOAD = 12 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
ID_RE = re.compile(r"^[a-f0-9]{64}$")
# Blue Iris documents the memo format as e.g. person:81%, car:92%.
FINDING_RE = re.compile(r"(?:^|[,;|\n])\s*([\w][\w -]{0,63}?)\s*:\s*(\d+(?:\.\d+)?)\s*%", re.I)


class RejectedAlert(ValueError):
    """An event does not meet the archive's input contract."""


def normalized_label(value):
    return " ".join(str(value).lower().replace("_", " ").replace("-", " ").split())


def classify(memo, labels=None):
    """Exact label matching avoids treating 'car' as part of an unrelated word."""
    mapping = {k: {normalized_label(v) for v in values}
               for k, values in (labels or DEFAULT_LABELS).items()}
    found = {}
    for raw_label, raw_score in FINDING_RE.findall(memo):
        label, score = normalized_label(raw_label), float(raw_score)
        if 0 < score <= 100:
            found[label] = max(score, found.get(label, 0))
    categories = [key for key in CATEGORIES if mapping.get(key, set()).intersection(found)]
    return categories, [{"label": k, "confidence": v} for k, v in sorted(found.items())]


def parse_alert(payload, topic_camera, tz_name, labels=None, now=None):
    """Parse one atomic metadata + image message; never substitute a live frame."""
    if len(payload) > MAX_PAYLOAD:
        raise RejectedAlert("MQTT payload exceeds 12 MiB")
    try:
        data = json.loads(payload)
    except (ValueError, TypeError, UnicodeError) as err:
        raise RejectedAlert("Invalid JSON; check the Blue Iris MQTT payload") from err
    if not isinstance(data, dict):
        raise RejectedAlert("Payload must be a JSON object")
    if data.get("confirmed") is not True:
        raise RejectedAlert("Missing confirmed: true; publish only from AI-confirmed On alert actions")
    camera = data.get("camera")
    if not isinstance(camera, str) or not camera or len(camera) > 100 or camera != camera.strip():
        raise RejectedAlert("Invalid camera short name")
    if any(c in camera for c in "/+#\x00") or camera.startswith("&"):
        raise RejectedAlert("Invalid camera short name; use &CAM in the payload")
    if topic_camera is not None and camera != topic_camera:
        raise RejectedAlert("Camera must match the last MQTT topic segment")
    memo = data.get("memo", "")
    if not isinstance(memo, str) or len(memo) > 4096:
        raise RejectedAlert("Invalid AI memo")
    categories, detections = classify(memo, labels)
    if not categories:
        raise RejectedAlert("No recognized scored AI label in memo; check labels or AI confirmation")
    raw_time = data.get("timestamp")
    if not isinstance(raw_time, str) or len(raw_time) > 64:
        raise RejectedAlert("Missing ISO timestamp; use &ALERT_TIME")
    try:
        dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        epoch = dt.timestamp()
        local = dt.astimezone(ZoneInfo(tz_name))
    except (ValueError, OverflowError, OSError) as err:
        raise RejectedAlert("Invalid timestamp; use &ALERT_TIME") from err
    if epoch > (now if now is not None else time.time()) + 300:
        raise RejectedAlert("Alert timestamp is over five minutes in the future; check BI clock")
    alert_id = data.get("alert_id")
    if not isinstance(alert_id, str) or not alert_id or len(alert_id) > 256 or alert_id.startswith("&") or alert_id == "@-1":
        raise RejectedAlert("Missing alert database locator; use &ALERT_DB on a real alert")
    encoded = data.get("image_b64", "")
    if not isinstance(encoded, str) or not encoded:
        raise RejectedAlert("Missing alert JPEG; use &ALERT_JPEG")
    if encoded.startswith("data:image/jpeg;base64,"):
        encoded = encoded.split(",", 1)[1]
    try:
        image = base64.b64decode("".join(encoded.split()), validate=True)
    except (ValueError, binascii.Error) as err:
        raise RejectedAlert("Invalid base64 JPEG; the Blue Iris macro may not have expanded") from err
    if not 4 <= len(image) <= MAX_IMAGE or not image.startswith(b"\xff\xd8"):
        raise RejectedAlert("Alert image must be a JPEG no larger than 8 MiB")
    ident = hashlib.sha256(json.dumps([camera, alert_id, epoch], separators=(",", ":")).encode()).hexdigest()
    return {
        "id": ident, "camera": camera, "alert_id": alert_id,
        "timestamp": local.isoformat(), "epoch": epoch, "date": local.date().isoformat(),
        "time": local.strftime("%H:%M:%S"), "memo": memo,
        "categories": categories, "detections": detections,
    }, image


def thumbnail(jpeg):
    try:
        with Image.open(BytesIO(jpeg)) as im:
            if im.format != "JPEG" or im.width * im.height > MAX_PIXELS:
                raise RejectedAlert("JPEG exceeds the 25 megapixel image limit")
            im.load()  # Fail rather than archive an incomplete/truncated image.
            im = ImageOps.exif_transpose(im)
            im.thumbnail((400, 250), Image.Resampling.LANCZOS)
            output = BytesIO()
            im.convert("RGB").save(output, format="JPEG", quality=75)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as err:
        raise RejectedAlert("Unreadable alert JPEG") from err


class AlertStore:
    """SQLite index with immutable images; all methods run in HA's executor."""
    def __init__(self, directory, retention_days=30, max_storage_mb=2048, camera_names=None,
                 labels=None):
        self.root = Path(directory).expanduser().absolute()
        self.retention_days = retention_days
        self.max_bytes = max_storage_mb * 1024 * 1024
        self.camera_names = camera_names or {}
        self.labels = {**DEFAULT_LABELS, **(labels or {})}
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / "timeline.sqlite3", timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.root = self.root.resolve()
            (self.root / "images").mkdir(exist_ok=True)
            (self.root / "thumbs").mkdir(exist_ok=True)
            with self.db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS alerts (
                        id TEXT PRIMARY KEY, epoch REAL NOT NULL, day TEXT NOT NULL,
                        camera TEXT NOT NULL, people INTEGER NOT NULL,
                        animals INTEGER NOT NULL, cars INTEGER NOT NULL,
                        delivery INTEGER NOT NULL DEFAULT 0,
                        metadata TEXT NOT NULL, bytes INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS alerts_day_time ON alerts(day, epoch, id);
                    CREATE INDEX IF NOT EXISTS alerts_epoch ON alerts(epoch);
                    CREATE TABLE IF NOT EXISTS diagnostics (key TEXT PRIMARY KEY, value TEXT);
                """)
                if "delivery" not in {r["name"] for r in db.execute("PRAGMA table_info(alerts)")}:
                    self._migrate_delivery(db)
            # Complete deletion interrupted by a prior process exit. Only our hashed files.
            with self.db() as db:
                for sub in ("images", "thumbs"):
                    for path in (self.root / sub).iterdir():
                        if (ID_RE.fullmatch(path.stem) and (path.suffix == ".part" or
                                (path.suffix == ".jpg" and not db.execute(
                                    "SELECT 1 FROM alerts WHERE id=?", (path.stem,)).fetchone()))):
                            path.unlink(missing_ok=True)
            self.cleanup()

    def _migrate_delivery(self, db):
        """Upgrade old indexes atomically, in small metadata-only batches."""
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE alerts ADD COLUMN delivery INTEGER NOT NULL DEFAULT 0")
        after = ""
        while True:
            rows = db.execute("SELECT id,people,animals,cars,metadata FROM alerts "
                              "WHERE id>? ORDER BY id LIMIT 256", (after,)).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                meta = json.loads(row["metadata"])
                categories, _ = classify(meta.get("memo", ""), {"delivery": self.labels["delivery"]})
                # Preserve the original three classifications, even with custom labels.
                meta["categories"] = [cat for cat in CATEGORIES[:-1] if row[cat]] + categories
                updates.append((int(bool(categories)), json.dumps(meta), row["id"]))
            db.executemany("UPDATE alerts SET delivery=?,metadata=? WHERE id=?", updates)
            after = rows[-1]["id"]

    def record_status(self, **values):
        with self.lock, self.db() as db:
            db.executemany("INSERT OR REPLACE INTO diagnostics(key,value) VALUES(?,?)",
                           [(key, json.dumps(value)) for key, value in values.items()])

    def ingest(self, payload, camera, tz_name, labels):
        meta, image = parse_alert(payload, camera, tz_name, labels)
        with self.lock:
            with self.db() as db:
                if db.execute("SELECT 1 FROM alerts WHERE id=?", (meta["id"],)).fetchone():
                    return "duplicate"
            if meta["epoch"] < time.time() - self.retention_days * 86400:
                raise RejectedAlert("Alert is outside the configured retention period")
            small = thumbnail(image)
            ident = meta["id"]
            paths = [self.root / "images" / f"{ident}.jpg", self.root / "thumbs" / f"{ident}.jpg"]
            try:
                for path, blob in zip(paths, (image, small)):
                    temp = path.with_suffix(".part")
                    try:
                        temp.write_bytes(blob)
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)
                with self.db() as db:
                    # Explicit columns also support legacy databases with delivery appended.
                    db.execute("INSERT INTO alerts "
                               "(id,epoch,day,camera,people,animals,cars,delivery,metadata,bytes) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        ident, meta["epoch"], meta["date"], meta["camera"],
                        *[int(cat in meta["categories"]) for cat in CATEGORIES],
                        json.dumps(meta), len(image) + len(small),
                    ))
            except Exception:
                for path in paths:
                    path.unlink(missing_ok=True)
                raise
            self.cleanup()
            self.record_status(last_saved=datetime.now(timezone.utc).isoformat(), last_error=None)
            return "saved"

    def _delete(self, db, ids):
        # Remove index entry first. Crash leftovers are recovered during initialize().
        db.executemany("DELETE FROM alerts WHERE id=?", [(ident,) for ident in ids])
        db.commit()
        for ident in ids:
            if ID_RE.fullmatch(ident):
                for sub in ("images", "thumbs"):
                    (self.root / sub / f"{ident}.jpg").unlink(missing_ok=True)

    def cleanup(self):
        with self.lock, self.db() as db:
            expired = [r[0] for r in db.execute("SELECT id FROM alerts WHERE epoch<?",
                       (time.time() - self.retention_days * 86400,))]
            self._delete(db, expired)
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM alerts").fetchone()[0]
            evict = []
            if total > self.max_bytes:
                for row in db.execute("SELECT id,bytes FROM alerts ORDER BY epoch,id"):
                    evict.append(row["id"])
                    total -= row["bytes"]
                    if total <= self.max_bytes:
                        break
                self._delete(db, evict)
            return len(expired) + len(evict)

    def list_events(self, category, day=None, camera="", before=None, limit=80):
        if category not in CATEGORIES:
            raise ValueError("Unknown category")
        if day:
            date.fromisoformat(day)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1 to 200")
        with self.lock, self.db() as db:
            scope, params = "1=1", []
            if camera:
                scope += " AND camera=?"
                params.append(camera)
            # Keep available dates common to all tabs so switching tabs stays on the day.
            dates = [{"date": r[0], "count": r[1]} for r in db.execute(
                f"SELECT day,COUNT(*) FROM alerts WHERE {scope} GROUP BY day ORDER BY day", params)]
            latest = dates[-1]["date"] if dates else None
            selected = day or latest
            counts = dict.fromkeys(CATEGORIES, 0)
            if selected:
                row = db.execute(f"SELECT SUM(people),SUM(animals),SUM(cars),SUM(delivery) "
                                 f"FROM alerts WHERE {scope} AND day=?",
                                 params + [selected]).fetchone()
                counts = dict(zip(CATEGORIES, [v or 0 for v in row]))
            # Category interpolates only after whitelist validation; all user values are bound.
            where, args = f"{scope} AND day=? AND {category}=1", params + [selected]
            if before:
                if not ID_RE.fullmatch(before):
                    raise ValueError("Invalid cursor")
                pivot = db.execute("SELECT epoch,id FROM alerts WHERE id=?", (before,)).fetchone()
                if pivot is None:
                    raise ValueError("Page expired; refresh the timeline")
                where += " AND (epoch<? OR (epoch=? AND id<?))"
                args += [pivot["epoch"], pivot["epoch"], pivot["id"]]
            rows = list(db.execute(f"SELECT metadata FROM alerts WHERE {where} ORDER BY epoch DESC,id DESC LIMIT ?",
                                  args + [limit + 1]))
            has_more = len(rows) > limit
            items = [json.loads(r[0]) for r in reversed(rows[:limit])]
            for item in items:
                item["camera_name"] = self.camera_names.get(item["camera"], item["camera"])
                item["url"] = f'/api/blue_iris_timeline/image/{item["id"]}'
                item["thumbnail_url"] = f'/api/blue_iris_timeline/thumbnail/{item["id"]}'
                item["label"] = item["camera_name"]
            cameras = [{"id": r[0], "name": self.camera_names.get(r[0], r[0])}
                       for r in db.execute("SELECT DISTINCT camera FROM alerts ORDER BY camera")]
            total_count, stored_bytes = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM alerts").fetchone()
            status = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM diagnostics")}
            return {
                "selected_date": selected, "latest_date": latest, "dates": dates,
                "category": category, "camera": camera, "cameras": cameras,
                "counts": counts, "images": items, "has_more": has_more,
                "next_before": items[0]["id"] if has_more and items else None,
                "total_count": total_count, "storage_bytes": stored_bytes,
                "retention_days": self.retention_days, "max_storage_mb": self.max_bytes // (1024 * 1024),
                "status": status,
            }

    def image_path(self, ident, small=False):
        if not ID_RE.fullmatch(ident):
            return None
        with self.lock, self.db() as db:
            if not db.execute("SELECT 1 FROM alerts WHERE id=?", (ident,)).fetchone():
                return None
            folder = self.root / ("thumbs" if small else "images")
            path = folder / f"{ident}.jpg"
            if path.is_symlink() or folder.is_symlink() or not path.is_file():
                return None
            return path

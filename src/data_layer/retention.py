# src/data_layer/retention.py
import json
import os
import shutil
import time
import logging

logger = logging.getLogger(__name__)

class RetentionManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.archive_dir = os.path.join(data_dir, "archive")
        os.makedirs(self.archive_dir, exist_ok=True)

    def archive_old_rounds(self, max_age_hours: int = 24, delete_after_hours: int = 168):
        rounds_dir = os.path.join(self.data_dir, "rounds")
        if not os.path.isdir(rounds_dir):
            return
        now = time.time()
        for name in os.listdir(rounds_dir):
            round_dir = os.path.join(rounds_dir, name)
            if not os.path.isdir(round_dir):
                continue
            try:
                ts = int(name)
            except ValueError:
                continue
            age_hours = (now - ts) / 3600
            if age_hours > delete_after_hours:
                shutil.rmtree(round_dir)
                logger.info(f"Deleted old round {name} (age: {age_hours:.0f}h)")
            elif age_hours > max_age_hours:
                archive_path = os.path.join(self.archive_dir, f"round-{name}.tar.gz")
                if not os.path.exists(archive_path):
                    shutil.make_archive(
                        os.path.join(self.archive_dir, f"round-{name}"),
                        "gztar",
                        root_dir=rounds_dir,
                        base_dir=name,
                    )
                shutil.rmtree(round_dir)
                logger.info(f"Archived round {name}")

    def rotate_predictions(self):
        pred_path = os.path.join(self.data_dir, "history", "predictions.jsonl")
        if not os.path.exists(pred_path) or os.path.getsize(pred_path) == 0:
            return
        date_str = time.strftime("%Y-%m-%d")
        archive_name = f"predictions-{date_str}.jsonl"
        archive_path = os.path.join(self.archive_dir, archive_name)
        with open(pred_path) as src, open(archive_path, "a") as dst:
            dst.write(src.read())
        with open(pred_path, "w") as f:
            pass
        logger.info(f"Rotated predictions to {archive_name}")

    def check_disk_space(self) -> tuple[bool, int]:
        stat = os.statvfs(self.data_dir)
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 100:
            return False, int(free_mb)
        if free_mb < 500:
            self.archive_old_rounds(max_age_hours=12)
            return True, int(free_mb)
        return True, int(free_mb)

    def compact_shared_knowledge(self, max_discoveries: int = 30):
        """Archive old discovery files to prevent unbounded growth.

        Keeps the last max_discoveries files + all non-discovery files (approaches.md, etc.).
        Old discoveries are concatenated into a single archive file.
        """
        shared_dir = os.path.join(self.data_dir, "shared_knowledge")
        if not os.path.isdir(shared_dir):
            return

        discovery_files = sorted([
            f for f in os.listdir(shared_dir)
            if f.startswith("discovery-") and os.path.isfile(os.path.join(shared_dir, f))
        ])

        if len(discovery_files) <= max_discoveries:
            return

        # Archive old discoveries
        to_archive = discovery_files[:-max_discoveries]
        archive_path = os.path.join(shared_dir, "archived-discoveries.md")
        with open(archive_path, "a") as archive:
            for fname in to_archive:
                fpath = os.path.join(shared_dir, fname)
                with open(fpath) as f:
                    archive.write(f"\n\n---\n## {fname}\n{f.read()}")
                os.unlink(fpath)

        logger.info(f"Compacted shared knowledge: archived {len(to_archive)} old discoveries, kept {max_discoveries}")

    def run_cleanup(self):
        self.archive_old_rounds()
        self.compact_shared_knowledge()
        ok, free_mb = self.check_disk_space()
        if not ok:
            logger.critical(f"Disk critically low: {free_mb}MB free. Pausing data collection.")
        return ok

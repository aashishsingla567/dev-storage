import importlib.util
import os
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("linux", Path(__file__).parents[1] / "dev_storage_linux.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class AccountingTests(unittest.TestCase):
    def test_allocated_total_matches_gnu_du(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for i in range(10):
                p = home / str(i) / "node_modules"
                p.mkdir(parents=True)
                (p / "file").write_bytes(b"x" * (i * 1234))
            os.link(home / "3/node_modules/file", home / "link")
            expected = int(subprocess.check_output(["du", "-sx", "-B1", str(home)], text=True).split()[0])
            result = m.scan([home], home=home)
            self.assertEqual(result["roots"][0]["allocated"], expected)
    def test_exclusive_totals_hardlinks_sparse_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dep = home / "Development/app/node_modules/pkg"
            dep.mkdir(parents=True)
            original = dep / "file\nwith-tab\tname"
            original.write_bytes(b"x" * 9000)
            os.link(original, dep / "hardlink")
            (home / "external").symlink_to("/usr", target_is_directory=True)
            sparse = home / "sparse.img"
            with sparse.open("wb") as stream:
                stream.truncate(128 * m.GIB)
            result = m.scan([home], home=home)
            self.assertEqual(sum(result["categories"].values()), result["roots"][0]["allocated"])
            self.assertEqual(result["counts"]["duplicate_hardlinks"], 1)
            self.assertLess(result["roots"][0]["allocated"], 1024 ** 2)
            self.assertGreater(result["roots"][0]["apparent"], 100 * m.GIB)
            self.assertEqual(result["counts"]["files"], 2)
            self.assertIn("project-dependencies", result["categories"])

    def test_explicit_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ignored = home / "ignored"
            ignored.mkdir()
            (ignored / "large").write_bytes(b"x" * 100000)
            result = m.scan([home], [str(ignored)], home=home)
            self.assertEqual(result["counts"].get("files", 0), 0)
            self.assertEqual(result["skipped"][0]["reason"], "explicit exclusion")

    def test_backing_root_must_match_inode_and_use_another_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "other"
            other.mkdir()
            with self.assertRaisesRegex(ValueError, "match"):
                m.scan([root], backing_root=other)
            with self.assertRaisesRegex(ValueError, "distinct"):
                m.scan([root], backing_root=root)

    def test_specific_prefixes_win(self):
        home = Path("/home/example")
        self.assertEqual(m.category(home / ".cache/pip/a", home), "package-caches")
        self.assertEqual(m.category(home / ".cache/ms-playwright/a", home), "browser-runtimes")
        self.assertEqual(m.category(home / ".t3/runtime/versions/a", home), "toolchains")
        self.assertEqual(m.category(home / ".codex/sessions/a", home), "agent-state")

    def test_unreadable_directory_retains_its_own_blocks_and_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            blocked = home / "blocked"
            blocked.mkdir()
            original_scandir = os.scandir
            def scandir(path):
                if Path(path) == blocked:
                    raise PermissionError("test: permission denied")
                return original_scandir(path)
            with patch.object(m.os, "scandir", side_effect=scandir):
                result = m.scan([home], home=home)
            self.assertEqual(result["counts"]["errors"], 1)
            self.assertEqual(sum(result["categories"].values()), result["roots"][0]["allocated"])

    def test_budgets_leave_available_target_and_reserved_blocks(self):
        cfg = m.defaults()
        fs = m.filesystem(m.HOME)
        allocated = sum(cfg["budgets_gib"].values()) * m.GIB
        room = fs["total"] - fs["reserved_unavailable"] - cfg["minimum_available_gib"] * m.GIB
        self.assertLessEqual(allocated, room + m.GIB)  # per-category rounding


class CleanupTests(unittest.TestCase):
    def test_symlinked_cache_and_hardlinks_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            outside = home / "valuable"
            outside.mkdir()
            (outside / "keep").write_text("keep")
            (home / ".cache").mkdir()
            (home / ".cache/pip").symlink_to(outside, target_is_directory=True)
            npm = home / ".npm/_cacache"
            npm.mkdir(parents=True)
            (npm / "shared").write_text("keep")
            os.link(npm / "shared", home / "shared")
            self.assertEqual(list(m.candidate_files(home, 30, now=time.time() + 100 * 86400)), [])

    def test_ctime_prevents_deleting_recently_created_old_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".cache/pip"
            cache.mkdir(parents=True)
            item = cache / "item"
            item.write_text("recently extracted")
            os.utime(item, (1, 1))
            self.assertEqual(list(m.candidate_files(home, 30)), [])
            future = list(m.candidate_files(home, 30, now=time.time() + 100 * 86400))
            self.assertEqual(len(future), 1)

    def test_replaced_directory_symlink_cannot_escape_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / "cache"
            cache.mkdir()
            item = cache / "item"
            item.write_text("cache")
            before = item.stat()
            cache.rename(home / "old-cache")
            valuable = home / "valuable"
            valuable.mkdir()
            (valuable / "item").write_text("keep")
            cache.symlink_to(valuable, target_is_directory=True)
            with self.assertRaises(OSError):
                m.unlink_checked(cache, item, before)
            self.assertEqual((valuable / "item").read_text(), "keep")

    def test_changed_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = root / "cache"
            item.write_text("old")
            before = item.stat()
            item.write_text("new content")
            with self.assertRaises(RuntimeError):
                m.unlink_checked(root, item, before)
            self.assertTrue(item.exists())

    def test_dry_run_preserves_and_apply_deletes_only_eligible_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".cache/pip"
            cache.mkdir(parents=True)
            item = cache / "cache"
            item.write_bytes(b"x" * 8000)
            original_candidates = m.candidate_files
            with patch.object(m, "HOME", home), patch.object(m, "busy_reasons", return_value=[]), \
                 patch.object(m, "active_cache_users", return_value=False), \
                 patch.object(m, "candidate_files", side_effect=lambda h, d: original_candidates(h, d, time.time() + 100 * 86400)):
                preview = m.clean({"cache_retention_days": 30})
                self.assertEqual(preview["removed_bytes"], 0)
                self.assertTrue(item.exists())
                applied = m.clean({"cache_retention_days": 30}, True)
                self.assertGreater(applied["removed_bytes"], 0)
                self.assertFalse(item.exists())

    def test_apply_defers_during_pressure(self):
        with patch.object(m, "busy_reasons", return_value=["busy"]), \
             patch.object(m, "active_cache_users", return_value=False):
            result = m.clean({"cache_retention_days": 30}, True)
            self.assertEqual(result["files"], 0)
            self.assertEqual(result["skipped"], ["busy"])

    def test_private_report_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            m.atomic_json(output, {"paths": ["private"]})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

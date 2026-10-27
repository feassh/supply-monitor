import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from procurement_watch import container
from procurement_watch.storage import Store


class PrivilegeTests(unittest.TestCase):
    def test_drop_all_groups_before_changing_user(self):
        calls = Mock()
        with patch.object(container.os, "setgroups", create=True) as groups, \
             patch.object(container.os, "setgid", create=True) as gid, \
             patch.object(container.os, "setuid", create=True) as uid, \
             patch.object(container.os, "umask") as umask:
            calls.attach_mock(groups,"groups")
            calls.attach_mock(gid,"gid")
            calls.attach_mock(uid,"uid")
            calls.attach_mock(umask,"umask")
            container.drop_privileges()
        self.assertEqual(calls.mock_calls,[call.groups([]),call.gid(10001),call.uid(10001),call.umask(0o077)])


@unittest.skipUnless(os.name == "posix", "Linux 文件权限测试，由 GitHub Actions 执行")
class MountPermissionTests(unittest.TestCase):
    def test_existing_database_and_key_preserved_and_writable(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)/"data"
            database = directory/"watch.db"
            store = Store(str(database))
            store.set_setting("permission_test",{"value":42})
            store.close()
            key = directory/"watch.db.push.key"
            key.write_bytes(b"synthetic-key-contents")
            unrelated = directory/"unrelated.txt"
            unrelated.write_text("leave unchanged",encoding="utf-8")
            for path in (database,key,unrelated):
                path.chmod(0o400)
            directory.chmod(0o500)
            container.prepare_data_directory(directory,os.getuid(),os.getgid())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode)&0o700,0o700)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode)&0o600,0o600)
            self.assertEqual(key.read_bytes(),b"synthetic-key-contents")
            self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode),0o400)
            store = Store(str(database))
            try:
                self.assertEqual(store.setting("permission_test"),{"value":42})
                store.set_setting("permission_test",{"value":43})
            finally:
                store.close()

    def test_does_not_follow_database_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)/"data"
            directory.mkdir()
            outside = Path(root)/"outside.txt"
            outside.write_text("unchanged",encoding="utf-8")
            outside.chmod(0o400)
            (directory/"watch.db").symlink_to(outside)
            with self.assertRaises(OSError):
                container.prepare_data_directory(directory,os.getuid(),os.getgid())
            self.assertEqual(outside.read_text(encoding="utf-8"),"unchanged")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o400)

    def test_rejects_symlink_for_data_directory(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/"other"
            target.mkdir()
            link = Path(root)/"data"
            link.symlink_to(target,target_is_directory=True)
            with self.assertRaises(OSError):
                container.prepare_data_directory(link,os.getuid(),os.getgid())

from contextlib import contextmanager
import filecmp
import os
import posix
import stat
import sys
import sysconfig
import tempfile
import time
import unittest

from ..xattr import get_all
from ..platform import get_flags
from .. import platform

# Note: this is used by borg.selftest, do not use or import py.test functionality here.

try:
    import llfuse
    # Does this version of llfuse support ns precision?
    have_fuse_mtime_ns = hasattr(llfuse.EntryAttributes, 'st_mtime_ns')
except ImportError:
    have_fuse_mtime_ns = False

try:
    from pytest import raises
except ImportError:
    raises = None

has_lchflags = hasattr(os, 'lchflags') or sys.platform.startswith('linux')
no_lchlfags_because = '' if has_lchflags else '(not supported on this platform)'
try:
    with tempfile.NamedTemporaryFile() as file:
        platform.set_flags(file.name, stat.UF_NODUMP)
except OSError:
    has_lchflags = False
    no_lchlfags_because = '(the file system at %s does not support flags)' % tempfile.gettempdir()

try:
    import llfuse
    has_llfuse = True or llfuse  # avoids "unused import"
except ImportError:
    has_llfuse = False

# The mtime get/set precision varies on different OS and Python versions
if 'HAVE_FUTIMENS' in getattr(posix, '_have_functions', []):
    st_mtime_ns_round = 0
elif 'HAVE_UTIMES' in sysconfig.get_config_vars():
    st_mtime_ns_round = -6
else:
    st_mtime_ns_round = -9

if sys.platform.startswith('netbsd'):
    st_mtime_ns_round = -4  # only >1 microsecond resolution here?


class BaseTestCase(unittest.TestCase):
    """
    """
    assert_in = unittest.TestCase.assertIn
    assert_not_in = unittest.TestCase.assertNotIn
    assert_equal = unittest.TestCase.assertEqual
    assert_not_equal = unittest.TestCase.assertNotEqual
    assert_true = unittest.TestCase.assertTrue

    if raises:
        assert_raises = staticmethod(raises)
    else:
        assert_raises = unittest.TestCase.assertRaises

    @contextmanager
    def assert_creates_file(self, path):
        self.assert_true(not os.path.exists(path), '{} should not exist'.format(path))
        yield
        self.assert_true(os.path.exists(path), '{} should exist'.format(path))

    def assert_dirs_equal(self, dir1, dir2):
        diff = filecmp.dircmp(dir1, dir2)
        self._assert_dirs_equal_cmp(diff)

    def _assert_dirs_equal_cmp(self, diff):
        self.assert_equal(diff.left_only, [])
        self.assert_equal(diff.right_only, [])
        self.assert_equal(diff.diff_files, [])
        self.assert_equal(diff.funny_files, [])
        for filename in diff.common:
            path1 = os.path.join(diff.left, filename)
            path2 = os.path.join(diff.right, filename)
            s1 = os.lstat(path1)
            s2 = os.lstat(path2)
            # Assume path2 is on FUSE if st_dev is different
            fuse = s1.st_dev != s2.st_dev
            attrs = ['st_mode', 'st_uid', 'st_gid', 'st_rdev']
            if not fuse or not os.path.isdir(path1):
                # dir nlink is always 1 on our fuse filesystem
                attrs.append('st_nlink')
            d1 = [filename] + [getattr(s1, a) for a in attrs]
            d2 = [filename] + [getattr(s2, a) for a in attrs]
            d1.append(get_flags(path1, s1))
            d2.append(get_flags(path2, s2))
            # ignore st_rdev if file is not a block/char device, fixes #203
            if not stat.S_ISCHR(d1[1]) and not stat.S_ISBLK(d1[1]):
                d1[4] = None
            if not stat.S_ISCHR(d2[1]) and not stat.S_ISBLK(d2[1]):
                d2[4] = None
            # Older versions of llfuse do not support ns precision properly
            if fuse and not have_fuse_mtime_ns:
                d1.append(round(s1.st_mtime_ns, -4))
                d2.append(round(s2.st_mtime_ns, -4))
            else:
                d1.append(round(s1.st_mtime_ns, st_mtime_ns_round))
                d2.append(round(s2.st_mtime_ns, st_mtime_ns_round))
            d1.append(get_all(path1, follow_symlinks=False))
            d2.append(get_all(path2, follow_symlinks=False))
            self.assert_equal(d1, d2)
        for sub_diff in diff.subdirs.values():
            self._assert_dirs_equal_cmp(sub_diff)

    @contextmanager
    def fuse_mount(self, location, mountpoint, mount_options=None):
        os.mkdir(mountpoint)
        args = ['mount', location, mountpoint]
        if mount_options:
            args += '-o', mount_options
        self.cmd(*args, fork=True)
        self.wait_for_mount(mountpoint)
        yield
        if sys.platform.startswith('linux'):
            cmd = 'fusermount -u %s' % mountpoint
        else:
            cmd = 'umount %s' % mountpoint
        os.system(cmd)
        os.rmdir(mountpoint)
        # Give the daemon some time to exit
        time.sleep(.2)

    def wait_for_mount(self, path, timeout=5):
        """Wait until a filesystem is mounted on `path`
        """
        timeout += time.time()
        while timeout > time.time():
            if os.path.ismount(path):
                return
            time.sleep(.1)
        raise Exception('wait_for_mount(%s) timeout' % path)


class changedir:
    def __init__(self, dir):
        self.dir = dir

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.dir)

    def __exit__(self, *args, **kw):
        os.chdir(self.old)


class environment_variable:
    def __init__(self, **values):
        self.values = values
        self.old_values = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old_values[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *args, **kw):
        for k, v in self.old_values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeInputs:
    """Simulate multiple user inputs, can be used as input() replacement"""
    def __init__(self, inputs):
        self.inputs = inputs

    def __call__(self, prompt=None):
        if prompt is not None:
            print(prompt, end='')
        try:
            return self.inputs.pop(0)
        except IndexError:
            raise EOFError from None

import hashlib
import tempfile
from pathlib import Path

from dimergio.mover import _hash_file


class TestHashFile:
    def test_small_file(self):
        content = b"hello dimergio"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            expected = hashlib.sha256(content).hexdigest()
            assert _hash_file(tmp) == expected
        finally:
            tmp.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = Path(f.name)
        try:
            expected = hashlib.sha256(b"").hexdigest()
            assert _hash_file(tmp) == expected
        finally:
            tmp.unlink()

    def test_large_chunks(self):
        content = b"x" * 200000  # spans multiple 64KB chunks
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            expected = hashlib.sha256(content).hexdigest()
            assert _hash_file(tmp) == expected
        finally:
            tmp.unlink()

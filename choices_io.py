"""
Shared helper for reading and writing choices.json with a file lock.

Usage in any script:
    from choices_io import load_choices, save_choices

    with load_choices() as (choices, save):
        choices["some/path"] = "SomePack"
        save()          # writes atomically while lock is still held

The lock is released automatically when the `with` block exits.
Never read/write choices.json directly — always go through this module.
"""
import json
from contextlib import contextmanager
from pathlib import Path
from filelock import FileLock

SCRIPT_DIR        = Path(__file__).parent
CHOICES_FILE      = SCRIPT_DIR / "choices.json"
CHOICES_LOCK_FILE = SCRIPT_DIR / "choices.json.lock"


def _write(data: dict) -> None:
    """Validate and atomically write *data* to choices.json (caller holds lock)."""
    text = json.dumps(data, indent=2)
    parsed = json.loads(text)          # sanity-check round-trip
    tmp = CHOICES_FILE.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CHOICES_FILE)
    return len(parsed)


@contextmanager
def load_choices(timeout: float = 30):
    """
    Context manager that acquires the lock, loads choices, and yields
    ``(choices_dict, save_fn)``.  Call ``save_fn()`` to persist changes.
    Lock is released on exit whether or not save_fn was called.

    Example::

        with load_choices() as (ch, save):
            ch["foo/bar"] = "PackName"
            save()
    """
    lock = FileLock(CHOICES_LOCK_FILE, timeout=timeout)
    with lock:
        data = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
        before = len(data)
        saved = [False]

        def save():
            if len(data) < before:
                raise ValueError(
                    f"Refusing to write: would shrink {before} → {len(data)} entries"
                )
            count = _write(data)
            saved[0] = True
            return count

        yield data, save

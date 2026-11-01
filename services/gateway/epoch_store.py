"""Durable gateway claim authority.

State directory: ``HANDS_RELAY_STATE_DIR``, or ``services/gateway/state``
beside this file when the gateway process starts. Two files live there:

- ``high-water.json`` — pairing id and the highest epoch this directory
  has admitted
- ``epoch.json`` — the epoch published for the process that holds the lock

The lock is an OS exclusive lock on ``authority.lock``. It dies with the
process, so a killed gateway does not keep the authority forever.
Epochs are integers. They are not UUIDs and not wall-clock values.
A missing, rolled-back, foreign, or unwritable store fails closed.
Nothing in this module starts a new epoch at zero to paper over that.
``recover("reset-pairing")`` is the explicit way to begin again.
"""
import json
import os
import uuid

class SyncError(Exception):
    """The epoch store cannot be trusted. Do not serve claim traffic."""


class EpochStore:
    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        self.lock_path = os.path.join(self.directory, "authority.lock")
        self.high_path = os.path.join(self.directory, "high-water.json")
        self.epoch_path = os.path.join(self.directory, "epoch.json")
        self._handle = None
        self.epoch = None
        self.pairing = None
        self.serving = False

    def _take_lock(self):
        os.makedirs(self.directory, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        try:
            if not _try_lock(handle):
                handle.close()
                return False
        except OSError as exc:
            handle.close()
            raise SyncError(f"authority lock failed: {exc}") from exc
        self._handle = handle
        return True

    def acquire(self):
        """Take the authority lock and allocate the next epoch.

        Returns False when a live process already holds the lock.
        Raises SyncError when the store is missing, rolled back, foreign,
        or cannot be written. A False result does not change the files.
        """
        if not self._take_lock():
            return False
        try:
            self._allocate()
        except Exception:
            self.serving = False
            self.release()
            raise
        self.serving = True
        return True

    def recheck(self):
        """Fail closed if the files moved backward under a live holder."""
        if not self.serving:
            raise SyncError("authority is not held")
        high, epoch = _read_pair(self.high_path, self.epoch_path)
        if high["pairing"] != self.pairing or epoch["pairing"] != self.pairing:
            self.serving = False
            raise SyncError("pairing identity changed under the lock")
        if int(epoch["epoch"]) != self.epoch or int(high["high_water"]) != self.epoch:
            self.serving = False
            raise SyncError("epoch store rolled back")

    def recover(self, acknowledgement):
        """Start a new pairing at epoch 1. Refuses any other acknowledgement.

        This does not allocate from the damaged files. It replaces them,
        and only after the caller supplies the exact recovery phrase.
        """
        if acknowledgement != "reset-pairing":
            raise SyncError("pairing recovery requires reset-pairing")
        if self._handle is None and not self._take_lock():
            raise SyncError("another gateway holds claim authority")
        pairing = str(uuid.uuid4())
        _atomic_json(self.epoch_path, {"epoch": 1, "pairing": pairing})
        _atomic_json(self.high_path, {"high_water": 1, "pairing": pairing})
        self.pairing = pairing
        self.epoch = 1
        self.serving = True
        return self.epoch

    def release(self):
        handle, self._handle = self._handle, None
        self.serving = False
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def _allocate(self):
        fresh = not os.path.exists(self.high_path) and not os.path.exists(self.epoch_path)
        if fresh:
            pairing = str(uuid.uuid4())
            _atomic_json(self.epoch_path, {"epoch": 1, "pairing": pairing})
            _atomic_json(self.high_path, {"high_water": 1, "pairing": pairing})
            self.pairing = pairing
            self.epoch = 1
            return
        high, epoch = _read_pair(self.high_path, self.epoch_path)
        if high["pairing"] != epoch["pairing"]:
            raise SyncError("epoch pairing does not match its high-water mark")
        published = int(epoch["epoch"])
        marked = int(high["high_water"])
        if published == marked + 1:
            # The epoch file landed and the process died before the marker.
            # That epoch may have been served. Do not reuse it.
            marked = published
        elif published != marked:
            raise SyncError("epoch store rolled back or lost")
        nxt = marked + 1
        _atomic_json(self.epoch_path, {"epoch": nxt, "pairing": high["pairing"]})
        try:
            _atomic_json(self.high_path, {"high_water": nxt, "pairing": high["pairing"]})
        except OSError:
            # epoch.json is one ahead. The next live process adopts it.
            raise SyncError("high-water mark could not be stored")
        self.pairing = high["pairing"]
        self.epoch = nxt


def _read_pair(high_path, epoch_path):
    if not os.path.exists(high_path) or not os.path.exists(epoch_path):
        raise SyncError("epoch store is incomplete")
    high = _read_json(high_path)
    epoch = _read_json(epoch_path)
    for record, key in ((high, "high_water"), (epoch, "epoch")):
        value = record.get(key)
        pairing = record.get("pairing")
        if type(value) is not int or isinstance(value, bool) or value < 1:
            raise SyncError(f"{key} is not a positive integer")
        if not isinstance(pairing, str) or not pairing:
            raise SyncError("pairing id is missing")
    return high, epoch


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"epoch store unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise SyncError("epoch store record is not an object")
    return record


def _atomic_json(path, record):
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise SyncError(f"epoch store could not be written: {exc}") from exc


def _try_lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

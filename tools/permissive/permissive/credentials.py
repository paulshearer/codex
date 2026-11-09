"""Local client key protection and the instance data directory."""

import ctypes
import os
from pathlib import Path


def protected(value, decrypt=False):
    if os.name != "nt":
        return value

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not operation(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def default_home():
    if os.environ.get("PERMISSIVE_HOME"):
        return Path(os.environ["PERMISSIVE_HOME"])
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
        / "CodexPermissive"
    )

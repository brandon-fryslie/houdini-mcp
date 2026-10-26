"""Keep macOS from napping Houdini while a client's call waits for its main thread.

A graphical Houdini runs a call handed to its main thread from an event loop
callback it fires when idle (``hdefereval``), and Houdini never opts out of App
Nap: macOS naps an app whose windows are all hidden and defers its timers, so
the call waits past its pickup bound while reads, served on socket threads,
still answer (houdini-install-aby.80r). An ``NSProcessInfo`` activity tells
macOS the process is doing work the user asked for, which it does not nap.

Houdini's Python has no PyObjC, so the activity is taken through ctypes into
the Objective-C runtime. Other platforms have no App Nap, and :func:`prevented`
holds nothing there.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

logger = logging.getLogger("houdini_mcp_plugin.app_nap")

__all__ = ["prevented"]

# NSActivityUserInitiatedAllowingIdleSystemSleep: NSActivityUserInitiated
# (0x00FFFFFF) without NSActivityIdleSystemSleepDisabled (bit 20), so the Mac
# still sleeps when idle; only the nap is held off.
_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP = 0x00EFFFFF
_CF_STRING_ENCODING_UTF8 = 0x08000100


class _ProcessInfo:
    """``[NSProcessInfo processInfo]``, reached through the Objective-C runtime."""

    def __init__(self) -> None:
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._objc = objc
        self._cf = cf
        # objc_msgSend takes the signature of the method it sends to, so each
        # message gets a prototype of its own.
        void_p = ctypes.c_void_p
        self._send = ctypes.CFUNCTYPE(void_p, void_p, void_p)(("objc_msgSend", objc))
        self._send_begin = ctypes.CFUNCTYPE(void_p, void_p, void_p, ctypes.c_uint64, void_p)(
            ("objc_msgSend", objc)
        )
        self._send_end = ctypes.CFUNCTYPE(None, void_p, void_p, void_p)(("objc_msgSend", objc))
        self._info = self._send(objc.objc_getClass(b"NSProcessInfo"), self._selector("processInfo"))

    def _selector(self, name: str) -> int:
        selector: int = self._objc.sel_registerName(name.encode())
        return selector

    def begin(self, reason: str) -> int:
        """Begin an activity for ``reason``; the activity object, retained."""
        text = self._cf.CFStringCreateWithCString(None, reason.encode(), _CF_STRING_ENCODING_UTF8)
        # The activity comes back autoreleased: drained here, on whatever thread
        # this is, with the retain below keeping it until end().
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            activity = self._send_begin(
                self._info,
                self._selector("beginActivityWithOptions:reason:"),
                _USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP,
                text,
            )
            if not activity:
                raise OSError(f"NSProcessInfo began no activity for {reason!r}")
            retained: int = self._send(activity, self._selector("retain"))
        finally:
            self._objc.objc_autoreleasePoolPop(pool)
            self._cf.CFRelease(text)
        return retained

    def end(self, activity: int) -> None:
        self._send_end(self._info, self._selector("endActivity:"), activity)
        self._send(activity, self._selector("release"))


def _held_by(process_info: _ProcessInfo) -> Callable[[str], AbstractContextManager[None]]:
    @contextmanager
    def held(reason: str) -> Iterator[None]:
        activity = process_info.begin(reason)
        logger.debug("Began an App Nap activity: %s", reason)
        try:
            yield
        finally:
            process_info.end(activity)
            logger.debug("Ended the App Nap activity: %s", reason)

    return held


@contextmanager
def _nothing_to_hold(reason: str) -> Iterator[None]:
    yield


# [LAW:dataflow-not-control-flow] the platform is chosen once, here; every call
# enters the same context manager.
prevented: Callable[[str], AbstractContextManager[None]] = (
    _held_by(_ProcessInfo()) if sys.platform == "darwin" else _nothing_to_hold
)
"""``prevented(reason)``: macOS does not nap this process while the block runs."""

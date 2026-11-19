"""Windows active-window capture regression tests."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import visual


class _Call:
    """ctypes-like callable whose arg/restype can be configured by production code."""

    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def _write_rect(pointer, bounds):
    rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
    rect.left, rect.top, rect.right, rect.bottom = bounds


class _Dwm:
    def __init__(self, result: int, bounds=(0, 0, 0, 0)):
        self.result = result
        self.bounds = bounds
        self.DwmGetWindowAttribute = _Call(self._get)

    def _get(self, _hwnd, attribute, pointer, _size):
        assert attribute == 9
        _write_rect(pointer, self.bounds)
        return self.result


class _User32:
    def __init__(self):
        self.aware = False
        self.context_calls = []
        self.GetWindowRect = _Call(self._get_window_rect)
        self.SetThreadDpiAwarenessContext = _Call(self._set_context)

    def _set_context(self, context):
        value = context.value if isinstance(context, ctypes.c_void_p) else int(context)
        self.context_calls.append(value)
        if value == 1234:
            self.aware = False
            return ctypes.c_void_p(-4)
        self.aware = True
        return ctypes.c_void_p(1234)

    def _get_window_rect(self, _hwnd, pointer):
        # Reproduce the real failure mode: a DPI-unaware call sees the
        # 150%-virtualized logical rectangle, while PMv2 sees physical pixels.
        bounds = (0, 0, 2560, 1600) if self.aware else (0, 0, 1707, 1067)
        _write_rect(pointer, bounds)
        return 1


def test_physical_window_bounds_prefers_non_virtualized_dwm_frame():
    user32 = _User32()
    dwm = _Dwm(0, (-8, -8, 2568, 1608))

    bounds, source = visual._physical_window_bounds(101, user32, dwm, ctypes, wintypes)

    assert bounds == (-8, -8, 2568, 1608)
    assert source == "dwm_extended_frame_bounds"
    assert user32.context_calls == []


def test_physical_window_bounds_fallback_temporarily_uses_per_monitor_v2():
    user32 = _User32()
    dwm = _Dwm(-1)

    bounds, source = visual._physical_window_bounds(101, user32, dwm, ctypes, wintypes)

    assert bounds == (0, 0, 2560, 1600)
    assert source == "get_window_rect_per_monitor_v2"
    assert user32.aware is False, "thread DPI awareness must be restored"
    assert len(user32.context_calls) == 2
    assert user32.context_calls[-1] == 1234

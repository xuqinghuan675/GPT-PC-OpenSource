"""Focused local tests: no real Windows desktop switching or browser startup."""
import unittest
from unittest import mock
import desktopctl as d


class DesktopWorkspacesTest(unittest.TestCase):
    def test_background_is_zero_allocation(self):
        with mock.patch.object(d, "native", side_effect=AssertionError("native must not load")):
            result = d.allocate("muse", "background")
        self.assertIsNone(result["desktop_number"])
        self.assertFalse(result["created"])

    def test_web_is_zero_allocation(self):
        with mock.patch.object(d, "desktop_status", side_effect=AssertionError("no desktops")):
            result = d.allocate("muse", "web")
        self.assertEqual(result["browser"], "existing_chrome_playwright_extension")
        self.assertIsNone(result["desktop_number"])

    def test_rejects_bad_agent(self):
        for agent in ("", "../test", "bad agent", "*", "X"*49):
            with self.subTest(agent=agent):
                with self.assertRaises(d.WorkspaceError):
                    d.allocate(agent, "background")

    def test_visual_creates_once_then_reuses_without_switching(self):
        state = {"names": ["Main"], "active": 1}
        calls = []
        def status():
            return {"count": len(state["names"]), "active": state["active"],
                    "desktops": [{"number": i+1,"name": name} for i, name in enumerate(state["names"])]}
        def native(name, *args):
            def call(*params):
                calls.append((name, params))
                if name == "CreateDesktop":
                    state["names"].append("")
                    return len(state["names"])-1
                if name == "SetDesktopName":
                    state["names"][params[0]] = params[1].decode("utf-8")
                    return 0
                if name == "GoToDesktopNumber":
                    state["active"] = params[0] + 1
                    return 0
                raise AssertionError(name)
            return call
        with mock.patch.object(d, "desktop_status", side_effect=status),\
             mock.patch.object(d, "native", side_effect=native),\
             mock.patch.object(d, "count", side_effect=lambda: len(state["names"])),\
             mock.patch.object(d, "active", side_effect=lambda: state["active"]-1):
            a = d.allocate("muse", "visual")
            b = d.allocate("muse", "visual")
        self.assertTrue(a["created"])
        self.assertFalse(b["created"])
        self.assertTrue(b["reused"])
        self.assertEqual(a["desktop_number"], 2)
        self.assertEqual(b["desktop_number"], 2)
        self.assertEqual([x[0] for x in calls].count("CreateDesktop"), 1)
        self.assertEqual(state["active"], 1)

    def test_visual_caps_desktops(self):
        obj = {"count": 6,"active":1,"desktops":[{"number":i+1, "name": "Other"} for i in range(6)]}
        with mock.patch.object(d, "desktop_status", return_value=obj):
            with self.assertRaisesRegex(d.WorkspaceError, "cap"):
                d.allocate("muse", "visual")

    def test_visual_does_not_hide_native_error(self):
        obj = {"count": 1, "active":1,"desktops":[{"number":1,"name":"Main"}]}
        with mock.patch.object(d, "desktop_status", return_value=obj),\
             mock.patch.object(d,"native",return_value=lambda: -1),\
             mock.patch.object(d,"active",return_value=0),\
             mock.patch.object(d,"count",return_value=1):
            with self.assertRaisesRegex(d.WorkspaceError, "CreateDesktop failed"):
                d.allocate("muse", "visual")

    def test_command_scope(self):
        self.assertEqual(d.dispatch({"action":"allocate","args":{"agent":"muse","mode":"web"}})["created"],False)
        with self.assertRaisesRegex(d.WorkspaceError, "Unsupported"):
            d.dispatch({"action":"delete_everything", "args":{}})

    def test_safe_pipe_name(self):
        self.assertTrue(d.PIPE_NAME.startswith(r"\\.\pipe\GPTPCDesktopWorkspace-"))
        self.assertTrue(d.KEY_FILE.is_relative_to(d.DATA))


if __name__ == "__main__":
    unittest.main()


class ReleaseTest(unittest.TestCase):
    def test_release_only_owned_empty_desktop(self):
        state = {"names": ["Main", "AI-muse"]}
        def listing():
            return {"count": len(state["names"]), "active": 1,
                    "desktops": [{"number": i + 1, "name": n} for i, n in enumerate(state["names"])]}
        def fake_native(name, *argtypes):
            if name != "RemoveDesktop":
                raise AssertionError(name)
            def call(index, fallback):
                self.assertEqual((index, fallback), (1, 0))
                state["names"].pop(index)
                return 0
            return call
        with mock.patch.object(d, "desktop_status", side_effect=listing),\
             mock.patch.object(d, "visible_window_count_on_desktop", return_value=0),\
             mock.patch.object(d, "native", side_effect=fake_native):
            result = d.release("muse")
        self.assertTrue(result["removed"])
        self.assertEqual(result["desktop_count"], 1)

    def test_release_retains_desktop_with_windows(self):
        listing = {"count": 2, "active": 1, "desktops": [
            {"number": 1, "name": "Main"}, {"number": 2, "name": "AI-muse"}]}
        with mock.patch.object(d, "desktop_status", return_value=listing),\
             mock.patch.object(d, "visible_window_count_on_desktop", return_value=2),\
             mock.patch.object(d, "native", side_effect=AssertionError("must not remove")):
            result = d.release("muse")
        self.assertFalse(result["removed"])
        self.assertEqual(result["visible_windows"], 2)

    def test_release_does_not_remove_active_desktop(self):
        listing = {"count": 2, "active": 2, "desktops": [
            {"number": 1, "name": "Main"}, {"number": 2, "name": "AI-muse"}]}
        with mock.patch.object(d, "desktop_status", return_value=listing):
            with self.assertRaisesRegex(d.WorkspaceError, "active"):
                d.release("muse")

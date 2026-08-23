from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import updates


RELEASE_URL = "https://github.com/itobey/playlist-mirror/releases/tag/0.2.0"


class _Response:
    def __init__(self, body: object, status: int = 200) -> None:
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _release(**changes):
    payload = {
        "tag_name": "0.2.0",
        "html_url": RELEASE_URL,
        "draft": False,
        "prerelease": False,
    }
    payload.update(changes)
    return payload


class VersionComparisonTests(unittest.TestCase):
    def test_detects_newer_major_minor_and_patch_versions(self) -> None:
        for current, candidate in (
            ("1.9.9", "2.0.0"),
            ("1.7.9", "1.8.0"),
            ("1.8.0", "1.8.1"),
        ):
            with self.subTest(current=current, candidate=candidate):
                self.assertTrue(updates.is_newer_version(current, candidate))

    def test_rejects_equal_and_older_versions(self) -> None:
        self.assertFalse(updates.is_newer_version("1.8.0", "1.8.0"))
        self.assertFalse(updates.is_newer_version("1.8.0", "1.7.9"))

    def test_accepts_leading_v(self) -> None:
        self.assertTrue(updates.is_newer_version("v1.7.0", "v1.8.0"))
        self.assertTrue(updates.is_newer_version("1.7.0", "v1.8.0"))

    def test_ignores_running_build_suffix(self) -> None:
        self.assertFalse(
            updates.is_newer_version("0.1.3+master.a1b2c3d", "0.1.3")
        )
        self.assertTrue(
            updates.is_newer_version("0.1.3+master.a1b2c3d", "0.1.4")
        )

    def test_rejects_malformed_versions(self) -> None:
        for value in ("", "dev", "1.2", "1.2.3.4", "1.2.3-rc1", "1.two.3"):
            with self.subTest(value=value):
                self.assertFalse(updates.is_newer_version("1.0.0", value))


class ReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        with updates._state_lock:
            updates._available = None

    def tearDown(self) -> None:
        with updates._state_lock:
            updates._available = None

    @patch("app.updates.VERSION", "0.1.0")
    @patch("app.updates._fetch_latest_release", return_value=_release())
    def test_newer_release_stores_version_and_url(self, _fetch) -> None:
        updates.check_for_update()

        self.assertEqual(
            updates.available_update(),
            updates.AvailableUpdate("0.2.0", RELEASE_URL),
        )

    @patch("app.updates.VERSION", "0.1.0")
    @patch(
        "app.updates._fetch_latest_release",
        return_value=_release(
            tag_name="v0.2.0",
            html_url="https://github.com/itobey/playlist-mirror/releases/tag/v0.2.0",
        ),
    )
    def test_release_with_leading_v_uses_its_exact_url(self, _fetch) -> None:
        updates.check_for_update()

        self.assertEqual(
            updates.available_update(),
            updates.AvailableUpdate(
                "v0.2.0",
                "https://github.com/itobey/playlist-mirror/releases/tag/v0.2.0",
            ),
        )

    @patch("app.updates.VERSION", "0.2.0")
    @patch("app.updates._fetch_latest_release", return_value=_release())
    def test_equal_release_clears_an_old_notice(self, _fetch) -> None:
        with updates._state_lock:
            updates._available = updates.AvailableUpdate("0.2.0", RELEASE_URL)

        updates.check_for_update()

        self.assertIsNone(updates.available_update())

    @patch("app.updates.VERSION", "0.1.0")
    def test_ignores_draft_prerelease_and_malformed_payloads(self) -> None:
        payloads = (
            _release(draft=True),
            _release(prerelease=True),
            _release(tag_name=None),
            _release(tag_name="dev"),
            _release(html_url=None),
            _release(html_url="javascript:alert(1)"),
            _release(html_url="https://github.com/other/project/releases/tag/0.2.0"),
            _release(html_url="https://github.com/itobey/playlist-mirror/releases/tag/9.9.9"),
        )
        for payload in payloads:
            with self.subTest(payload=payload), patch(
                "app.updates._fetch_latest_release", return_value=payload
            ):
                updates.check_for_update()
                self.assertIsNone(updates.available_update())

    @patch("app.updates.VERSION", "0.1.0")
    def test_failed_refresh_retains_last_successful_result(self) -> None:
        expected = updates.AvailableUpdate("0.2.0", RELEASE_URL)
        with updates._state_lock:
            updates._available = expected

        for error in (TimeoutError("slow"), OSError("offline"), ValueError("bad json")):
            with self.subTest(error=error), patch(
                "app.updates._fetch_latest_release", side_effect=error
            ):
                updates.check_for_update()
                self.assertEqual(updates.available_update(), expected)


class HttpBoundaryTests(unittest.TestCase):
    @patch("app.updates.urllib.request.urlopen")
    def test_fetches_and_decodes_github_json(self, urlopen) -> None:
        urlopen.return_value = _Response(_release())

        self.assertEqual(updates._fetch_latest_release(), _release())
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("application/vnd.github+json", request.headers["Accept"])

    def test_network_and_response_failures_never_raise(self) -> None:
        cases = (
            TimeoutError("slow"),
            OSError("offline"),
        )
        for error in cases:
            with self.subTest(error=error), patch(
                "app.updates.urllib.request.urlopen", side_effect=error
            ):
                self.assertIsNone(updates.check_for_update())

        for response in (_Response({}, status=403), _Response(b"not json")):
            with self.subTest(status=response.status), patch(
                "app.updates.urllib.request.urlopen", return_value=response
            ):
                self.assertIsNone(updates.check_for_update())


class CheckerLifecycleTests(unittest.TestCase):
    @patch("app.updates.config.UPDATE_CHECK_INITIAL_DELAY", 60)
    def test_start_is_idempotent_and_stop_wakes_the_thread(self) -> None:
        checker = updates._Checker()
        checker.start()
        first_thread = checker._thread
        checker.start()

        self.assertIs(checker._thread, first_thread)
        self.assertIsNotNone(first_thread)
        self.assertTrue(first_thread.is_alive())

        checker.stop()
        first_thread.join(timeout=1)
        self.assertFalse(first_thread.is_alive())

    def test_stop_is_safe_before_start(self) -> None:
        checker = updates._Checker()
        checker.stop()
        self.assertIsNone(checker._thread)


if __name__ == "__main__":
    unittest.main()

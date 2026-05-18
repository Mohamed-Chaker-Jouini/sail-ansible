# tests/unit/test_loki_push.py
"""
Unit tests for callback_plugins/loki_push.py

Run with:
    pytest tests/unit/test_loki_push.py -v
"""
import json
import os
import sys
import time
import threading
import urllib.error
from unittest.mock import MagicMock, patch, call

import pytest

# Make callback_plugins importable without Ansible installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ── Stub out the Ansible base classes before importing the plugin ─────────────
# This lets the tests run on any machine — no Ansible install required.
callback_mock = MagicMock()
callback_mock.CallbackBase = object  # plain base so our class still works

sys.modules.setdefault("ansible",                  MagicMock())
sys.modules.setdefault("ansible.plugins",          MagicMock())
sys.modules.setdefault("ansible.plugins.callback", callback_mock)

# Now safe to import
from callback_plugins.loki_push import CallbackModule  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_module(loki_url="http://10.0.0.1:3100") -> CallbackModule:
    """Return a freshly constructed CallbackModule with LOKI_URL set."""
    with patch.dict(os.environ, {"LOKI_URL": loki_url}, clear=False):
        mod = CallbackModule.__new__(CallbackModule)
        mod._display = MagicMock()
        mod._loki_url     = loki_url.rstrip("/")
        mod._timeout      = 5
        mod._playbook_name = "unknown"
        mod._play_name     = "unknown"
        return mod


def _fake_result(task_name="my task", host_name="myhost",
                 result_dict=None, has_role=False):
    """Build a minimal Ansible result-like MagicMock."""
    res = MagicMock()
    res._task.name = task_name
    res._task._role = MagicMock(get_name=lambda: "my_role") if has_role else None
    res._host.name = host_name
    res._result    = result_dict or {}
    return res


# ─── _extract_ip_from_url ─────────────────────────────────────────────────────

class TestExtractIpFromUrl:
    def test_standard_ip_and_port(self):
        assert CallbackModule._extract_ip_from_url("http://10.202.52.109:3100") == "10.202.52.109"

    def test_hostname(self):
        assert CallbackModule._extract_ip_from_url("http://loki.internal:3100") == "loki.internal"

    def test_no_port(self):
        assert CallbackModule._extract_ip_from_url("http://10.0.0.1") == "10.0.0.1"

    def test_https(self):
        assert CallbackModule._extract_ip_from_url("https://loki.example.com/path") == "loki.example.com"

    def test_empty_string_returns_empty(self):
        assert CallbackModule._extract_ip_from_url("") == ""

    def test_garbage_returns_empty(self):
        # should not raise
        result = CallbackModule._extract_ip_from_url("not_a_url")
        assert isinstance(result, str)


# ─── _patch_no_proxy ─────────────────────────────────────────────────────────

class TestPatchNoProxy:
    def test_adds_ip_to_empty_no_proxy(self):
        env_backup = os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        try:
            CallbackModule._patch_no_proxy("10.0.0.1")
            assert "10.0.0.1" in os.environ.get("no_proxy", "")
        finally:
            if env_backup is not None:
                os.environ["no_proxy"] = env_backup
            else:
                os.environ.pop("no_proxy", None)

    def test_does_not_duplicate_ip(self):
        with patch.dict(os.environ, {"no_proxy": "10.0.0.1,10.0.0.2"}, clear=False):
            CallbackModule._patch_no_proxy("10.0.0.1")
            entries = os.environ["no_proxy"].split(",")
            assert entries.count("10.0.0.1") == 1

    def test_appends_new_ip_to_existing_list(self):
        with patch.dict(os.environ, {"no_proxy": "10.0.0.1", "NO_PROXY": "10.0.0.1"}, clear=False):
            CallbackModule._patch_no_proxy("10.0.0.99")
            assert "10.0.0.99" in os.environ["no_proxy"]

    def test_empty_ip_is_noop(self):
        original = os.environ.get("no_proxy", "sentinel")
        CallbackModule._patch_no_proxy("")
        assert os.environ.get("no_proxy", "sentinel") == original


# ─── _push_sync ───────────────────────────────────────────────────────────────

class TestPushSync:
    def test_posts_correct_json_to_loki_endpoint(self):
        mod = _make_module("http://10.0.0.1:3100")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__  = MagicMock(return_value=False)
            mod._push_sync({"status": "ok"}, "hello loki")

        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://10.0.0.1:3100/loki/api/v1/push"
        assert req.get_header("Content-type") == "application/json"

        payload = json.loads(req.data.decode())
        assert payload["streams"][0]["stream"]["job"]     == "ansible"
        assert payload["streams"][0]["stream"]["project"] == "SAIL"
        assert payload["streams"][0]["stream"]["status"]  == "ok"
        assert payload["streams"][0]["values"][0][1]      == "hello loki"

    def test_does_nothing_when_loki_url_is_none(self):
        mod = _make_module()
        mod._loki_url = None
        with patch("urllib.request.urlopen") as mock_open:
            mod._push_sync({}, "test")
        mock_open.assert_not_called()

    def test_swallows_network_errors(self):
        mod = _make_module()
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("no loki")):
            # must not raise
            mod._push_sync({"status": "ok"}, "fire and forget")

    def test_extra_labels_merged_into_stream(self):
        mod = _make_module()
        captured = []

        def fake_open(req, timeout=None):
            captured.append(json.loads(req.data))
            cm = MagicMock()
            cm.__enter__ = lambda s: s
            cm.__exit__  = MagicMock(return_value=False)
            return cm

        with patch("urllib.request.urlopen", side_effect=fake_open):
            mod._push_sync({"host": "myhost", "playbook": "sail"}, "msg")

        stream = captured[0]["streams"][0]["stream"]
        assert stream["host"]     == "myhost"
        assert stream["playbook"] == "sail"

    def test_empty_label_values_excluded_from_stream(self):
        mod = _make_module()
        captured = []

        def fake_open(req, timeout=None):
            captured.append(json.loads(req.data))
            cm = MagicMock()
            cm.__enter__ = lambda s: s
            cm.__exit__  = MagicMock(return_value=False)
            return cm

        with patch("urllib.request.urlopen", side_effect=fake_open):
            mod._push_sync({"host": "", "status": "ok"}, "msg")

        stream = captured[0]["streams"][0]["stream"]
        assert "host" not in stream        # empty string filtered out
        assert stream["status"] == "ok"


# ─── _push (async wrapper) ────────────────────────────────────────────────────

class TestPushAsync:
    def test_push_fires_background_thread(self):
        mod = _make_module()
        fired = threading.Event()

        def fake_sync(labels, msg):
            fired.set()

        mod._push_sync = fake_sync
        mod._push({"status": "ok"}, "async test")
        assert fired.wait(timeout=2), "Background thread did not fire within 2 s"


# ─── _task_labels ─────────────────────────────────────────────────────────────

class TestTaskLabels:
    def test_basic_labels(self):
        mod    = _make_module()
        result = _fake_result(task_name="do stuff", host_name="myhost")
        mod._playbook_name = "sail.yml"
        mod._play_name     = "Phase 1"

        labels = mod._task_labels(result._task, result._host.name, "ok")
        assert labels["task"]     == "do stuff"
        assert labels["host"]     == "myhost"
        assert labels["status"]   == "ok"
        assert labels["playbook"] == "sail.yml"
        assert labels["play"]     == "Phase 1"

    def test_role_label_present_when_role_exists(self):
        mod    = _make_module()
        result = _fake_result(has_role=True)
        labels = mod._task_labels(result._task, None, "ok")
        assert labels.get("role") == "my_role"

    def test_role_label_absent_when_no_role(self):
        mod    = _make_module()
        result = _fake_result(has_role=False)
        labels = mod._task_labels(result._task, None, "ok")
        assert "role" not in labels


# ─── _result_msg ──────────────────────────────────────────────────────────────

class TestResultMsg:
    def _msg(self, result_dict):
        mod = _make_module()
        res = MagicMock()
        res._result = result_dict
        return mod._result_msg(res)

    def test_returns_msg_field(self):
        assert "hello" in self._msg({"msg": "hello"})

    def test_returns_stdout_lines(self):
        assert "line1" in self._msg({"stdout_lines": ["line1", "line2"]})

    def test_prefers_stdout_lines_over_stdout(self):
        msg = self._msg({"stdout_lines": ["preferred"], "stdout": "ignored"})
        assert "preferred" in msg
        assert "ignored" not in msg

    def test_includes_stderr(self):
        assert "stderr" in self._msg({"stderr": "oops"}).lower()

    def test_empty_dict_returns_json(self):
        result = self._msg({})
        assert isinstance(result, str)

    def test_non_dict_result_returns_truncated_string(self):
        mod = _make_module()
        res = MagicMock()
        res._result = "raw string"
        assert isinstance(mod._result_msg(res), str)


# ─── Callback hooks ───────────────────────────────────────────────────────────

class TestCallbackHooks:
    def setup_method(self):
        self.mod = _make_module()
        # Replace _push with a spy so we capture calls without network I/O
        self.mod._push = MagicMock()

    def test_playbook_on_start_sets_name_and_pushes(self):
        pb = MagicMock()
        pb._file_name = "sail_sync.yml"
        self.mod.v2_playbook_on_start(pb)
        assert self.mod._playbook_name == "sail_sync.yml"
        self.mod._push.assert_called_once()

    def test_play_on_start_sets_play_name(self):
        play = MagicMock()
        play.get_name.return_value = "Phase 1"
        self.mod.v2_playbook_on_play_start(play)
        assert self.mod._play_name == "Phase 1"

    def test_runner_on_ok_pushes_ok_status(self):
        result = _fake_result(result_dict={"changed": False})
        self.mod.v2_runner_on_ok(result)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "ok"

    def test_runner_on_ok_pushes_changed_status(self):
        result = _fake_result(result_dict={"changed": True})
        self.mod.v2_runner_on_ok(result)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "changed"

    def test_runner_on_failed_pushes_failed_status(self):
        result = _fake_result(result_dict={"msg": "boom"})
        self.mod.v2_runner_on_failed(result)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "failed"

    def test_runner_on_skipped_pushes_skipped_status(self):
        result = _fake_result()
        self.mod.v2_runner_on_skipped(result)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "skipped"

    def test_runner_on_unreachable_pushes_unreachable(self):
        result = _fake_result()
        self.mod.v2_runner_on_unreachable(result)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "unreachable"

    def test_playbook_on_stats_pushes_per_host(self):
        stats = MagicMock()
        stats.processed = {"host1": None, "host2": None}
        stats.summarize.return_value = {
            "ok": 5, "changed": 2, "failures": 0, "unreachable": 0, "skipped": 1
        }
        self.mod.v2_playbook_on_stats(stats)
        assert self.mod._push.call_count == 2

    def test_playbook_on_stats_marks_failed_host(self):
        stats = MagicMock()
        stats.processed = {"badhost": None}
        stats.summarize.return_value = {
            "ok": 0, "changed": 0, "failures": 1, "unreachable": 0, "skipped": 0
        }
        self.mod.v2_playbook_on_stats(stats)
        label_arg = self.mod._push.call_args[0][0]
        assert label_arg["status"] == "failed"
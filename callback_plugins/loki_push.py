# callback_plugins/loki_push.py
# Pushes Ansible execution events directly to Loki's HTTP push API.
# No dependencies beyond Python stdlib. Works on any Morpheus node that
# clones this repo — fully HA-safe.

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: loki_push
    type: notification
    short_description: Stream Ansible events to Grafana Loki
    description:
      - Pushes task-level telemetry to Loki's native /loki/api/v1/push endpoint.
      - Configured via ansible.cfg [callback_loki_push] section.
    options:
      loki_url:
        description: Base URL of your Loki instance (no trailing slash)
        ini:
          - section: callback_loki_push
            key: loki_url
        env:
          - name: LOKI_URL
        required: true
      loki_timeout:
        description: HTTP timeout in seconds
        type: int
        ini:
          - section: callback_loki_push
            key: loki_timeout
        default: 5
      no_proxy:
        description: Comma-separated hosts/IPs to bypass the system proxy
        type: str
        ini:
          - section: callback_loki_push
            key: no_proxy
        env:
          - name: NO_PROXY
        default: ""
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "loki_push"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self._loki_url = None
        self._timeout = 5
        self._playbook_name = "unknown"
        self._play_name = "unknown"

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)
        
        # Safely handle missing options to prevent NoneType crashes
        url = self.get_option("loki_url")
        self._loki_url = url.rstrip("/") if url else None
        
        timeout = self.get_option("loki_timeout")
        try:
            self._timeout = int(timeout) if timeout else 5
        except ValueError:
            self._timeout = 5

        # Inject no_proxy safely so urllib skips the corporate proxy for Loki's IP
        no_proxy = self.get_option("no_proxy")
        if no_proxy:
            existing = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
            merged = ",".join(filter(None, [existing, str(no_proxy)]))
            os.environ["no_proxy"] = merged
            os.environ["NO_PROXY"] = merged

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _push(self, labels: dict, message: str) -> None:
        """Send a single log line to Loki. Silently swallows errors so a
        dead Loki instance never breaks an Ansible run."""
        if not self._loki_url:
            return

        # Use time_ns() for exact nanosecond precision to prevent Loki out-of-order errors
        ts = str(time.time_ns())

        # Merge in standard SAIL labels
        base_labels = {
            "job":     "ansible",
            "project": "SAIL",
        }
        # Filter out empty values from dynamic labels to keep Grafana clean
        base_labels.update({k: v for k, v in labels.items() if v})

        payload = {
            "streams": [
                {
                    "stream": base_labels,
                    "values": [[ts, message]],
                }
            ]
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self._loki_url}/loki/api/v1/push",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception:
            # Broad catch handles URLError, HTTPError, timeouts, and malformed URLs
            # Never let observability failure break the automation run
            pass

    def _task_labels(self, task, host=None, status="unknown") -> dict:
        """Construct safe labels for a task event."""
        # Safely extract role name if it exists, without str() casting the object memory address
        role_name = task._role.get_name() if getattr(task, '_role', None) else None

        labels = {
            "playbook": self._playbook_name,
            "play":     self._play_name,
            "task":     getattr(task, 'name', "unnamed") or "unnamed",
            "status":   status,
        }
        
        if role_name:
            labels["role"] = role_name
        if host:
            labels["host"] = str(host)
            
        return labels

    def _result_msg(self, result) -> str:
        """Extract a clean human-readable message from a task result safely."""
        res = getattr(result, '_result', {})
        
        # Ensure we are parsing a dictionary (prevents TypeError on hard crashes/plain string returns)
        if not isinstance(res, dict):
            return str(res)[:500]
            
        parts = []
        if "msg" in res:
            parts.append(str(res["msg"]))
        if "stdout_lines" in res:
            parts.extend([str(line) for line in res["stdout_lines"]])
        elif "stdout" in res and res["stdout"]:
            parts.append(str(res["stdout"]))
        if "stderr" in res and res["stderr"]:
            parts.append(f"stderr: {res['stderr']}")
        if "reason" in res:
            parts.append(str(res["reason"]))
            
        return " | ".join(parts) if parts else json.dumps(res, default=str)[:500]

    # ------------------------------------------------------------------ #
    # Playbook lifecycle                                                 #
    # ------------------------------------------------------------------ #

    def v2_playbook_on_start(self, playbook):
        self._playbook_name = playbook._file_name or "unknown"
        self._push(
            {"status": "started", "playbook": self._playbook_name},
            f"Playbook started: {self._playbook_name}",
        )

    def v2_playbook_on_play_start(self, play):
        # Native hook to track the play name safely rather than traversing parent properties
        self._play_name = play.get_name()

    def v2_playbook_on_stats(self, stats):
        for host in sorted(stats.processed.keys()):
            s = stats.summarize(host)
            msg = (
                f"Playbook finished | host={host} ok={s['ok']} "
                f"changed={s['changed']} failed={s['failures']} "
                f"unreachable={s['unreachable']} skipped={s['skipped']}"
            )
            self._push(
                {
                    "playbook": self._playbook_name,
                    "host":     host,
                    "status":   "failed" if (s["failures"] or s["unreachable"]) else "ok",
                },
                msg,
            )

    # ------------------------------------------------------------------ #
    # Task results                                                       #
    # ------------------------------------------------------------------ #

    def v2_runner_on_ok(self, result):
        changed = getattr(result, '_result', {}).get("changed", False) if isinstance(getattr(result, '_result', None), dict) else False
        status = "changed" if changed else "ok"
        self._push(
            self._task_labels(result._task, result._host.name, status),
            f"[{status.upper()}] {result._task.name} | {self._result_msg(result)}",
        )

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._push(
            self._task_labels(result._task, result._host.name, "failed"),
            f"[FAILED] {result._task.name} | {self._result_msg(result)}",
        )

    def v2_runner_on_skipped(self, result):
        self._push(
            self._task_labels(result._task, result._host.name, "skipped"),
            f"[SKIPPED] {result._task.name}",
        )

    def v2_runner_on_unreachable(self, result):
        self._push(
            self._task_labels(result._task, result._host.name, "unreachable"),
            f"[UNREACHABLE] {result._task.name} | {self._result_msg(result)}",
        )
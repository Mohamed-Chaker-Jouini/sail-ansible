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
        type: str
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
    # False so Morpheus doesn't need to do anything special to enable it —
    # presence in callbacks_enabled in ansible.cfg is sufficient.
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self):
        super().__init__()
        self._loki_url = None
        self._timeout = 5
        self._playbook_name = "unknown"
        self._play_name = "unknown"

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)

        url = self.get_option("loki_url")
        self._loki_url = url.rstrip("/") if url else None

        timeout = self.get_option("loki_timeout")
        try:
            self._timeout = int(timeout) if timeout else 5
        except (ValueError, TypeError):
            self._timeout = 5

        # Inject no_proxy so urllib skips the corporate proxy for Loki's IP.
        # urllib does NOT support CIDR notation — explicit IP is required here.
        no_proxy = self.get_option("no_proxy")
        if no_proxy:
            existing = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
            merged = ",".join(filter(None, [existing, str(no_proxy)]))
            os.environ["no_proxy"] = merged
            os.environ["NO_PROXY"] = merged

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _push(self, labels: dict, message: str) -> None:
        """Send a single log line to Loki. Silently swallows errors so a
        dead Loki instance never breaks an Ansible run."""
        if not self._loki_url:
            return

        # time_ns() gives exact nanoseconds — prevents Loki out-of-order rejections
        ts = str(time.time_ns())

        base_labels = {
            "job":     "ansible",
            "project": "SAIL",
        }
        # Only include labels that have a non-empty value — keeps Grafana streams clean
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
        except Exception as e:
            # Surface to Ansible's display at vvv so it's visible when running
            # with -vvv but never raises — observability must not break automation.
            self._display.vvv(f"loki_push: failed to send to {self._loki_url}: {e}")

    def _task_labels(self, task, host=None, status="unknown") -> dict:
        role_name = task._role.get_name() if getattr(task, "_role", None) else None
        labels = {
            "playbook": self._playbook_name,
            "play":     self._play_name,
            "task":     getattr(task, "name", "unnamed") or "unnamed",
            "status":   status,
        }
        if role_name:
            labels["role"] = role_name
        if host:
            labels["host"] = str(host)
        return labels

    def _result_msg(self, result) -> str:
        res = getattr(result, "_result", {})
        if not isinstance(res, dict):
            return str(res)[:500]
        parts = []
        if "msg" in res:
            parts.append(str(res["msg"]))
        if "stdout_lines" in res:
            parts.extend(str(line) for line in res["stdout_lines"])
        elif "stdout" in res and res["stdout"]:
            parts.append(str(res["stdout"]))
        if "stderr" in res and res["stderr"]:
            parts.append(f"stderr: {res['stderr']}")
        if "reason" in res:
            parts.append(str(res["reason"]))
        return " | ".join(parts) if parts else json.dumps(res, default=str)[:500]

    # ------------------------------------------------------------------ #
    # Playbook lifecycle                                                   #
    # ------------------------------------------------------------------ #

    def v2_playbook_on_start(self, playbook):
        self._playbook_name = playbook._file_name or "unknown"
        # Canary line — if this appears in Grafana the plugin loaded and
        # the proxy bypass is working correctly end-to-end.
        self._push(
            {"status": "started", "playbook": self._playbook_name},
            f"[SAIL] loki_push loaded OK | target={self._loki_url} | playbook={self._playbook_name}",
        )

    def v2_playbook_on_play_start(self, play):
        # Dedicated hook — avoids fragile _parent traversal used in the original
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
    # Task results                                                         #
    # ------------------------------------------------------------------ #

    def v2_runner_on_ok(self, result):
        res = getattr(result, "_result", {})
        changed = res.get("changed", False) if isinstance(res, dict) else False
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
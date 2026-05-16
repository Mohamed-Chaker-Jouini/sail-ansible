"""
callback_plugins/loki_push.py
==============================
Pushes Ansible execution events directly to Loki's HTTP push API.
No dependencies beyond Python stdlib. Works on any Morpheus node that
clones this repo — fully HA-safe.

The Loki IP is derived at __init__ time from the LOKI_URL environment
variable (which Ansible sets from ansible.cfg before the plugin loads),
so there is a single source of truth — no hardcoded IPs in this file.

The no_proxy patch is applied at __init__ before super().__init__() runs
because Morpheus injects http_proxy into the environment before Ansible
starts, and Python's urllib will have already cached the proxy config by
the time set_options() is called.
"""

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
      - Pushes task-level telemetry to Loki's /loki/api/v1/push endpoint.
      - Configured via ansible.cfg [callback_loki_push] section or LOKI_URL env var.
    options:
      loki_url:
        description: Base URL of your Loki instance (no trailing slash).
        type: str
        ini:
          - section: callback_loki_push
            key: loki_url
        env:
          - name: LOKI_URL
        required: true
      loki_timeout:
        description: HTTP timeout in seconds.
        type: int
        ini:
          - section: callback_loki_push
            key: loki_timeout
        default: 5
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION      = 2.0
    CALLBACK_TYPE         = "notification"
    CALLBACK_NAME         = "loki_push"
    CALLBACK_NEEDS_ENABLED = False

    # ── Proxy bypass ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_ip_from_url(url: str) -> str:
        """Pull the bare host/IP out of a URL string.
        e.g. 'http://10.202.52.109:3100' → '10.202.52.109'
        Returns an empty string if parsing fails.
        """
        try:
            host = url.split("//", 1)[-1].split("/")[0].split(":")[0]
            return host if host else ""
        except Exception:
            return ""

    @staticmethod
    def _patch_no_proxy(ip: str) -> None:
        """Append *ip* to both no_proxy and NO_PROXY so urllib never routes
        Loki traffic through the corporate proxy.

        Note: Python's urllib ignores CIDR ranges in no_proxy, so we must
        add the explicit IP even if the /24 is already listed.
        """
        if not ip:
            return
        for key in ("no_proxy", "NO_PROXY"):
            existing = os.environ.get(key, "")
            entries  = [s.strip() for s in existing.split(",") if s.strip()]
            if ip not in entries:
                entries.append(ip)
            os.environ[key] = ",".join(entries)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def __init__(self):
        # Derive the Loki IP from LOKI_URL *before* super().__init__() so the
        # proxy patch fires at the earliest possible moment.
        raw_url  = os.environ.get("LOKI_URL", "")
        loki_ip  = self._extract_ip_from_url(raw_url)
        self._patch_no_proxy(loki_ip)

        super().__init__()

        self._loki_url     = raw_url.rstrip("/") if raw_url else None
        self._timeout      = 5
        self._playbook_name = "unknown"
        self._play_name     = "unknown"

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)

        url = self.get_option("loki_url")
        if url:
            self._loki_url = url.rstrip("/")
            # Re-patch in case Morpheus mutated the environment between
            # __init__ and set_options (belt-and-suspenders).
            self._patch_no_proxy(self._extract_ip_from_url(url))

        try:
            self._timeout = int(self.get_option("loki_timeout") or 5)
        except (ValueError, TypeError):
            self._timeout = 5

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _push(self, labels: dict, message: str) -> None:
        """Send one log line to Loki. Silently swallows all errors — a dead
        Loki instance must never break an Ansible run."""
        if not self._loki_url:
            return

        base_labels = {"job": "ansible", "project": "SAIL"}
        # Drop empty-string label values to keep Grafana streams clean.
        base_labels.update({k: v for k, v in labels.items() if v})

        payload = {
            "streams": [
                {
                    "stream": base_labels,
                    # time_ns() prevents Loki out-of-order rejections.
                    "values": [[str(time.time_ns()), message]],
                }
            ]
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req  = urllib.request.Request(
                f"{self._loki_url}/loki/api/v1/push",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception as exc:
            # Visible at -vvv; never raises.
            self._display.vvv(f"loki_push: failed → {self._loki_url}: {exc}")

    def _task_labels(self, task, host=None, status: str = "unknown") -> dict:
        role_name = task._role.get_name() if getattr(task, "_role", None) else None
        labels = {
            "playbook": self._playbook_name,
            "play":     self._play_name,
            "task":     getattr(task, "name", None) or "unnamed",
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
        if "msg"          in res:                         parts.append(str(res["msg"]))
        if "stdout_lines" in res:                         parts.extend(str(l) for l in res["stdout_lines"])
        elif res.get("stdout"):                           parts.append(str(res["stdout"]))
        if res.get("stderr"):                             parts.append(f"stderr: {res['stderr']}")
        if "reason"       in res:                         parts.append(str(res["reason"]))
        return " | ".join(parts) if parts else json.dumps(res, default=str)[:500]

    # ── Playbook lifecycle hooks ───────────────────────────────────────────────

    def v2_playbook_on_start(self, playbook):
        self._playbook_name = playbook._file_name or "unknown"
        # Canary line: if this appears in Grafana the plugin loaded and the
        # proxy bypass is working correctly end-to-end.
        self._push(
            {"status": "started", "playbook": self._playbook_name},
            f"[SAIL] loki_push loaded OK | target={self._loki_url} | playbook={self._playbook_name}",
        )

    def v2_playbook_on_play_start(self, play):
        self._play_name = play.get_name()

    def v2_playbook_on_stats(self, stats):
        for host in sorted(stats.processed.keys()):
            s   = stats.summarize(host)
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

    # ── Task result hooks ──────────────────────────────────────────────────────

    def v2_runner_on_ok(self, result):
        res     = getattr(result, "_result", {})
        changed = res.get("changed", False) if isinstance(res, dict) else False
        status  = "changed" if changed else "ok"
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
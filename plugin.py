from __future__ import annotations

import base64
import copy
import html
import json
import math
import mimetypes
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import gradio as gr
import requests
from PIL import Image
from shared.utils.plugins import WAN2GPPlugin


CONNECTOR_NAME = "Filexa2Wan2GP Connector"
CONNECTOR_VERSION = "0.8.1"
CONNECTOR_TAB_LABEL = "Filexa2Wan2GP"
CONNECTOR_ID = "Filexa2Wan2GPConnector"
FILEXA_BOT_URL = "https://t.me/WorkOnBigFilesBot"
PLUGIN_REPO_URL = "https://github.com/Teutonick/Filexa2wan2gp"
FILEXA_ENGINE = "wangp"
POLL_DELAY_SECONDS = 10
MAX_PROMPT_CHARS = 8000
MAX_REFERENCE_COUNT = 4
MAX_UPLOAD_IMAGE_BYTES = 40 * 1024 * 1024
MAX_UPLOAD_VIDEO_BYTES = 50 * 1024 * 1024
MAX_FALLBACK_IMAGE_BYTES = 3 * 1024 * 1024
BINARY_CHUNK_BYTES = 50 * 1024
TEXT_CHUNK_BYTES_FAST = 8 * 1024
TEXT_CHUNK_BYTES_SAFE = 4 * 1024
DIRECT_UPLOAD_TIMEOUT = 10
CHUNK_UPLOAD_TIMEOUT = 10
FILEXA_JSON_TIMEOUT = 15
STATUS_TIMEOUT = 4
REFERENCE_DOWNLOAD_TIMEOUT = 20
REFERENCE_DIRECT_ATTEMPTS = 2
REFERENCE_TEXT_ATTEMPTS = 3
JSON_CHUNK_FAST_DELAY = 0.5
JSON_CHUNK_SAFE_DELAY = 0.75
UPLOAD_MODE_HINT_TTL_SECONDS = 6 * 60 * 60
REFERENCE_MODE_HINT_TTL_SECONDS = 60 * 60
UPLOAD_MODE_TEXT_FAST = "text_fast"
UPLOAD_MODE_TEXT_SAFE = "text_safe"
REFERENCE_MODE_TEXT = "text"

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{8,512}$")
SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
SUPPORTED_VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime"}
SNAPSHOT_REFERENCE_PATH_KEYS = {
    "control_image",
    "control_images",
    "controlnet_image",
    "controlnet_images",
    "image_end",
    "image_ref",
    "image_refs",
    "image_reference",
    "image_references",
    "image_start",
    "init_image",
    "init_images",
    "input_image",
    "input_images",
    "input_reference",
    "input_references",
    "mask_image",
    "mask_images",
    "reference_image",
    "reference_images",
    "reference_path",
    "reference_paths",
    "source_image",
    "source_images",
}


@dataclass
class FilexaConfig:
    enabled: bool = False
    api_url: str = ""
    token: str = ""
    keep_result_on_pc_only: bool = False
    compress_images_before_upload: bool = True
    manual_snapshot_mode: bool = False
    default_settings_json: str = ""
    default_video_settings_json: str = ""
    last_success_settings_json: str = ""
    last_success_video_settings_json: str = ""
    worker_backend: str = ""
    settings_source: str = ""
    diagnostics: list[str] = field(default_factory=list)
    status: str = "disabled"
    last_event: str = ""
    last_error: str = ""
    active_job_id: str = ""
    active_kind: str = ""
    active_prompt_preview: str = ""
    started_at_utc: str = ""
    updated_at_utc: str = ""
    poll_count: int = 0
    last_duration_seconds: float = 0.0
    upload_mode_hint: str = ""
    upload_mode_hint_until_utc: str = ""
    reference_download_mode_hint: str = ""
    reference_download_mode_hint_until_utc: str = ""


@dataclass
class UploadPayload:
    bytes: bytes
    mime_type: str
    label: str = ""


@dataclass
class TaskRuntime:
    task: dict[str, Any]
    temp_dir: Path
    started_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    reference_paths: list[str] = field(default_factory=list)


class FilexaUnauthorizedError(RuntimeError):
    pass


class FilexaHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class FilexaClient:
    def __init__(self, config: FilexaConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token.strip()}",
                "X-Filexa-Connector-Version": CONNECTOR_VERSION,
            }
        )

    def close(self) -> None:
        self.session.close()

    def absolute_url(self, path: str) -> str:
        base = _require_base_url(self.config.api_url)
        raw = str(path or "").strip()
        if not raw:
            raise ValueError("Filexa URL is empty")
        candidate = urlparse(raw)
        if candidate.scheme:
            resolved = raw
        else:
            resolved = urljoin(f"{base.scheme}://{base.netloc}/", raw.lstrip("/"))
        parsed = urlparse(resolved)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Filexa URL must use http or https")
        if not _same_origin(base, parsed):
            raise ValueError("Filexa URL origin does not match configured API URL")
        if not parsed.path.startswith("/local/v1/"):
            raise ValueError("Filexa URL path is outside /local/v1/")
        return resolved

    def post_json(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = FILEXA_JSON_TIMEOUT,
        connection_close: bool = False,
    ) -> dict[str, Any]:
        headers = {"Connection": "close"} if connection_close else None
        response = self.session.post(
            self.absolute_url(path),
            json=body or {},
            timeout=timeout,
            headers=headers,
        )
        self._ensure_success(response)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    def get_bytes(self, path: str, *, timeout: float = REFERENCE_DOWNLOAD_TIMEOUT) -> tuple[bytes, str]:
        response = self.session.get(
            self.absolute_url(path),
            timeout=timeout,
            headers={"Connection": "close"},
        )
        self._ensure_success(response)
        mime_type = _clean_mime(response.headers.get("Content-Type") or "application/octet-stream")
        return response.content, mime_type

    def post_bytes(
        self,
        path: str,
        payload: UploadPayload,
        *,
        timeout: float,
        model_type: str = "",
    ) -> None:
        headers = {
            "Content-Type": payload.mime_type,
            "Content-Length": str(len(payload.bytes)),
            "Connection": "close",
        }
        if model_type:
            headers["X-Filexa-Model-Type"] = model_type
        response = self.session.post(
            self.absolute_url(path),
            data=payload.bytes,
            headers=headers,
            timeout=timeout,
        )
        self._ensure_success(response)

    def post_binary_chunk(
        self,
        chunk_base_path: str,
        *,
        upload_id: str,
        index: int,
        chunk_count: int,
        total_bytes: int,
        mime_type: str,
        chunk: bytes,
        model_type: str = "",
    ) -> None:
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(chunk)),
            "X-Filexa-Upload-Id": upload_id,
            "X-Filexa-Chunk-Index": str(index),
            "X-Filexa-Chunk-Count": str(chunk_count),
            "X-Filexa-Total-Bytes": str(total_bytes),
            "X-Filexa-Image-Mime": mime_type,
            "Connection": "close",
        }
        if model_type:
            headers["X-Filexa-Model-Type"] = model_type
        response = self.session.post(
            self.absolute_url(f"{chunk_base_path.rstrip('/')}/{index}"),
            data=chunk,
            headers=headers,
            timeout=CHUNK_UPLOAD_TIMEOUT,
        )
        self._ensure_success(response)

    def _ensure_success(self, response: requests.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        body = _short_text(response.text, 500)
        if response.status_code in {401, 403}:
            raise FilexaUnauthorizedError(
                f"Filexa returned {response.status_code} Unauthorized; reconnect with a new token."
            )
        raise FilexaHttpError(response.status_code, f"Filexa HTTP {response.status_code}: {body}")


class FilexaProgressCallbacks:
    def __init__(self, plugin: "Filexa2Wan2GPConnectorPlugin", runtime: TaskRuntime, client: FilexaClient) -> None:
        self.plugin = plugin
        self.runtime = runtime
        self.client = client
        self.last_status = ""
        self.last_progress = -1
        self.last_posted_at = 0.0

    def on_status(self, status: Any) -> None:
        text = _short_text(str(status or "").strip(), 120)
        if text:
            self._maybe_post(text, None)

    def on_progress(self, update: Any) -> None:
        progress = _coerce_progress(getattr(update, "progress", None))
        status = _short_text(str(getattr(update, "status", "") or getattr(update, "phase", "") or "generating"), 120)
        self._maybe_post(status, progress)

    def on_event(self, event: Any) -> None:
        if str(getattr(event, "kind", "")) == "progress":
            self.on_progress(getattr(event, "data", None))

    def _maybe_post(self, status: str, progress: int | None) -> None:
        now = time.monotonic()
        progress_value = self.last_progress if progress is None else progress
        if status == self.last_status and progress_value == self.last_progress and now - self.last_posted_at < 2:
            return
        if now - self.last_posted_at < 1 and progress_value == self.last_progress:
            return
        self.last_status = status
        self.last_progress = progress_value
        self.last_posted_at = now
        self.plugin._post_task_status_safe(self.client, self.runtime.task, status, progress_value)


class Filexa2Wan2GPConnectorPlugin(WAN2GPPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.name = CONNECTOR_NAME
        self.version = CONNECTOR_VERSION
        self.description = "Connects WanGP to Filexa local image/video generation."
        self.author = "Filexa"
        self.url = PLUGIN_REPO_URL
        self._config_path = Path(__file__).with_name("filexa2wan2gp_config.json")
        self._config_lock = threading.RLock()
        self._config = self._load_config()
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._ui_ready = False
        self._headless_session: Any = None
        self._session_lock = threading.RLock()
        self._active_runtime: TaskRuntime | None = None
        self._active_job: Any = None
        self._live_lock = threading.RLock()
        self._live_status = "idle"
        self._live_progress: int | None = None
        self._last_video_settings: dict[str, Any] = self._settings_from_text(self._config.default_video_settings_json)
        self._last_image_settings: dict[str, Any] = self._settings_from_text(self._config.default_settings_json)

    def setup_ui(self) -> None:
        self.request_component("state")
        self.request_global("get_current_model_settings")
        self.request_global("get_model_def")
        self.add_tab(
            tab_id=CONNECTOR_ID,
            label=CONNECTOR_TAB_LABEL,
            component_constructor=self.create_ui,
        )

    def on_tab_select(self, state: dict[str, Any]) -> tuple[str, str, str, str, list[str]]:
        if not self._config_snapshot().manual_snapshot_mode:
            self._capture_current_settings_from_state(state, source="auto on tab open")
        return self._ui_tick(include_snapshot_json=True)

    def create_ui(self, api_session: Any) -> None:
        self._ui_ready = True
        self._debug(f"UI loaded from {Path(__file__).resolve()}")
        self._ensure_worker()

        with gr.Column():
            gr.Markdown(
                f"### {CONNECTOR_TAB_LABEL}\n"
                f"<small>Version: {CONNECTOR_VERSION}</small>\n\n"
                "Connect this WanGP instance to Filexa local generation. All traffic is outbound "
                f"from this PC to the configured [Filexa]({FILEXA_BOT_URL}) API URL.\n\n"
                f"Plugin source: [Teutonick/Filexa2wan2gp]({PLUGIN_REPO_URL}).\n\n"
                "- Set the Filexa API URL and token, enable the connector, then click Save / reconnect.\n"
                "- On the WanGP Video Generator tab, choose the model and generation settings; Filexa "
                "task prompt/reference fields take priority.\n"
                "- Keep WanGP running. The connector is ready to receive Filexa tasks."
            )
            activity = gr.HTML(value=self._render_activity_html())
            with gr.Row():
                api_url = gr.Textbox(label="Filexa API URL", value=self._config.api_url, scale=2)
                token = gr.Textbox(label="Filexa token", value="", type="password", scale=2)
            with gr.Row():
                enabled = gr.Checkbox(label="Enable connector", value=self._config.enabled)
                compress_images = gr.Checkbox(
                    label="JPEG fallback before upload",
                    value=self._config.compress_images_before_upload,
                )
                local_only = gr.Checkbox(
                    label="Keep result on this PC only",
                    value=self._config.keep_result_on_pc_only,
                )
                manual_snapshot_mode = gr.Checkbox(
                    label="Manual settings snapshots",
                    value=self._config.manual_snapshot_mode,
                )
            reference_gallery = gr.Gallery(
                label="Input references for active task",
                value=self._reference_gallery_value(),
                columns=4,
                rows=1,
                height=150,
                object_fit="contain",
                preview=True,
            )
            with gr.Accordion("Manual snapshots and advanced task JSON", open=False):
                gr.Markdown(
                    "In the default mode these snapshots are refreshed automatically for the matching "
                    "task type before generation. Enable manual snapshots only when you want to freeze "
                    "WanGP settings and update them by button."
                )
                settings_json = gr.Textbox(
                    label="Manual WanGP image snapshot JSON",
                    value=self._config.default_settings_json,
                    lines=10,
                    placeholder=(
                        "Optional. Paste a WanGP Export Settings JSON for image output, or click "
                        "Update image snapshot after configuring WanGP image settings."
                    ),
                )
                video_settings_json = gr.Textbox(
                    label="Manual WanGP video snapshot JSON",
                    value=self._config.default_video_settings_json,
                    lines=10,
                    placeholder=(
                        "Optional. Paste/capture a WanGP settings JSON configured for text-to-video "
                        "or image-to-video output."
                    ),
                )
                with gr.Row():
                    capture_btn = gr.Button("Update image snapshot")
                    capture_video_btn = gr.Button("Update video snapshot")
            with gr.Row():
                save_btn = gr.Button("Save / reconnect", variant="primary")
                refresh_btn = gr.Button("Refresh status")
                cancel_btn = gr.Button("Cancel active task")
                disconnect_btn = gr.Button("Disconnect")
            status = gr.Textbox(label="Status", value=self._render_status(), lines=20, interactive=False)
            capture_image_target = gr.State("image")
            capture_video_target = gr.State("video")
            self.on_tab_outputs = [settings_json, video_settings_json, activity, status, reference_gallery]

        save_btn.click(
            fn=self._ui_save,
            inputs=[
                api_url,
                token,
                enabled,
                compress_images,
                local_only,
                manual_snapshot_mode,
                settings_json,
                video_settings_json,
            ],
            outputs=[activity, status],
            queue=False,
        )
        capture_btn.click(
            fn=self._ui_capture_current_settings,
            inputs=[self.state, capture_image_target],
            outputs=[settings_json, activity, status],
            queue=False,
        )
        capture_video_btn.click(
            fn=self._ui_capture_current_settings,
            inputs=[self.state, capture_video_target],
            outputs=[video_settings_json, activity, status],
            queue=False,
        )
        refresh_btn.click(fn=self._ui_tick_status, outputs=[activity, status, reference_gallery], queue=False)
        cancel_btn.click(fn=self._ui_cancel_active_task, outputs=[activity, status], queue=False)
        disconnect_btn.click(fn=self._ui_disconnect, outputs=[activity, status], queue=False)
        timer = _make_timer()
        if timer is not None:
            timer.tick(fn=self._ui_tick_status, outputs=[activity, status, reference_gallery], queue=False)

    def _ui_save(
        self,
        api_url: str,
        token: str,
        enabled: bool,
        compress_images: bool,
        local_only: bool,
        manual_snapshot_mode: bool,
        settings_json: str,
        video_settings_json: str,
    ) -> tuple[str, str]:
        saved_video_settings_json = video_settings_json.strip()
        saved_image_settings_json = settings_json.strip()
        image_settings = self._settings_from_text(saved_image_settings_json)
        video_settings = self._settings_from_text(saved_video_settings_json)
        if saved_image_settings_json and not image_settings:
            _json_object(saved_image_settings_json)
        if saved_video_settings_json and not video_settings:
            _json_object(saved_video_settings_json)
        if image_settings and self._settings_media_kind(image_settings) != "image":
            raise gr.Error("Image snapshot JSON does not look like WanGP image-output settings.")
        if video_settings and self._settings_media_kind(video_settings) != "video":
            raise gr.Error("Video snapshot JSON does not look like WanGP video-output settings.")
        if image_settings:
            saved_image_settings_json = json.dumps(image_settings, indent=2, ensure_ascii=False)
        if video_settings:
            saved_video_settings_json = json.dumps(video_settings, indent=2, ensure_ascii=False)
        with self._config_lock:
            config = copy.deepcopy(self._config)
            config.api_url = _clean_base_url(api_url) if api_url.strip() or enabled else ""
            if token.strip():
                if not SAFE_TOKEN_RE.fullmatch(token.strip()):
                    raise gr.Error("Invalid Filexa token shape.")
                config.token = token.strip()
            if enabled and (not config.api_url or not config.token):
                raise gr.Error("Filexa API URL and token are required before enabling the connector.")
            if image_settings:
                self._last_image_settings = copy.deepcopy(image_settings)
            if video_settings:
                self._last_video_settings = copy.deepcopy(video_settings)
            config.default_settings_json = saved_image_settings_json
            config.default_video_settings_json = saved_video_settings_json
            config.enabled = bool(enabled)
            config.compress_images_before_upload = bool(compress_images)
            config.keep_result_on_pc_only = bool(local_only)
            config.manual_snapshot_mode = bool(manual_snapshot_mode)
            config.status = "enabled" if config.enabled else "disabled"
            config.last_event = "Configuration saved"
            config.last_error = ""
            self._config = config
            self._remember_diagnostic_locked(f"Configuration saved in {Path(__file__).resolve()}")
            self._save_config_locked()
        self._ensure_worker()
        return self._render_activity_html(), self._render_status()

    def _ui_capture_current_settings(self, state: dict[str, Any], target: str = "image") -> tuple[str, str, str]:
        settings = self._current_wangp_settings_from_state(state)
        if not settings:
            raise gr.Error("WanGP returned invalid settings.")
        captured = self._store_settings_snapshot(
            str(target or "image"),
            settings,
            source=f"manual {target} snapshot",
            event=f"Updated {target} snapshot",
            strict=True,
        )
        return captured, self._render_activity_html(), self._render_status()

    def _ui_disconnect(self) -> tuple[str, str]:
        runtime = self._active_runtime
        if runtime is not None:
            runtime.cancel_event.set()
        if self._active_job is not None and not getattr(self._active_job, "done", False):
            try:
                self._active_job.cancel()
            except Exception:
                pass
        with self._config_lock:
            self._config.enabled = False
            self._config.token = ""
            self._config.status = "disabled"
            self._config.last_event = "Disconnected"
            self._config.last_error = ""
            self._clear_active_locked()
            self._save_config_locked()
        self._close_headless_session()
        self._set_live_progress("idle", None)
        return self._render_activity_html(), self._render_status()

    def _ui_cancel_active_task(self) -> tuple[str, str]:
        runtime = self._active_runtime
        if runtime is None:
            with self._config_lock:
                self._config.last_event = "No active task to cancel"
                self._save_config_locked()
            return self._render_activity_html(), self._render_status()
        runtime.cancel_event.set()
        if self._active_job is not None and not getattr(self._active_job, "done", False):
            try:
                self._active_job.cancel()
            except Exception as exc:
                self._debug(f"WanGP cancel skipped: {exc}")
        try:
            client = self._client_snapshot()
            self._report_cancel_safe(client, runtime.task, "Canceled in Filexa2Wan2GP Connector")
            client.close()
        except Exception as exc:
            self._debug(f"Cancel report skipped: {exc}")
        self._close_headless_session()
        self._active_job = None
        self._active_runtime = None
        self._finish_runtime("canceled", "Cancel requested", runtime.started_at)
        return self._render_activity_html(), self._render_status()

    def _ensure_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_stop.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="filexa2wangp-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        consecutive_errors = 0
        while not self._worker_stop.is_set():
            client: FilexaClient | None = None
            try:
                if not self._ui_ready:
                    self._set_status("waiting", "Waiting for WanGP UI")
                    self._sleep_interruptible(POLL_DELAY_SECONDS)
                    continue
                config = self._config_snapshot()
                if not config.enabled or not config.api_url or not config.token:
                    self._sleep_interruptible(POLL_DELAY_SECONDS)
                    continue
                client = FilexaClient(config)
                poll = client.post_json(
                    "/local/v1/tasks/poll",
                    {
                        "client_name": CONNECTOR_NAME,
                        "client_version": CONNECTOR_VERSION,
                        "status": self._poll_status_label(),
                    },
                    timeout=FILEXA_JSON_TIMEOUT,
                )
                consecutive_errors = 0
                with self._config_lock:
                    self._config.poll_count += 1
                    self._config.updated_at_utc = _utc_now_iso()
                    self._save_config_locked()
                task = poll.get("task") if isinstance(poll, dict) else None
                if isinstance(task, dict):
                    self._run_task(task, client)
                    client = None
                else:
                    self._set_status("enabled", "Polling Filexa")
                    self._sleep_interruptible(POLL_DELAY_SECONDS)
            except FilexaUnauthorizedError as exc:
                self._disable_after_filexa_failure(str(exc))
                self._sleep_interruptible(POLL_DELAY_SECONDS)
            except Exception as exc:
                if _is_filexa_server_unavailable(exc):
                    self._disable_after_filexa_failure(
                        "Filexa server is unavailable; check the API URL, server, network path, and connect again."
                    )
                    self._sleep_interruptible(POLL_DELAY_SECONDS)
                    continue
                consecutive_errors += 1
                self._set_error("error", f"Worker error: {exc}")
                self._sleep_interruptible(min(60, POLL_DELAY_SECONDS * max(1, consecutive_errors)))
            finally:
                if client is not None:
                    client.close()

    def _run_task(self, task: dict[str, Any], client: FilexaClient) -> None:
        self._validate_task(task, client)
        started_at = time.monotonic()
        temp_dir = Path(tempfile.mkdtemp(prefix="filexa2wangp_"))
        runtime = TaskRuntime(task=task, temp_dir=temp_dir, started_at=started_at)
        self._active_runtime = runtime
        self._set_live_progress("task received", 0)
        with self._config_lock:
            self._config.active_job_id = str(task["job_id"])
            self._config.active_kind = str(task.get("kind") or "")
            self._config.active_prompt_preview = _short_text(str(task.get("prompt") or ""), 140)
            self._config.started_at_utc = _utc_now_iso()
            self._config.updated_at_utc = self._config.started_at_utc
            self._config.status = "running"
            self._config.last_event = f"Task {task['job_id']}: received"
            self._config.last_error = ""
            self._save_config_locked()
        try:
            self._post_task_status_safe(client, task, "preparing WanGP task", 8)
            wangp_task = self._build_wangp_task(task, client, temp_dir)
            callbacks = FilexaProgressCallbacks(self, runtime, client)
            session = self._worker_wangp_session()
            self._debug(
                f"Submitting task {task['job_id']} via {session.__class__.__module__}."
                f"{session.__class__.__name__}"
            )
            self._post_task_status_safe(client, task, "generating in WanGP", 15)
            job = session.submit_task(wangp_task, callbacks=callbacks)
            self._active_job = job
            result = job.result()
            if runtime.cancel_event.is_set() or bool(getattr(result, "cancelled", False)):
                self._report_cancel_safe(client, task, "Canceled in Filexa2Wan2GP Connector")
                self._finish_runtime("canceled", "Task canceled", started_at)
                return
            if not getattr(result, "success", False):
                errors = list(getattr(result, "errors", []) or [])
                raise RuntimeError(str(errors[0] if errors else "WanGP generation failed"))
            output_path = self._first_output_path(result)
            if output_path is None:
                raise RuntimeError("WanGP completed without returning an output file")
            params = wangp_task.get("params") if isinstance(wangp_task, dict) else {}
            model_type = _result_model_type(params if isinstance(params, dict) else {})
            self._post_task_status_safe(client, task, "uploading result", 94)
            self._deliver_output(client, task, output_path, model_type=model_type)
            self._remember_successful_snapshot(
                str(task.get("kind") or ""),
                params if isinstance(params, dict) else {},
            )
            self._finish_runtime("completed", f"Task complete: {output_path.name}", started_at)
        except FilexaUnauthorizedError:
            raise
        except Exception as exc:
            if runtime.cancel_event.is_set():
                self._report_cancel_safe(client, task, "Canceled in Filexa2Wan2GP Connector")
                self._finish_runtime("canceled", "Task canceled", started_at)
                return
            if _is_filexa_server_unavailable(exc):
                raise
            error = _worker_error_message(exc)
            self._debug(f"Task failed: {traceback.format_exc()}")
            self._report_failure_safe(client, task, error)
            self._finish_runtime("failed", f"Task failed: {_short_text(error, 300)}", started_at, error=error)
        finally:
            self._active_job = None
            self._active_runtime = None
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _worker_wangp_session(self) -> Any:
        with self._session_lock:
            if self._headless_session is not None:
                return self._headless_session
            self._set_status("initializing", "Initializing WanGP worker session")
            try:
                from shared.api import init as init_wangp_session
            except Exception as exc:
                raise RuntimeError("WanGP in-process API is unavailable. Update WanGP and restart it.") from exc
            self._headless_session = init_wangp_session(
                console_output=False,
                console_isatty=False,
            )
            self._set_worker_backend(
                f"headless(shared.api.init) -> {self._headless_session.__class__.__module__}."
                f"{self._headless_session.__class__.__name__}"
            )
            return self._headless_session

    def _close_headless_session(self) -> None:
        with self._session_lock:
            session = self._headless_session
            self._headless_session = None
        if session is None:
            return
        try:
            session.close()
        except Exception as exc:
            self._debug(f"WanGP worker session close skipped: {exc}")

    def _build_wangp_task(self, task: dict[str, Any], client: FilexaClient, temp_dir: Path) -> dict[str, Any]:
        kind = str(task.get("kind") or "")
        base = self._default_wangp_settings(kind)
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        wangp_task_override = params.get("wangp_task") if isinstance(params.get("wangp_task"), dict) else None
        if wangp_task_override is not None:
            prepared = copy.deepcopy(wangp_task_override)
            if isinstance(prepared.get("params"), dict):
                prepared_params = copy.deepcopy(prepared["params"])
            else:
                prepared_params = copy.deepcopy(wangp_task_override)
                prepared = {"id": task["job_id"], "params": prepared_params, "plugin_data": {}}
        else:
            clean_params = {
                key: copy.deepcopy(value)
                for key, value in params.items()
                if key not in {"wangp_task", "reference_bindings"} and not str(key).startswith("_filexa_")
            }
            prepared_params = _deep_merge(base, clean_params)
            prepared = {"id": task["job_id"], "params": prepared_params, "plugin_data": {}}
        prepared_params["prompt"] = str(task.get("prompt") or "")
        prepared_params.setdefault("_api", {"return_media": True})
        if isinstance(prepared_params.get("_api"), dict):
            prepared_params["_api"].setdefault("return_media", True)
        self._force_output_kind_settings(kind, prepared_params)
        _clear_task_reference_settings(prepared_params)
        self._attach_references(task, client, temp_dir, prepared_params, params)
        if not str(prepared_params.get("model_type") or "").strip():
            raise RuntimeError(self._missing_settings_message(_snapshot_kind_for_task(kind)))
        prepared["id"] = task["job_id"]
        prepared["params"] = prepared_params
        prepared.setdefault("plugin_data", {})
        return prepared

    def _default_wangp_settings(self, kind: str = "") -> dict[str, Any]:
        target = _snapshot_kind_for_task(kind)
        if not self._config_snapshot().manual_snapshot_mode:
            self._capture_current_settings_for_kind(target, source="auto before task")
        snapshot = self._live_settings_snapshot(target)
        if _settings_has_model_type(snapshot):
            media_kind = self._settings_media_kind(snapshot)
            if media_kind == target:
                self._set_settings_source(f"{target} snapshot: {_settings_model_type(snapshot)}")
                return snapshot
            self._remember_diagnostic(
                f"Ignored cached {target} snapshot because it looks like {media_kind} settings."
            )
        with self._config_lock:
            text = self._config.default_video_settings_json if target == "video" else self._config.default_settings_json
        fallback = self._last_success_settings(target)
        if text.strip():
            settings = self._settings_from_text(text)
            if _settings_has_model_type(settings):
                media_kind = self._settings_media_kind(settings)
                if media_kind == target:
                    self._remember_live_settings(target, settings)
                    self._set_settings_source(f"saved {target} snapshot: {_settings_model_type(settings)}")
                    return settings
                if fallback:
                    self._remember_diagnostic(
                        f"Fell back to last successful {target} snapshot because saved snapshot "
                        f"looks like {media_kind} settings."
                    )
                    self._set_settings_source(
                        f"last successful {target} fallback: {_settings_model_type(fallback)}"
                    )
                    return fallback
                raise RuntimeError(
                    f"The saved WanGP {target} snapshot is actually {media_kind} settings. "
                    f"Choose a WanGP {target} configuration and update the {target} snapshot."
                )
            if fallback:
                self._remember_diagnostic(
                    f"Fell back to last successful {target} snapshot because saved snapshot is invalid."
                )
                self._set_settings_source(
                    f"last successful {target} fallback: {_settings_model_type(fallback)}"
                )
                return fallback
            self._set_settings_source(f"invalid {target} snapshot")
            raise RuntimeError(self._missing_settings_message(target))
        if fallback:
            self._set_settings_source(f"last successful {target} fallback: {_settings_model_type(fallback)}")
            return fallback
        self._set_settings_source(f"missing {target} snapshot")
        raise RuntimeError(self._missing_settings_message(target))

    def _force_output_kind_settings(self, kind: str, settings: dict[str, Any]) -> None:
        target = _snapshot_kind_for_task(kind)
        if target == "video":
            settings["image_mode"] = 0
            return
        try:
            image_mode = int(settings.get("image_mode") or 0)
        except (TypeError, ValueError):
            image_mode = 0
        if image_mode <= 0:
            settings["image_mode"] = 1

    def _current_wangp_settings_from_component(self) -> dict[str, Any]:
        state_component = getattr(self, "state", None)
        state = getattr(state_component, "value", None)
        return self._current_wangp_settings_from_state(state)

    def _current_wangp_settings_from_state(self, state: Any) -> dict[str, Any]:
        getter = getattr(self, "get_current_model_settings", None)
        if not callable(getter):
            return {}
        if not isinstance(state, dict):
            return {}
        try:
            settings = getter(state)
        except Exception as exc:
            self._debug(f"Could not read current Video Generator settings: {exc}")
            return {}
        if not isinstance(settings, dict):
            return {}
        clean = copy.deepcopy(settings)
        model_type = _state_model_type(state)
        if model_type and not str(clean.get("model_type") or "").strip():
            clean["model_type"] = model_type
        clean.pop("client_id", None)
        clean.pop("state", None)
        clean.pop("plugin_data", None)
        return clean

    def _live_settings_snapshot(self, target: str) -> dict[str, Any]:
        with self._config_lock:
            return copy.deepcopy(self._last_video_settings if target == "video" else self._last_image_settings)

    def _remember_live_settings(self, target: str, settings: dict[str, Any]) -> None:
        with self._config_lock:
            if target == "video":
                self._last_video_settings = copy.deepcopy(settings)
            else:
                self._last_image_settings = copy.deepcopy(settings)

    def _capture_current_settings_for_kind(self, target: str, *, source: str) -> dict[str, Any]:
        settings = self._current_wangp_settings_from_component()
        if not settings:
            self._remember_diagnostic(f"Could not auto-update {target} snapshot: current WanGP settings are unavailable.")
            return {}
        return self._store_settings_snapshot(
            target,
            settings,
            source=source,
            event=f"Updated {target} snapshot",
            strict=False,
            return_settings=True,
        )

    def _capture_current_settings_from_state(self, state: Any, *, source: str) -> dict[str, Any]:
        settings = self._current_wangp_settings_from_state(state)
        if not settings:
            self._remember_diagnostic("Could not auto-update snapshot: current WanGP settings are unavailable.")
            return {}
        target = self._settings_media_kind(settings)
        if target not in {"image", "video"}:
            self._remember_diagnostic(f"Skipped auto-update for unsupported WanGP {target} settings.")
            return {}
        return self._store_settings_snapshot(
            target,
            settings,
            source=source,
            event=f"Updated {target} snapshot",
            strict=False,
            return_settings=True,
        )

    def _store_settings_snapshot(
        self,
        target: str,
        settings: dict[str, Any],
        *,
        source: str,
        event: str,
        strict: bool,
        return_settings: bool = False,
    ):
        target = "video" if target == "video" else "image"
        clean = _sanitize_settings_snapshot(settings)
        if not _settings_has_model_type(clean):
            message = f"Choose a WanGP {target} model/settings before updating the {target} snapshot."
            if strict:
                raise gr.Error(message)
            self._remember_diagnostic(message)
            return {} if return_settings else ""
        media_kind = self._settings_media_kind(clean)
        if media_kind != target:
            message = (
                f"Current WanGP settings look like {media_kind} output, not {target}. "
                f"The {target} snapshot was not changed."
            )
            if strict:
                raise gr.Error(message)
            self._remember_diagnostic(message)
            return {} if return_settings else ""
        captured = json.dumps(clean, indent=2, ensure_ascii=False)
        model_type = _settings_model_type(clean)
        with self._config_lock:
            if target == "video":
                changed = captured != self._config.default_video_settings_json
                self._last_video_settings = clean
                self._config.default_video_settings_json = captured
            else:
                changed = captured != self._config.default_settings_json
                self._last_image_settings = clean
                self._config.default_settings_json = captured
            self._config.settings_source = f"{source} {target}: {model_type}" if model_type else f"{source} {target}"
            self._config.last_event = event
            self._config.last_error = ""
            if changed:
                self._remember_diagnostic_locked(f"{event}: kind={target} model_type={model_type or '-'}")
            self._save_config_locked()
        return clean if return_settings else captured

    def _remember_successful_snapshot(self, kind: str, settings: dict[str, Any]) -> None:
        target = _snapshot_kind_for_task(kind)
        clean = _sanitize_settings_snapshot(settings)
        if not _settings_has_model_type(clean):
            return
        media_kind = self._settings_media_kind(clean)
        if media_kind != target:
            self._remember_diagnostic(
                f"Successful {target} task returned {media_kind} settings; snapshot was not persisted."
            )
            return
        captured = json.dumps(clean, indent=2, ensure_ascii=False)
        model_type = _settings_model_type(clean)
        with self._config_lock:
            if target == "video":
                self._config.last_success_video_settings_json = captured
            else:
                self._config.last_success_settings_json = captured
            self._config.settings_source = f"last successful {target}: {model_type}"
            self._remember_diagnostic_locked(
                f"Saved last successful {target} snapshot: model_type={model_type or '-'}"
            )
            self._save_config_locked()

    def _last_success_settings(self, target: str) -> dict[str, Any]:
        with self._config_lock:
            text = (
                self._config.last_success_video_settings_json
                if target == "video"
                else self._config.last_success_settings_json
            )
        settings = self._settings_from_text(text)
        if not _settings_has_model_type(settings):
            return {}
        if self._settings_media_kind(settings) != target:
            return {}
        return settings

    def _settings_media_kind(self, settings: dict[str, Any]) -> str:
        mode = str(settings.get("mode") or "").strip().lower()
        if mode in {"edit_audio", "edit_remux"}:
            return "audio"
        model_def = self._model_def(settings)
        if bool(model_def.get("audio_only", False)):
            return "audio"
        return _settings_media_kind_guess(settings)

    def _model_def(self, settings: dict[str, Any]) -> dict[str, Any]:
        getter = getattr(self, "get_model_def", None)
        if not callable(getter):
            return {}
        model_type = _settings_model_type(settings)
        if not model_type:
            return {}
        try:
            model_def = getter(model_type)
        except Exception as exc:
            self._debug(f"Could not read WanGP model definition for {model_type}: {exc}")
            return {}
        return model_def if isinstance(model_def, dict) else {}

    def _missing_settings_message(self, target: str) -> str:
        if self._config_snapshot().manual_snapshot_mode:
            return (
                f"WanGP {target} settings snapshot is missing. In Filexa2Wan2GP, open Manual "
                f"snapshots and click Update {target} snapshot after selecting a suitable WanGP model."
            )
        return (
            f"WanGP {target} settings are not configured. Open WanGP Video Generator, choose a "
            f"{target}-output model/settings, then keep Filexa2Wan2GP open or update the {target} "
            "snapshot manually."
        )

    @staticmethod
    def _settings_from_text(text: str) -> dict[str, Any]:
        if not str(text or "").strip():
            return {}
        try:
            return _sanitize_settings_snapshot(_settings_from_payload(_json_object(text)))
        except Exception:
            return {}

    def _attach_references(
        self,
        task: dict[str, Any],
        client: FilexaClient,
        temp_dir: Path,
        settings: dict[str, Any],
        task_params: dict[str, Any],
    ) -> None:
        references = task.get("references") if isinstance(task.get("references"), list) else []
        if not references:
            return
        paths = []
        for index, reference in enumerate(references[:MAX_REFERENCE_COUNT]):
            if not isinstance(reference, dict):
                continue
            paths.append(str(self._download_reference(client, reference, index, temp_dir)))
        if not paths:
            return
        runtime = self._active_runtime
        if runtime is not None:
            runtime.reference_paths = list(paths)
        self._remember_diagnostic(f"Received {len(paths)} Filexa reference(s) for task {task.get('job_id')}")
        bindings = task_params.get("reference_bindings")
        if isinstance(bindings, dict):
            for key, spec in bindings.items():
                if not isinstance(key, str) or not key:
                    continue
                value = _binding_value(spec, paths)
                if value:
                    settings[key] = value
            return
        if str(task.get("kind") or "") in {"image_edit", "video"}:
            settings["image_start"] = paths[0] if len(paths) == 1 else paths
            settings["image_refs"] = paths
            self._enable_reference_prompt_mode(settings, str(task.get("kind") or ""))

    def _enable_reference_prompt_mode(self, settings: dict[str, Any], kind: str) -> None:
        if kind not in {"image_edit", "video"}:
            return
        model_def = self._model_def(settings)
        allowed = str(model_def.get("image_prompt_types_allowed") or "")
        if "S" in allowed:
            settings["image_prompt_type"] = _add_sequence_letter(str(settings.get("image_prompt_type") or ""), "S")
        if _model_accepts_image_refs(model_def):
            settings["video_prompt_type"] = _add_sequence_letter(str(settings.get("video_prompt_type") or ""), "I")

    def _download_reference(
        self,
        client: FilexaClient,
        reference: dict[str, Any],
        index: int,
        temp_dir: Path,
    ) -> Path:
        mime = _clean_mime(str(reference.get("mime_type") or "image/jpeg"))
        if mime not in SUPPORTED_IMAGE_MIMES:
            raise ValueError("Unsupported Filexa reference mime type")
        filename = _safe_filename(str(reference.get("filename") or f"reference-{index + 1}{_extension_for_mime(mime)}"))
        direct_url = str(reference.get("url") or "")
        text_chunk_url = str(reference.get("text_chunk_url") or "")
        data: bytes | None = None
        use_text_first = self._active_reference_hint() == REFERENCE_MODE_TEXT
        if not use_text_first and direct_url:
            for _attempt in range(REFERENCE_DIRECT_ATTEMPTS):
                try:
                    data, got_mime = client.get_bytes(direct_url, timeout=REFERENCE_DOWNLOAD_TIMEOUT)
                    mime = _clean_mime(got_mime or mime)
                    _validate_image_bytes(data, mime)
                    break
                except Exception as exc:
                    self._debug(f"Direct reference download failed: {exc}")
                    data = None
        if data is None and text_chunk_url:
            last_error: Exception | None = None
            for _attempt in range(REFERENCE_TEXT_ATTEMPTS):
                try:
                    data, mime = self._download_reference_text_chunks(client, text_chunk_url)
                    self._remember_reference_hint(REFERENCE_MODE_TEXT)
                    break
                except Exception as exc:
                    last_error = exc
            if data is None and last_error is not None:
                raise last_error
        if data is None:
            raise RuntimeError("Could not download Filexa reference")
        _validate_image_bytes(data, mime)
        output = temp_dir / filename
        output.write_bytes(data)
        return output

    def _download_reference_text_chunks(self, client: FilexaClient, path: str) -> tuple[bytes, str]:
        chunks: list[bytes] = []
        chunk_count: int | None = None
        total_bytes: int | None = None
        mime_type = "image/jpeg"
        for index in range(1024):
            body = client.session.get(
                client.absolute_url(f"{path.rstrip('/')}/{index}"),
                timeout=REFERENCE_DOWNLOAD_TIMEOUT,
                headers={"Connection": "close"},
            )
            client._ensure_success(body)
            payload = body.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Invalid reference chunk payload")
            body_index = int(payload.get("index"))
            if body_index != index:
                raise RuntimeError("Reference chunk index mismatch")
            if chunk_count is None:
                chunk_count = int(payload.get("chunk_count"))
                total_bytes = int(payload.get("total_bytes"))
                mime_type = _clean_mime(str(payload.get("mime_type") or mime_type))
            if int(payload.get("chunk_count")) != chunk_count or int(payload.get("total_bytes")) != total_bytes:
                raise RuntimeError("Reference chunk metadata mismatch")
            chunks.append(base64.b64decode(str(payload.get("data_b64") or ""), validate=True))
            if index + 1 >= chunk_count:
                data = b"".join(chunks)
                if total_bytes is None or len(data) != total_bytes:
                    raise RuntimeError("Reference chunk size mismatch")
                return data, mime_type
        raise RuntimeError("Too many reference chunks")

    def _deliver_output(self, client: FilexaClient, task: dict[str, Any], path: Path, *, model_type: str = "") -> None:
        payload = self._media_payload_from_path(path)
        if payload is None:
            self._post_task_status_safe(client, task, "completed locally", 100)
            self._report_complete(
                client,
                task,
                "WanGP produced a file that Filexa cannot upload yet. The result stayed on your PC.",
                model_type=model_type,
            )
            return
        if (
            _snapshot_kind_for_task(str(task.get("kind") or "")) == "image"
            and payload.mime_type in SUPPORTED_VIDEO_MIMES
        ):
            self._post_task_status_safe(client, task, "video kept on this PC", 100)
            self._report_complete(
                client,
                task,
                "WanGP generated a video for an image task, so the file stayed on your PC. "
                "Check that the saved WanGP image snapshot uses an image-output mode.",
                model_type=model_type,
            )
            return
        if self._config_snapshot().keep_result_on_pc_only:
            self._post_task_status_safe(client, task, "completed locally", 100)
            self._report_complete(client, task, model_type=model_type)
            return
        if payload.mime_type in SUPPORTED_VIDEO_MIMES:
            self._deliver_video_output(client, task, payload, model_type=model_type)
            return
        self._deliver_image_output(client, task, payload, model_type=model_type)

    def _deliver_video_output(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload, *, model_type: str = "") -> None:
        if len(payload.bytes) > MAX_UPLOAD_VIDEO_BYTES:
            self._post_task_status_safe(client, task, "video kept on this PC", 100)
            self._report_complete(
                client,
                task,
                "WanGP generated a video, but it is larger than Filexa's 50 MB direct upload limit. "
                "The file stayed on your PC.",
                model_type=model_type,
            )
            return
        try:
            self._post_task_status_safe(client, task, "uploading video result", 96)
            client.post_bytes(
                str(task.get("result_upload_url") or ""),
                payload,
                timeout=DIRECT_UPLOAD_TIMEOUT,
                model_type=model_type,
            )
        except FilexaUnauthorizedError:
            raise
        except FilexaHttpError as exc:
            if exc.status_code == 410:
                raise
            self._debug(f"Video direct upload failed: {exc}")
            self._report_complete(
                client,
                task,
                "WanGP generated the video, but direct upload to Filexa failed. "
                "The file stayed on your PC; check the network path before retrying.",
                model_type=model_type,
            )
        except Exception as exc:
            self._debug(f"Video direct upload failed: {exc}")
            self._report_complete(
                client,
                task,
                "WanGP generated the video, but direct upload to Filexa failed. "
                "The file stayed on your PC; check the network path before retrying.",
                model_type=model_type,
            )

    def _deliver_image_output(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload, *, model_type: str = "") -> None:
        if len(payload.bytes) > MAX_UPLOAD_IMAGE_BYTES:
            converted = self._jpeg_payload(payload)
            payload = converted if converted is not None else payload
        direct_payload = self._jpeg_payload(payload) if self._config_snapshot().compress_images_before_upload else payload
        if direct_payload is None:
            direct_payload = payload
        if len(direct_payload.bytes) <= MAX_UPLOAD_IMAGE_BYTES:
            try:
                client.post_bytes(
                    str(task.get("result_upload_url") or ""),
                    direct_payload,
                    timeout=DIRECT_UPLOAD_TIMEOUT,
                    model_type=model_type,
                )
                return
            except FilexaUnauthorizedError:
                raise
            except FilexaHttpError as exc:
                if exc.status_code == 410:
                    raise
                self._debug(f"Direct upload failed: {exc}")
            except Exception as exc:
                self._debug(f"Direct upload failed: {exc}")
        fallback = direct_payload if direct_payload.mime_type == "image/jpeg" else self._jpeg_payload(payload)
        if fallback is None or len(fallback.bytes) > MAX_FALLBACK_IMAGE_BYTES:
            self._post_task_status_safe(client, task, "completed locally", 100)
            self._report_complete(client, task, model_type=model_type)
            return
        preferred = self._active_upload_hint()
        if preferred in {UPLOAD_MODE_TEXT_FAST, UPLOAD_MODE_TEXT_SAFE}:
            self._upload_text_chunks_adaptive(client, task, fallback, preferred=preferred, model_type=model_type)
            return
        try:
            self._upload_binary_chunks(client, task, fallback, model_type=model_type)
            return
        except FilexaUnauthorizedError:
            raise
        except FilexaHttpError as exc:
            if exc.status_code == 410:
                raise
            self._debug(f"Binary chunk upload failed: {exc}")
        except Exception as exc:
            self._debug(f"Binary chunk upload failed: {exc}")
        self._upload_text_chunks_adaptive(client, task, fallback, model_type=model_type)

    def _upload_binary_chunks(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload, *, model_type: str = "") -> None:
        base_path = str(task.get("result_chunk_upload_url") or f"{str(task.get('result_upload_url') or '').rstrip('/')}/chunks")
        client.absolute_url(base_path)
        upload_id = uuid.uuid4().hex
        chunk_count = max(1, math.ceil(len(payload.bytes) / BINARY_CHUNK_BYTES))
        for index in range(chunk_count):
            offset = index * BINARY_CHUNK_BYTES
            chunk = payload.bytes[offset : offset + BINARY_CHUNK_BYTES]
            progress = 94 + min(5, int(((index + 1) / chunk_count) * 5))
            self._post_task_status_safe(client, task, "uploading chunked result", progress)
            client.post_binary_chunk(
                base_path,
                upload_id=upload_id,
                index=index,
                chunk_count=chunk_count,
                total_bytes=len(payload.bytes),
                mime_type=payload.mime_type,
                chunk=chunk,
                model_type=model_type,
            )

    def _upload_text_chunks_adaptive(
        self,
        client: FilexaClient,
        task: dict[str, Any],
        payload: UploadPayload,
        *,
        preferred: str = "",
        model_type: str = "",
    ) -> None:
        if preferred != UPLOAD_MODE_TEXT_SAFE:
            try:
                self._upload_text_chunks(client, task, payload, TEXT_CHUNK_BYTES_FAST, JSON_CHUNK_FAST_DELAY, model_type=model_type)
                self._remember_upload_hint(UPLOAD_MODE_TEXT_FAST)
                return
            except FilexaUnauthorizedError:
                raise
            except FilexaHttpError as exc:
                if exc.status_code == 410:
                    raise
                self._debug(f"Fast JSON/base64 upload failed: {exc}")
            except Exception as exc:
                self._debug(f"Fast JSON/base64 upload failed: {exc}")
        self._upload_text_chunks(client, task, payload, TEXT_CHUNK_BYTES_SAFE, JSON_CHUNK_SAFE_DELAY, model_type=model_type)
        self._remember_upload_hint(UPLOAD_MODE_TEXT_SAFE)

    def _upload_text_chunks(
        self,
        client: FilexaClient,
        task: dict[str, Any],
        payload: UploadPayload,
        chunk_bytes: int,
        delay: float,
        *,
        model_type: str = "",
    ) -> None:
        base_path = str(task.get("result_text_chunk_upload_url") or f"{str(task.get('result_upload_url') or '').rstrip('/')}/text-chunks")
        client.absolute_url(base_path)
        upload_id = uuid.uuid4().hex
        chunk_count = max(1, math.ceil(len(payload.bytes) / chunk_bytes))
        for index in range(chunk_count):
            offset = index * chunk_bytes
            chunk = payload.bytes[offset : offset + chunk_bytes]
            self._post_task_status_safe(client, task, "uploading JSON/base64 result", 94 + min(5, int(((index + 1) / chunk_count) * 5)))
            client.post_json(
                f"{base_path.rstrip('/')}/{index}",
                {
                    "upload_id": upload_id,
                    "index": index,
                    "chunk_count": chunk_count,
                    "total_bytes": len(payload.bytes),
                    "mime_type": payload.mime_type,
                    "model_type": model_type,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                },
                timeout=CHUNK_UPLOAD_TIMEOUT,
                connection_close=True,
            )
            if index + 1 < chunk_count:
                time.sleep(delay)

    def _image_payload_from_path(self, path: Path) -> UploadPayload | None:
        if not path.is_file():
            return None
        mime_type = _clean_mime(mimetypes.guess_type(path.name)[0] or "")
        if mime_type not in SUPPORTED_IMAGE_MIMES:
            data = path.read_bytes()[:16]
            mime_type = _mime_from_magic(data)
        if mime_type not in SUPPORTED_IMAGE_MIMES:
            return None
        data = path.read_bytes()
        _validate_image_bytes(data, mime_type)
        return UploadPayload(data, mime_type, path.name)

    def _video_payload_from_path(self, path: Path) -> UploadPayload | None:
        if not path.is_file():
            return None
        mime_type = _clean_mime(mimetypes.guess_type(path.name)[0] or "")
        data = path.read_bytes()
        if mime_type not in SUPPORTED_VIDEO_MIMES:
            mime_type = _mime_from_magic(data[:16])
        if mime_type not in SUPPORTED_VIDEO_MIMES:
            return None
        _validate_video_bytes(data, mime_type)
        return UploadPayload(data, mime_type, path.name)

    def _media_payload_from_path(self, path: Path) -> UploadPayload | None:
        return self._image_payload_from_path(path) or self._video_payload_from_path(path)

    def _jpeg_payload(self, payload: UploadPayload) -> UploadPayload | None:
        try:
            with Image.open(BytesIO(payload.bytes)) as image:
                rgb = image.convert("RGB")
                output = BytesIO()
                rgb.save(output, format="JPEG", quality=80, optimize=True)
                return UploadPayload(output.getvalue(), "image/jpeg", payload.label)
        except Exception as exc:
            self._debug(f"JPEG conversion failed: {exc}")
            return None

    def _first_output_path(self, result: Any) -> Path | None:
        for value in list(getattr(result, "generated_files", []) or []):
            path = Path(str(value)).expanduser()
            if path.is_file():
                return path.resolve()
        for artifact in list(getattr(result, "artifacts", []) or []):
            value = getattr(artifact, "path", None)
            if value:
                path = Path(str(value)).expanduser()
                if path.is_file():
                    return path.resolve()
        return None

    def _validate_task(self, task: dict[str, Any], client: FilexaClient) -> None:
        job_id = str(task.get("job_id") or "")
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid Filexa task id")
        if str(task.get("kind") or "") not in {"image", "image_edit", "video"}:
            raise ValueError("Unsupported Filexa task kind")
        if str(task.get("engine") or FILEXA_ENGINE).lower() != FILEXA_ENGINE:
            raise ValueError("Unsupported Filexa local connector engine")
        if str(task.get("client_type") or FILEXA_ENGINE).lower() != FILEXA_ENGINE:
            raise ValueError("Unsupported Filexa local connector client_type")
        prompt = str(task.get("prompt") or "")
        if not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS or any(ord(char) < 32 and char not in "\r\n\t" for char in prompt):
            raise ValueError("Invalid Filexa task prompt")
        if not isinstance(task.get("params"), dict):
            raise ValueError("Invalid Filexa task params")
        references = task.get("references") if isinstance(task.get("references"), list) else []
        if len(references) > MAX_REFERENCE_COUNT:
            raise ValueError("Too many Filexa references")
        for key in (
            "result_upload_url",
            "result_chunk_upload_url",
            "result_text_chunk_upload_url",
            "result_complete_url",
            "status_url",
            "failure_url",
            "cancel_url",
        ):
            value = task.get(key)
            if value:
                client.absolute_url(str(value))
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("Invalid Filexa reference descriptor")
            client.absolute_url(str(reference.get("url") or ""))
            if reference.get("text_chunk_url"):
                client.absolute_url(str(reference.get("text_chunk_url")))

    def _report_complete(
        self,
        client: FilexaClient,
        task: dict[str, Any],
        message: str | None = None,
        *,
        model_type: str = "",
    ) -> None:
        payload = {"message": _short_text(message, 500)} if message else {}
        if model_type:
            payload["model_type"] = model_type
        client.post_json(str(task.get("result_complete_url") or ""), payload, timeout=FILEXA_JSON_TIMEOUT)

    def _report_failure_safe(self, client: FilexaClient, task: dict[str, Any], error: str) -> None:
        try:
            client.post_json(str(task.get("failure_url") or ""), {"error": _short_text(error, 1000)}, timeout=FILEXA_JSON_TIMEOUT)
        except Exception as exc:
            self._debug(f"Failure report skipped: {exc}")

    def _report_cancel_safe(self, client: FilexaClient, task: dict[str, Any], reason: str) -> None:
        try:
            client.post_json(str(task.get("cancel_url") or ""), {"reason": _short_text(reason, 300)}, timeout=FILEXA_JSON_TIMEOUT)
        except Exception as exc:
            self._debug(f"Cancel report skipped: {exc}")

    def _post_task_status_safe(self, client: FilexaClient, task: dict[str, Any], status: str, progress: int | None) -> None:
        path = str(task.get("status_url") or "")
        clean_status = _short_text(status, 120)
        clean_progress = _coerce_progress(progress)
        self._set_live_progress(clean_status, clean_progress)
        if not path:
            return
        try:
            client.post_json(
                path,
                {"status": clean_status, "progress": clean_progress},
                timeout=STATUS_TIMEOUT,
            )
        except Exception as exc:
            self._debug(f"Status update skipped: {exc}")

    def _client_snapshot(self) -> FilexaClient:
        return FilexaClient(self._config_snapshot())

    def _config_snapshot(self) -> FilexaConfig:
        with self._config_lock:
            return copy.deepcopy(self._config)

    def _set_status(self, status: str, event: str) -> None:
        with self._config_lock:
            self._config.status = status
            self._config.last_event = event
            self._config.updated_at_utc = _utc_now_iso()
            self._save_config_locked()

    def _set_worker_backend(self, backend: str) -> None:
        with self._config_lock:
            clean = _short_text(backend, 240)
            if self._config.worker_backend != clean:
                self._config.worker_backend = clean
                self._remember_diagnostic_locked(f"Worker backend: {clean}")
                self._save_config_locked()

    def _set_settings_source(self, source: str) -> None:
        with self._config_lock:
            clean = _short_text(source, 240)
            if self._config.settings_source != clean:
                self._config.settings_source = clean
                self._remember_diagnostic_locked(f"Settings source: {clean}")
                self._save_config_locked()

    def _set_live_progress(self, status: str, progress: int | None) -> None:
        with self._live_lock:
            self._live_status = _short_text(status or "idle", 160)
            self._live_progress = _coerce_progress(progress)

    def _live_progress_snapshot(self) -> tuple[str, int | None]:
        with self._live_lock:
            return self._live_status, self._live_progress

    def _set_error(self, status: str, error: str, *, disable: bool = False) -> None:
        with self._config_lock:
            if disable:
                self._config.enabled = False
            self._config.status = "disabled" if disable else status
            self._config.last_error = _short_text(error, 1000)
            self._config.last_event = _short_text(error, 300)
            self._config.updated_at_utc = _utc_now_iso()
            self._remember_diagnostic_locked(f"Worker error: {error}")
            self._save_config_locked()
        if disable:
            self._set_live_progress("disabled", None)

    def _disable_after_filexa_failure(self, message: str) -> None:
        with self._config_lock:
            self._config.enabled = False
            self._config.status = "disabled"
            self._config.last_error = ""
            self._config.last_event = _short_text(message, 300)
            self._clear_active_locked()
            self._remember_diagnostic_locked(f"Connector disabled: {message}")
            self._save_config_locked()
        self._set_live_progress("disabled", None)
        self._close_headless_session()

    def _finish_runtime(self, status: str, event: str, started_at: float, *, error: str = "") -> None:
        with self._config_lock:
            if status in {"completed", "canceled"}:
                self._config.status = "enabled" if self._config.enabled else "disabled"
            else:
                self._config.status = status
            self._config.last_event = event
            self._config.last_error = _short_text(error, 1000)
            self._config.last_duration_seconds = round(max(0.0, time.monotonic() - started_at), 1)
            self._clear_active_locked()
            if error:
                self._remember_diagnostic_locked(f"Task failure: {error}")
            self._save_config_locked()
        self._set_live_progress("idle" if status in {"completed", "canceled"} else status, None)

    def _clear_active_locked(self) -> None:
        self._config.active_job_id = ""
        self._config.active_kind = ""
        self._config.active_prompt_preview = ""
        self._config.started_at_utc = ""
        self._config.updated_at_utc = _utc_now_iso()

    def _poll_status_label(self) -> str:
        with self._config_lock:
            if self._config.active_job_id:
                return f"working:{self._config.active_job_id}"
            return self._config.status or "polling"

    def _render_status(self) -> str:
        config = self._config_snapshot()
        live_status, live_progress = self._live_progress_snapshot()
        lines = [
            f"Version: {CONNECTOR_VERSION}",
            f"Status: {config.status or 'unknown'}",
            f"Last event: {config.last_event or '-'}",
            f"Token saved: {'yes' if bool(config.token) else 'no'}",
            f"JPEG fallback before upload: {'on' if config.compress_images_before_upload else 'off'}",
            f"Result upload to bot: {'off' if config.keep_result_on_pc_only else 'on'}",
            f"Manual snapshots: {'on' if config.manual_snapshot_mode else 'off'}",
            f"Worker backend: {config.worker_backend or 'not initialized'}",
            f"Settings source: {config.settings_source or '-'}",
            f"Plugin path: {Path(__file__).resolve()}",
            f"Polls: {config.poll_count}",
        ]
        if config.active_job_id:
            lines.extend(
                [
                    f"Active job: {config.active_job_id}",
                    f"Kind: {config.active_kind or '-'}",
                    f"Elapsed: {_format_elapsed(config.started_at_utc)}",
                    f"Live stage: {live_status}{f' ({live_progress}%)' if live_progress is not None else ''}",
                    f"Prompt: {config.active_prompt_preview or '-'}",
                ]
            )
        if config.last_duration_seconds:
            lines.append(f"Last duration: {config.last_duration_seconds:.1f}s")
        if self._active_upload_hint():
            lines.append(f"Upload mode cache: {self._active_upload_hint()}")
        if self._active_reference_hint():
            lines.append(f"Reference download cache: {self._active_reference_hint()}")
        notice = self._network_fallback_notice()
        if notice:
            lines.append(notice)
        if config.last_error:
            lines.append(f"Last error: {config.last_error}")
        if config.diagnostics:
            lines.append("Last diagnostics:")
            lines.extend(f"- {item}" for item in config.diagnostics[-8:])
        return "\n".join(lines)

    def _render_activity_html(self) -> str:
        config = self._config_snapshot()
        live_status, live_progress = self._live_progress_snapshot()
        active = bool(config.active_job_id)
        label = "RUNNING" if active else ("ENABLED" if config.enabled else "DISABLED")
        color = "#c5221f" if active else ("#188038" if config.enabled else "#5f6368")
        progress_text = f" {live_progress}%" if active and live_progress is not None else ""
        job_text = f" job {config.active_job_id}" if active else ""
        stage = html.escape(live_status or config.last_event or "-")
        job_text = html.escape(job_text)
        notice = self._network_fallback_notice()
        notice_html = (
            "<div style='margin-top:8px;padding:7px 9px;border-radius:6px;"
            "background:#fff4ce;border:1px solid #f4c430;color:#5f4200;font-size:13px;'>"
            f"{html.escape(notice)}</div>"
            if notice
            else ""
        )
        return (
            "<div style='border:1px solid #dadce0;border-radius:8px;padding:10px 12px;"
            "font-size:16px;line-height:1.35;background:#fff;'>"
            f"<span style='display:inline-block;width:12px;height:12px;border-radius:50%;"
            f"background:{color};margin-right:8px;vertical-align:-1px;'></span>"
            f"<b>STATUS: {label}{progress_text}</b>{job_text}<br>"
            f"<span style='font-size:13px;color:#5f6368;'>Stage: {stage}</span>"
            f"{notice_html}"
            "</div>"
        )

    def _reference_gallery_value(self) -> list[str]:
        runtime = self._active_runtime
        if runtime is None:
            return []
        return [path for path in runtime.reference_paths if Path(path).is_file()]

    def _ui_tick_status(self) -> tuple[str, str, list[str]]:
        return self._render_activity_html(), self._render_status(), self._reference_gallery_value()

    def _ui_tick(self, *, include_snapshot_json: bool = False):
        config = self._config_snapshot()
        values = [self._render_activity_html(), self._render_status(), self._reference_gallery_value()]
        if include_snapshot_json:
            return (
                config.default_settings_json,
                config.default_video_settings_json,
                *values,
            )
        return tuple(values)

    def _load_config(self) -> FilexaConfig:
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return FilexaConfig()
        except Exception:
            return FilexaConfig(status="disabled", last_error="Could not read connector config")
        config = FilexaConfig()
        for key in asdict(config):
            if key in payload:
                setattr(config, key, payload[key])
        if not isinstance(config.diagnostics, list):
            config.diagnostics = []
        config.diagnostics = [_short_text(str(item), 400) for item in config.diagnostics[-8:]]
        config.last_success_settings_json = _normalize_snapshot_text(config.last_success_settings_json)
        config.last_success_video_settings_json = _normalize_snapshot_text(
            config.last_success_video_settings_json
        )
        config.default_settings_json = _normalize_snapshot_text(config.default_settings_json)
        config.default_video_settings_json = _normalize_snapshot_text(config.default_video_settings_json)
        config.default_settings_json = _restore_snapshot_text(
            config.default_settings_json,
            config.last_success_settings_json,
            "image",
        )
        config.default_video_settings_json = _restore_snapshot_text(
            config.default_video_settings_json,
            config.last_success_video_settings_json,
            "video",
        )
        if config.active_job_id:
            config.active_job_id = ""
            config.status = "enabled" if config.enabled else "disabled"
            config.last_event = "Recovered after WanGP restart"
        return config

    def _save_config_locked(self) -> None:
        self._config_path.write_text(json.dumps(asdict(self._config), indent=2, ensure_ascii=False), encoding="utf-8")

    def _debug(self, message: str) -> None:
        with self._config_lock:
            self._remember_diagnostic_locked(message)
            self._save_config_locked()

    def _remember_diagnostic_locked(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        entry = f"{stamp} {_short_text(message, 400)}"
        diagnostics = list(self._config.diagnostics or [])
        diagnostics.append(entry)
        self._config.diagnostics = diagnostics[-8:]

    def _remember_diagnostic(self, message: str) -> None:
        with self._config_lock:
            self._remember_diagnostic_locked(message)
            self._save_config_locked()

    def _active_upload_hint(self) -> str:
        with self._config_lock:
            if _parse_iso(self._config.upload_mode_hint_until_utc) <= datetime.now(timezone.utc):
                return ""
            return self._config.upload_mode_hint

    def _remember_upload_hint(self, mode: str) -> None:
        with self._config_lock:
            self._config.upload_mode_hint = mode
            self._config.upload_mode_hint_until_utc = (
                datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_MODE_HINT_TTL_SECONDS)
            ).isoformat()
            self._save_config_locked()

    def _active_reference_hint(self) -> str:
        with self._config_lock:
            if _parse_iso(self._config.reference_download_mode_hint_until_utc) <= datetime.now(timezone.utc):
                return ""
            return self._config.reference_download_mode_hint

    def _remember_reference_hint(self, mode: str) -> None:
        with self._config_lock:
            self._config.reference_download_mode_hint = mode
            self._config.reference_download_mode_hint_until_utc = (
                datetime.now(timezone.utc) + timedelta(seconds=REFERENCE_MODE_HINT_TTL_SECONDS)
            ).isoformat()
            self._save_config_locked()

    def _network_fallback_notice(self) -> str:
        if self._active_upload_hint() or self._active_reference_hint():
            return "⚠️ Unstable network, chunk transfer method temporarily enabled."
        return ""

    def _sleep_interruptible(self, seconds: float) -> None:
        self._worker_stop.wait(seconds)


def _is_filexa_server_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, FilexaHttpError):
        return exc.status_code >= 500
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(current, OSError) and getattr(current, "winerror", None) in {
            10051,  # network unreachable
            10060,  # connection timed out
            10061,  # connection refused
            11001,  # host not found
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def _json_object(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("JSON settings must be an object")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _sanitize_settings_snapshot(settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in settings.items():
        key_text = str(key)
        if _is_reference_path_setting(key_text, value):
            continue
        if isinstance(value, dict):
            clean[key_text] = _sanitize_settings_snapshot(value)
        elif isinstance(value, list):
            clean[key_text] = [
                _sanitize_settings_snapshot(item) if isinstance(item, dict) else copy.deepcopy(item)
                for item in value
            ]
        else:
            clean[key_text] = copy.deepcopy(value)
    return clean


def _clear_task_reference_settings(settings: dict[str, Any]) -> None:
    if not isinstance(settings, dict):
        return
    for key in list(settings.keys()):
        if _is_reference_path_setting(str(key), settings.get(key)):
            settings.pop(key, None)
    if "image_prompt_type" in settings:
        settings["image_prompt_type"] = _remove_sequence_letter(str(settings.get("image_prompt_type") or ""), "S")
    if "video_prompt_type" in settings:
        settings["video_prompt_type"] = _remove_sequence_letter(str(settings.get("video_prompt_type") or ""), "I")


def _is_reference_path_setting(key: str, value: Any) -> bool:
    clean = str(key or "").strip().lower()
    if clean in SNAPSHOT_REFERENCE_PATH_KEYS:
        return True
    if ("ref" in clean or "reference" in clean) and _contains_pathlike_string(value):
        return True
    if clean.endswith(("_image", "_images", "_path", "_paths")) and _contains_pathlike_string(value):
        return True
    return False


def _contains_pathlike_string(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        if re.search(r"^[A-Za-z]:[\\/]", text):
            return True
        if text.startswith(("/", "\\")):
            return True
        if "\\" in text or "/" in text:
            suffix = Path(text).suffix.lower()
            return suffix in {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
        return False
    if isinstance(value, list):
        return any(_contains_pathlike_string(item) for item in value)
    if isinstance(value, tuple):
        return any(_contains_pathlike_string(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_pathlike_string(item) for item in value.values())
    return False


def _remove_sequence_letter(value: str, letter: str) -> str:
    return "".join(char for char in str(value or "") if char != letter)


def _binding_value(spec: Any, paths: list[str]) -> Any:
    if isinstance(spec, int):
        return paths[spec] if 0 <= spec < len(paths) else None
    if isinstance(spec, list):
        selected = []
        for item in spec:
            if isinstance(item, int) and 0 <= item < len(paths):
                selected.append(paths[item])
        return selected
    if spec == "all":
        return paths
    if spec == "first":
        return paths[0] if paths else None
    return None


def _worker_error_message(error: BaseException) -> str:
    text = str(error) or error.__class__.__name__
    if "live Gradio request with a session hash" in text:
        return (
            f"{text} The Filexa2Wan2GP {CONNECTOR_VERSION} worker should use the headless "
            "WanGP session; restart WanGP after updating the plugin and confirm the Status panel "
            "shows Worker backend: headless(shared.api.init)."
        )
    return text


def _settings_has_model_type(settings: dict[str, Any]) -> bool:
    return bool(isinstance(settings, dict) and str(settings.get("model_type") or "").strip())


def _snapshot_kind_for_task(kind: str) -> str:
    return "video" if str(kind or "") == "video" else "image"


def _normalize_snapshot_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    try:
        settings = _sanitize_settings_snapshot(_settings_from_payload(_json_object(clean)))
    except Exception:
        return clean
    return json.dumps(settings, indent=2, ensure_ascii=False)


def _restore_snapshot_text(current_text: str, last_success_text: str, target: str) -> str:
    if _snapshot_text_has_kind(current_text, target):
        return current_text
    if _snapshot_text_has_kind(last_success_text, target):
        return last_success_text
    return "" if str(current_text or "").strip() else current_text


def _snapshot_text_has_kind(text: str, target: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    try:
        settings = _sanitize_settings_snapshot(_settings_from_payload(_json_object(clean)))
    except Exception:
        return False
    return _settings_has_model_type(settings) and _settings_media_kind_guess(settings) == target


def _settings_media_kind_guess(settings: dict[str, Any]) -> str:
    mode = str(settings.get("mode") or "").strip().lower()
    if mode in {"edit_audio", "edit_remux"}:
        return "audio"
    model_type = _settings_model_type(settings).lower()
    if _model_type_looks_video(model_type):
        return "video"
    if _model_type_looks_image(model_type):
        return "image"
    try:
        image_mode = int(settings.get("image_mode") or 0)
    except (TypeError, ValueError):
        image_mode = 0
    return "image" if image_mode > 0 else "video"


def _model_type_looks_video(model_type: str) -> bool:
    hints = (
        "i2v",
        "t2v",
        "v2v",
        "wan",
        "ltx",
        "hunyuan",
        "skyreels",
        "cogvideo",
        "mochi",
        "video",
    )
    return any(hint in model_type for hint in hints)


def _model_type_looks_image(model_type: str) -> bool:
    hints = (
        "qwen_image",
        "image_edit",
        "flux",
        "kolors",
        "sdxl",
        "sd3",
        "stable_diffusion",
        "image",
    )
    return any(hint in model_type for hint in hints)


def _settings_model_type(settings: dict[str, Any]) -> str:
    if not isinstance(settings, dict):
        return ""
    return str(settings.get("model_type") or "").strip()


def _result_model_type(settings: dict[str, Any]) -> str:
    return _short_text(_settings_model_type(settings), 50)


def _model_accepts_image_refs(model_def: dict[str, Any]) -> bool:
    if not isinstance(model_def, dict):
        return False
    if bool(model_def.get("one_image_ref_needed", False)):
        return True
    if bool(model_def.get("at_least_one_image_ref_needed", False)):
        return True
    choices = model_def.get("image_ref_choices")
    return isinstance(choices, dict) and bool(choices.get("choices"))


def _settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("params"), dict):
        return copy.deepcopy(payload["params"])
    return copy.deepcopy(payload)


def _state_model_type(state: Any) -> str:
    if not isinstance(state, dict):
        return ""
    key = "model_type" if state.get("active_form", "add") == "add" else "edit_model_type"
    value = state.get(key)
    if value:
        return str(value).strip()
    fallback = str(state.get("model_type") or state.get("edit_model_type") or "").strip()
    if fallback:
        return fallback
    all_settings = state.get("all_settings")
    if isinstance(all_settings, dict) and len(all_settings) == 1:
        only_key = next(iter(all_settings.keys()))
        return str(only_key or "").strip()
    return ""


def _add_sequence_letter(value: str, letter: str) -> str:
    clean = str(value or "")
    return clean if letter in clean else f"{clean}{letter}"


def _make_timer():
    timer_factory = getattr(gr, "Timer", None)
    if timer_factory is None:
        return None
    try:
        return timer_factory(value=3.0, active=True)
    except TypeError:
        try:
            return timer_factory(3.0)
        except Exception:
            return None
    except Exception:
        return None


def _clean_base_url(value: str) -> str:
    parsed = _require_base_url(value)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _require_base_url(value: str):
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid Filexa API URL")
    return parsed


def _same_origin(left: Any, right: Any) -> bool:
    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and _effective_port(left) == _effective_port(right)
    )


def _effective_port(parsed: Any) -> int:
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme.lower() == "https" else 80


def _clean_mime(value: str) -> str:
    clean = str(value or "").split(";", 1)[0].strip().lower()
    return "image/jpeg" if clean == "image/jpg" else clean


def _mime_from_magic(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return ""


def _validate_image_bytes(data: bytes, mime_type: str) -> None:
    if not data or _mime_from_magic(data[:16]) != mime_type:
        raise ValueError("Image bytes do not match declared MIME type")


def _validate_video_bytes(data: bytes, mime_type: str) -> None:
    detected = _mime_from_magic(data[:16])
    if not data or (detected != mime_type and not (mime_type == "video/quicktime" and detected == "video/mp4")):
        raise ValueError("Video bytes do not match declared MIME type")


def _extension_for_mime(mime_type: str) -> str:
    return { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp" }.get(mime_type, ".bin")


def _safe_filename(value: str) -> str:
    name = Path(value).name.strip()[:120]
    name = re.sub(r"[^A-Za-z0-9_. -]", "_", name)
    return name or f"reference-{uuid.uuid4().hex}.bin"


def _short_text(value: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    return clean[:limit]


def _coerce_progress(value: Any) -> int | None:
    if value is None:
        return None
    try:
        clean = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, clean))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_elapsed(started_at: str) -> str:
    start = _parse_iso(started_at)
    if start.year <= 1900:
        return "-"
    seconds = max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"

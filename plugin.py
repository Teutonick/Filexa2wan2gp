from __future__ import annotations

import base64
import copy
import json
import math
import mimetypes
import re
import shutil
import tempfile
import threading
import time
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
CONNECTOR_VERSION = "0.1.0"
CONNECTOR_ID = "Filexa2Wan2GPConnector"
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


@dataclass
class FilexaConfig:
    enabled: bool = False
    api_url: str = ""
    token: str = ""
    debug_logging: bool = False
    keep_result_on_pc_only: bool = False
    compress_images_before_upload: bool = True
    default_settings_json: str = ""
    default_video_settings_json: str = ""
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
    ) -> None:
        headers = {
            "Content-Type": payload.mime_type,
            "Content-Length": str(len(payload.bytes)),
            "Connection": "close",
        }
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
        if response.status_code == 401:
            raise FilexaUnauthorizedError("Filexa returned 401 Unauthorized; reconnect with a new token.")
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
        self.url = "https://github.com/Teutonick/Filexa2wan2gp"
        self._config_path = Path(__file__).with_name("filexa2wan2gp_config.json")
        self._config_lock = threading.RLock()
        self._config = self._load_config()
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._api_session: Any = None
        self._active_runtime: TaskRuntime | None = None
        self._active_job: Any = None

    def setup_ui(self) -> None:
        self.request_component("state")
        self.request_global("get_current_model_settings")
        self.add_tab(
            tab_id=CONNECTOR_ID,
            label="Filexa2Wan2GP Connector",
            component_constructor=self.create_ui,
        )

    def create_ui(self, api_session: Any) -> None:
        self._api_session = api_session
        self._ensure_worker()

        with gr.Column():
            gr.Markdown(
                "### Filexa2Wan2GP Connector\n"
                "Connect this WanGP instance to Filexa local generation. All traffic is outbound "
                "from this PC to the configured Filexa API URL."
            )
            with gr.Row():
                api_url = gr.Textbox(label="Filexa API URL", value=self._config.api_url, scale=2)
                token = gr.Textbox(label="Filexa token", value="", type="password", scale=2)
            with gr.Row():
                enabled = gr.Checkbox(label="Enable connector", value=self._config.enabled)
                debug_logging = gr.Checkbox(label="Debug logging", value=self._config.debug_logging)
                compress_images = gr.Checkbox(
                    label="JPEG fallback before upload",
                    value=self._config.compress_images_before_upload,
                )
                local_only = gr.Checkbox(
                    label="Keep result on this PC only",
                    value=self._config.keep_result_on_pc_only,
                )
            settings_json = gr.Textbox(
                label="Default WanGP image/settings JSON",
                value=self._config.default_settings_json,
                lines=14,
                placeholder=(
                    "Optional. Paste a WanGP Export Settings JSON or click Capture current WanGP settings. "
                    "Filexa task params are merged over these defaults and Filexa prompt always wins."
                ),
            )
            video_settings_json = gr.Textbox(
                label="Default WanGP video settings JSON",
                value=self._config.default_video_settings_json,
                lines=14,
                placeholder=(
                    "Optional. Paste/capture a WanGP settings JSON configured for text-to-video or image-to-video. "
                    "Video Filexa tasks use this first, then task params, then the Filexa prompt/reference."
                ),
            )
            with gr.Row():
                save_btn = gr.Button("Save / reconnect", variant="primary")
                capture_btn = gr.Button("Capture current settings as image defaults")
                capture_video_btn = gr.Button("Capture current settings as video defaults")
                refresh_btn = gr.Button("Refresh status")
                cancel_btn = gr.Button("Cancel active task")
                disconnect_btn = gr.Button("Disconnect")
            status = gr.Textbox(label="Status", value=self._render_status(), lines=12, interactive=False)
            capture_image_target = gr.State("image")
            capture_video_target = gr.State("video")

        save_btn.click(
            fn=self._ui_save,
            inputs=[
                api_url,
                token,
                enabled,
                debug_logging,
                compress_images,
                local_only,
                settings_json,
                video_settings_json,
            ],
            outputs=[status],
            queue=False,
        )
        capture_btn.click(
            fn=self._ui_capture_current_settings,
            inputs=[self.state, capture_image_target],
            outputs=[settings_json, status],
            queue=False,
        )
        capture_video_btn.click(
            fn=self._ui_capture_current_settings,
            inputs=[self.state, capture_video_target],
            outputs=[video_settings_json, status],
            queue=False,
        )
        refresh_btn.click(fn=self._render_status, outputs=[status], queue=False)
        cancel_btn.click(fn=self._ui_cancel_active_task, outputs=[status], queue=False)
        disconnect_btn.click(fn=self._ui_disconnect, outputs=[status], queue=False)

    def _ui_save(
        self,
        api_url: str,
        token: str,
        enabled: bool,
        debug_logging: bool,
        compress_images: bool,
        local_only: bool,
        settings_json: str,
        video_settings_json: str,
    ) -> str:
        with self._config_lock:
            config = copy.deepcopy(self._config)
            config.api_url = _clean_base_url(api_url) if api_url.strip() or enabled else ""
            if token.strip():
                if not SAFE_TOKEN_RE.fullmatch(token.strip()):
                    raise gr.Error("Invalid Filexa token shape.")
                config.token = token.strip()
            if enabled and (not config.api_url or not config.token):
                raise gr.Error("Filexa API URL and token are required before enabling the connector.")
            if settings_json.strip():
                _json_object(settings_json)
            if video_settings_json.strip():
                _json_object(video_settings_json)
            config.default_settings_json = settings_json.strip()
            config.default_video_settings_json = video_settings_json.strip()
            config.enabled = bool(enabled)
            config.debug_logging = bool(debug_logging)
            config.compress_images_before_upload = bool(compress_images)
            config.keep_result_on_pc_only = bool(local_only)
            config.status = "enabled" if config.enabled else "disabled"
            config.last_event = "Configuration saved"
            config.last_error = ""
            self._config = config
            self._save_config_locked()
        self._ensure_worker()
        return self._render_status()

    def _ui_capture_current_settings(self, state: dict[str, Any], target: str = "image") -> tuple[str, str]:
        if not callable(getattr(self, "get_current_model_settings", None)):
            raise gr.Error("WanGP did not expose current settings to this plugin.")
        try:
            settings = self.get_current_model_settings(state)
        except Exception as exc:
            raise gr.Error(f"Could not read current WanGP settings: {exc}") from exc
        if not isinstance(settings, dict):
            raise gr.Error("WanGP returned invalid settings.")
        clean = copy.deepcopy(settings)
        clean.pop("client_id", None)
        captured = json.dumps(clean, indent=2, ensure_ascii=False)
        with self._config_lock:
            if target == "video":
                self._config.default_video_settings_json = captured
                self._config.last_event = "Captured current WanGP video settings"
            else:
                self._config.default_settings_json = captured
                self._config.last_event = "Captured current WanGP image settings"
            self._config.last_error = ""
            self._save_config_locked()
        return captured, self._render_status()

    def _ui_disconnect(self) -> str:
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
        return self._render_status()

    def _ui_cancel_active_task(self) -> str:
        runtime = self._active_runtime
        if runtime is None:
            with self._config_lock:
                self._config.last_event = "No active task to cancel"
                self._save_config_locked()
            return self._render_status()
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
        with self._config_lock:
            self._config.status = "canceling"
            self._config.last_event = "Cancel requested"
            self._save_config_locked()
        return self._render_status()

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
                if self._api_session is None:
                    self._set_status("waiting", "Waiting for WanGP session")
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
                self._set_error("unauthorized", str(exc), disable=True)
                self._sleep_interruptible(POLL_DELAY_SECONDS)
            except Exception as exc:
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
            self._post_task_status_safe(client, task, "generating in WanGP", 15)
            job = self._api_session.submit_task(wangp_task, callbacks=callbacks)
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
            self._post_task_status_safe(client, task, "uploading result", 94)
            self._deliver_output(client, task, output_path)
            self._finish_runtime("completed", f"Task complete: {output_path.name}", started_at)
        except FilexaUnauthorizedError:
            raise
        except Exception as exc:
            self._report_failure_safe(client, task, str(exc))
            self._finish_runtime("failed", f"Task failed: {_short_text(str(exc), 300)}", started_at, error=str(exc))
        finally:
            self._active_job = None
            self._active_runtime = None
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _build_wangp_task(self, task: dict[str, Any], client: FilexaClient, temp_dir: Path) -> dict[str, Any]:
        base = self._default_wangp_settings(str(task.get("kind") or ""))
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
        self._attach_references(task, client, temp_dir, prepared_params, params)
        prepared["id"] = task["job_id"]
        prepared["params"] = prepared_params
        prepared.setdefault("plugin_data", {})
        return prepared

    def _default_wangp_settings(self, kind: str = "") -> dict[str, Any]:
        with self._config_lock:
            text = (
                self._config.default_video_settings_json
                if kind == "video" and self._config.default_video_settings_json.strip()
                else self._config.default_settings_json
            )
        if not text.strip():
            return {}
        payload = _json_object(text)
        if isinstance(payload.get("params"), dict):
            return copy.deepcopy(payload["params"])
        return copy.deepcopy(payload)

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
            settings.setdefault("image_start", paths[0])
            settings.setdefault("image_refs", paths)

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

    def _deliver_output(self, client: FilexaClient, task: dict[str, Any], path: Path) -> None:
        payload = self._media_payload_from_path(path)
        if payload is None:
            self._post_task_status_safe(client, task, "completed locally", 100)
            self._report_complete(
                client,
                task,
                "WanGP produced a file that Filexa cannot upload yet. The result stayed on your PC.",
            )
            return
        if self._config_snapshot().keep_result_on_pc_only:
            self._post_task_status_safe(client, task, "completed locally", 100)
            self._report_complete(client, task)
            return
        if payload.mime_type in SUPPORTED_VIDEO_MIMES:
            self._deliver_video_output(client, task, payload)
            return
        self._deliver_image_output(client, task, payload)

    def _deliver_video_output(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload) -> None:
        if len(payload.bytes) > MAX_UPLOAD_VIDEO_BYTES:
            self._post_task_status_safe(client, task, "video kept on this PC", 100)
            self._report_complete(
                client,
                task,
                "WanGP generated a video, but it is larger than Filexa's 50 MB direct upload limit. "
                "The file stayed on your PC.",
            )
            return
        try:
            self._post_task_status_safe(client, task, "uploading video result", 96)
            client.post_bytes(
                str(task.get("result_upload_url") or ""),
                payload,
                timeout=DIRECT_UPLOAD_TIMEOUT,
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
            )
        except Exception as exc:
            self._debug(f"Video direct upload failed: {exc}")
            self._report_complete(
                client,
                task,
                "WanGP generated the video, but direct upload to Filexa failed. "
                "The file stayed on your PC; check the network path before retrying.",
            )

    def _deliver_image_output(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload) -> None:
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
            self._report_complete(client, task)
            return
        preferred = self._active_upload_hint()
        if preferred in {UPLOAD_MODE_TEXT_FAST, UPLOAD_MODE_TEXT_SAFE}:
            self._upload_text_chunks_adaptive(client, task, fallback, preferred=preferred)
            return
        try:
            self._upload_binary_chunks(client, task, fallback)
            return
        except FilexaUnauthorizedError:
            raise
        except FilexaHttpError as exc:
            if exc.status_code == 410:
                raise
            self._debug(f"Binary chunk upload failed: {exc}")
        except Exception as exc:
            self._debug(f"Binary chunk upload failed: {exc}")
        self._upload_text_chunks_adaptive(client, task, fallback)

    def _upload_binary_chunks(self, client: FilexaClient, task: dict[str, Any], payload: UploadPayload) -> None:
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
            )

    def _upload_text_chunks_adaptive(
        self,
        client: FilexaClient,
        task: dict[str, Any],
        payload: UploadPayload,
        *,
        preferred: str = "",
    ) -> None:
        if preferred != UPLOAD_MODE_TEXT_SAFE:
            try:
                self._upload_text_chunks(client, task, payload, TEXT_CHUNK_BYTES_FAST, JSON_CHUNK_FAST_DELAY)
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
        self._upload_text_chunks(client, task, payload, TEXT_CHUNK_BYTES_SAFE, JSON_CHUNK_SAFE_DELAY)
        self._remember_upload_hint(UPLOAD_MODE_TEXT_SAFE)

    def _upload_text_chunks(
        self,
        client: FilexaClient,
        task: dict[str, Any],
        payload: UploadPayload,
        chunk_bytes: int,
        delay: float,
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

    def _report_complete(self, client: FilexaClient, task: dict[str, Any], message: str | None = None) -> None:
        payload = {"message": _short_text(message, 500)} if message else {}
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
        if not path:
            return
        try:
            client.post_json(
                path,
                {"status": _short_text(status, 120), "progress": _coerce_progress(progress)},
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

    def _set_error(self, status: str, error: str, *, disable: bool = False) -> None:
        with self._config_lock:
            if disable:
                self._config.enabled = False
            self._config.status = status
            self._config.last_error = _short_text(error, 1000)
            self._config.last_event = _short_text(error, 300)
            self._config.updated_at_utc = _utc_now_iso()
            self._save_config_locked()

    def _finish_runtime(self, status: str, event: str, started_at: float, *, error: str = "") -> None:
        with self._config_lock:
            self._config.status = "enabled" if status == "completed" else status
            self._config.last_event = event
            self._config.last_error = _short_text(error, 1000)
            self._config.last_duration_seconds = round(max(0.0, time.monotonic() - started_at), 1)
            self._clear_active_locked()
            self._save_config_locked()

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
        lines = [
            f"Status: {config.status or 'unknown'}",
            f"Last event: {config.last_event or '-'}",
            f"Token saved: {'yes' if bool(config.token) else 'no'}",
            f"Debug logging: {'on' if config.debug_logging else 'off'}",
            f"JPEG fallback before upload: {'on' if config.compress_images_before_upload else 'off'}",
            f"Result upload to bot: {'off' if config.keep_result_on_pc_only else 'on'}",
            f"Polls: {config.poll_count}",
        ]
        if config.active_job_id:
            lines.extend(
                [
                    f"Active job: {config.active_job_id}",
                    f"Kind: {config.active_kind or '-'}",
                    f"Elapsed: {_format_elapsed(config.started_at_utc)}",
                    f"Prompt: {config.active_prompt_preview or '-'}",
                ]
            )
        if config.last_duration_seconds:
            lines.append(f"Last duration: {config.last_duration_seconds:.1f}s")
        if self._active_upload_hint():
            lines.append(f"Upload mode cache: {self._active_upload_hint()}")
        if self._active_reference_hint():
            lines.append(f"Reference download cache: {self._active_reference_hint()}")
        if config.last_error:
            lines.append(f"Last error: {config.last_error}")
        return "\n".join(lines)

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
        if config.active_job_id:
            config.active_job_id = ""
            config.status = "enabled" if config.enabled else "disabled"
            config.last_event = "Recovered after WanGP restart"
        return config

    def _save_config_locked(self) -> None:
        self._config_path.write_text(json.dumps(asdict(self._config), indent=2, ensure_ascii=False), encoding="utf-8")

    def _debug(self, message: str) -> None:
        if self._config_snapshot().debug_logging:
            print(f"[{CONNECTOR_NAME}] {_short_text(message, 1000)}")

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

    def _sleep_interruptible(self, seconds: float) -> None:
        self._worker_stop.wait(seconds)


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

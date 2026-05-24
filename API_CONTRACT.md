# Filexa2Wan2GP Connector API Contract

This contract describes the bot-side API a third-party bot/server must implement to reuse the
Filexa2Wan2GP Connector plugin.

Version: 2026-05-24.

The plugin has no public inbound HTTP API for bots. It polls an outbound Filexa-compatible API,
then submits tasks to WanGP through WanGP's in-process `shared.api` worker.

## Required Bot API

Implement the Filexa local connector API described in:

`../../docs/LOCAL_GENERATION_CONNECTOR_API_CONTRACT.md`

The plugin calls these routes:

- `POST /local/v1/tasks/poll`
- `GET /local/v1/tasks/{job_id}/references/{index}`
- `GET /local/v1/tasks/{job_id}/references/{index}/text-chunks/{chunk_index}`
- `POST /local/v1/tasks/{job_id}/status`
- `POST /local/v1/tasks/{job_id}/result`
- `POST /local/v1/tasks/{job_id}/result/chunks/{index}`
- `POST /local/v1/tasks/{job_id}/result/text-chunks/{index}`
- `POST /local/v1/tasks/{job_id}/complete`
- `POST /local/v1/tasks/{job_id}/failure`
- `POST /local/v1/tasks/{job_id}/cancel`

All requests use `Authorization: Bearer <token>` and `X-Filexa-Connector-Version`.

## Task Contract

Supported task kinds:

- `image`
- `image_edit`
- `video`

Required task fields:

```json
{
  "job_id": "0123456789abcdef0123456789abcdef",
  "kind": "video",
  "engine": "wangp",
  "client_type": "wangp",
  "prompt": "A short cinematic clip",
  "profile": "default",
  "model": "default",
  "params": {},
  "references": [],
  "deadline_at": "2026-05-24T12:00:00+00:00",
  "result_upload_url": "/local/v1/tasks/<job_id>/result",
  "result_chunk_upload_url": "/local/v1/tasks/<job_id>/result/chunks",
  "result_text_chunk_upload_url": "/local/v1/tasks/<job_id>/result/text-chunks",
  "result_complete_url": "/local/v1/tasks/<job_id>/complete",
  "status_url": "/local/v1/tasks/<job_id>/status",
  "failure_url": "/local/v1/tasks/<job_id>/failure",
  "cancel_url": "/local/v1/tasks/<job_id>/cancel"
}
```

Validation expectations:

- `job_id`: 32 hex characters.
- `prompt`: non-empty, max 8000 characters, no control characters except common whitespace.
- `engine` and `client_type`: `wangp`.
- `params`: object.
- `references`: max four image references; Filexa currently sends one for local I2V.

## WanGP Settings Modes

Default mode:

- The plugin captures the current matching WanGP image/video settings snapshot on tab open and
  again before a task.
- Image tasks use the image snapshot; video tasks use the video snapshot.
- Snapshots are validated with `model_type`, `image_mode`, and WanGP model metadata.
- Task-specific reference paths are stripped from saved snapshots.
- Last successful snapshots are persisted separately and used only if the current/saved snapshot is
  missing, invalid, or the wrong media kind.

Manual snapshot mode:

- The plugin does not auto-refresh snapshots.
- Users must click `Update image snapshot` or `Update video snapshot` in the plugin tab.

## Advanced Task Overrides

`params.wangp_task`

If present, the plugin treats it as a full WanGP task payload. Filexa prompt, output API settings,
and references are still applied afterwards.

`params.reference_bindings`

Optional object mapping WanGP parameter names to reference selectors:

```json
{
  "reference_bindings": {
    "image_start": "first",
    "image_refs": "all",
    "custom_ref": 0,
    "custom_refs": [0, 1]
  }
}
```

Without explicit bindings, `image_edit` and `video` tasks place the first reference in
`image_start`, all references in `image_refs`, and enable WanGP reference prompt mode when the
selected model definition advertises it.

## Result Metadata

After successful generation, the plugin reports the actual WanGP `params.model_type` that was sent
to `shared.api`.

- Direct upload: `X-Filexa-Model-Type: <model_type>`.
- Binary chunk upload: `X-Filexa-Model-Type: <model_type>`.
- JSON/base64 chunk upload: `"model_type": "<model_type>"`.
- Local-only completion: `"model_type": "<model_type>"`.

Bots should truncate this value to 50 characters before displaying or storing it. Filexa uses it in
Telegram captions, for example: `WanGP ltx2_22B_distilled_1_1`.

## Result Handling

Images:

- direct raw PNG/JPEG/WebP upload capped at 40 MiB;
- optional JPEG conversion before upload;
- binary chunks of 50 KiB for compressed results up to 3 MiB;
- JSON/base64 chunks of 8 KiB and then 4 KiB safe mode;
- `/complete` if upload is disabled, impossible, or the file remains too large.

Videos:

- direct MP4/WebM/MOV upload capped at 50 MiB;
- no video chunk fallback;
- `/complete` if direct video upload is impossible or too large.

While upload/reference chunk-mode cache is active, the plugin shows:

`⚠️ Unstable network, chunk transfer method temporarily enabled.`

## Bot Compatibility Notes

- Return `410 Gone` when the task is no longer waiting; the plugin treats it as terminal.
- Keep task URLs on the same origin as the configured API URL.
- Do not long-poll; the plugin polls every 10 seconds.
- If the token is invalid or the bot API is unavailable, the plugin disables itself until the user
  manually reconnects.

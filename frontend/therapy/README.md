# Therapy frontend (Neural Therapy)

Source for the ambient sound-therapy SPA served at `/static/therapy/`
(opened from the Media tab). Binaural beats and soundscapes are synthesized
client-side with the Web Audio API; guided-voice overlays call the backend
`POST /api/tts`, which fronts the wyoming-piper container
(see `modules/media/therapy_tts.py`).

Originally the standalone `music_therapy` podman project on the NUC; the Node
`server.js` and its Piper sidecar were replaced by the FastAPI TTS routes, so
this folder is UI-only.

## Building

The build output (`static/therapy/`) is committed, so node is only needed
when changing this source:

```bash
cd frontend/therapy
corepack pnpm install
corepack pnpm build   # writes static/therapy/
```

# test2 (v2 workspace)

This folder is a v2-focused workspace cloned from `test`.

## Goals
- Keep `test` as a stable v1 baseline.
- Develop and iterate on v2 features in `test2` only.

## Default run commands
- `start.bat` -> runs `main_v2:app`
- `start_v2.bat` -> runs `main_v2:app` and opens `index_v2.html`
- `run.bat` -> installs requirements, runs `main_v2:app`, opens `index_v2.html`

## Notes
- v1 files remain for reference and migration.
- New development should prefer `backend/main_v2.py`, `backend/processing_v2.py`, and `frontend/*_v2.*`.

## Storage mode (for Render limits)
- Default mode is local (`STORAGE_MODE=local`) for zero-config development.
- For production, set `STORAGE_MODE=s3` and provide:
  - `S3_BUCKET`
  - `S3_ENDPOINT_URL` (optional for AWS S3, required for S3-compatible providers like R2)
  - `S3_REGION`
  - `S3_ACCESS_KEY_ID`
  - `S3_SECRET_ACCESS_KEY`
- v2 frontend uploads files to storage first, then sends references to backend (`/api/v2/restore-batch-by-ref`).
- Batch outputs and archives are stored in storage, and download links are generated from storage URLs.

## Cleanup policy
- Temporary processing files in `temp_audio` are cleaned periodically by TTL.
- Local storage mode also applies TTL cleanup (`LOCAL_STORAGE_TTL_HOURS`, default 24).

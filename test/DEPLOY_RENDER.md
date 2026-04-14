# Quick Deploy (Render)

This is the fastest way to run the app on web/mobile without local PowerShell setup each time.

## 1) Push this project to GitHub

Render deploys from a Git repository.

## 2) Create a Render Web Service

- In Render: **New +** -> **Blueprint**
- Connect your GitHub repo
- Render will detect `test/render.yaml`

## 3) Set required environment variable

In Render service settings:

- `GOOGLE_API_KEY` = your Gemini API key

Optional (already defaulted in `render.yaml`):

- `GOOGLE_MODEL` (default: `gemini-1.5-flash`)
- `GOOGLE_MODEL_FALLBACKS`
- `GOOGLE_PLAN_ENDPOINT`

## 4) Deploy

Render will build Docker image from `test/Dockerfile` and expose your app URL.

Health check:

- `https://<your-render-domain>/api/health`

## 5) Access from Android

Open the Render URL directly in Android browser.  
No local environment variable setup required anymore.

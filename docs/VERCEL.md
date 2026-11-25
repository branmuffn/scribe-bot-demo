# The web demo on Vercel

The public demo can run on [Vercel](https://vercel.com) as a read-mostly site with made-up meetings only.

## How it works

- `index.py` is the entry point Vercel loads. On a cold start it builds a Demo mode folder in `/tmp` (the only writable
  place on Vercel) from `scribe_bot/demo_data.py`, then serves the normal web app on it.
- There are no API keys, no worker and no speech models, so nothing is transcribed, no AI is called and nothing costs
  money. Pages that need keys show "No key".
- Changes a visitor makes (renaming, finishing a task, deleting) live only in that instance's `/tmp` copy and are gone
  when Vercel recycles it. Different visitors can land on different instances, so their changes don't mix for long.
- Every page shows one slim line saying the meetings are made up and changes reset (`SCRIBE_BOT_PUBLIC_DEMO=1`, set by
  `index.py`).

## Set it up (once)

1. Sign in at vercel.com (the free Hobby plan is enough) and choose **Add New… › Project**.
2. Import the `scribe-bot-demo` GitHub repository.
3. Leave the settings as Vercel detects them: Framework Preset **FastAPI** (or Other), Root Directory `./`, no build
   command, no environment variables. Never add API keys here.
4. Choose **Deploy**. When it finishes, Vercel shows the site's address (`https://<project>.vercel.app`).

Every push to `main` then redeploys by itself, and each pull request gets its own preview address.

## Keeping it fast (2026-11-25)

- **Small install.** Vercel installs only `[project] dependencies` in `pyproject.toml`. The speech and AI libraries
  (LiteLLM, sherpa-onnx, numpy) sit in the `full` group, so the function bundle went from about 395 MB to about 57 MB
  and cold starts load much less. This `pyproject.toml` differs from the private repo's on purpose: when syncing code
  from scribe-bot, don't copy `pyproject.toml` or `uv.lock` over.
- **Static files from the CDN.** CSS, JS and icons are versioned (`?v=`), and the app marks them
  `public, max-age=31536000, s-maxage=31536000, immutable`, so Vercel's CDN serves them without starting Python.
- **Optional, in the Vercel dashboard:** Project › Settings › Functions › Function Region: pick the region nearest to
  you (for example Washington, D.C., `iad1`, on the US East Coast), then redeploy.

## Stop the Vercel bot's comments on pull requests

Each pull request gets a comment from vercel[bot], which emails the PR's author. To turn the comments off and keep
the preview builds: **Project › Settings › Git › Connected Git Repository**, switch off **Pull Request Comments**
(the `github.silent` setting in `vercel.json` is deprecated and no longer does this).

## Optional: let preview addresses open without a Vercel login

Vercel protects preview deployments with a login by default. Since this repo and its data are public and made up, it
is fine to turn that off so anyone with a preview link can open it: **Project › Settings › Deployment Protection ›
Vercel Authentication › Disabled › Save**.

## Run the same thing locally

```sh
uv sync
SCRIBE_BOT_DATA=./demo-data SCRIBE_BOT_PUBLIC_DEMO=1 uv run uvicorn index:app --port 8000
```

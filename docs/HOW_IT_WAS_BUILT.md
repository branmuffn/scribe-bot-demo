# How Scribe Bot was built

I built Scribe Bot by directing Claude Code: a written spec first, then a decision log, then small steps, each with
tests before it was committed. These are the decisions that shaped it, drawn from the private decision log.

## 1. Measure before choosing

Before writing the app, a first milestone (M0) benchmarked the hard parts on the actual server, a 2014 MacBook Pro
with an Intel i7 and 16 GB of RAM.

- **Speech stays local.** sherpa-onnx with the Parakeet TDT model, voice activity detection, speaker segmentation, and
  speaker embeddings (voiceprints) all run on the server. The audio never leaves home. The engine sits behind an
  interface so it can be swapped; WhisperX was kept only as a fallback.
- **Local language models were tested and dropped.** On a scored test call, local models caught about a third of
  the key items, against roughly 89% for Claude, and took 16 to 33 minutes per 23-minute call while using up to
  13.4 GB of RAM. So the extraction step (summary, tasks, decisions, people notes) moved to Claude, and the local
  model was removed.
- **Own transcription, not the phone's.** The phone's built-in transcript is only an optional cross-check.

## 2. Privacy as rules, not promises

- **Private means private.** A meeting marked Private gets a transcript and search, and nothing from it goes to any
  AI. Turning Private off later goes through a confirm step that says exactly what will be sent and what it costs.
- **Ask first.** While I was testing, every new recording waited in Review with its length, an estimated processing
  time, and an estimated AI cost, and nothing ran until I chose "Transcribe + AI" or "Transcript only".
- **A ledger of everything that leaves.** Every outbound AI call is recorded: when, which account, which model, what
  was sent, the cost, and the provider's training policy. Accounts that may train on your data are blocked for real
  data and usable only in Demo mode.
- **Secrets only in the Keychain.** Keys are entered through a local hidden prompt, masked in the UI, and registered
  with a log redactor so they are scrubbed if they ever reach a log line. A gitleaks hook blocks commits that look
  like secrets.
- **No auto-send.** The app drafts; it never sends messages or posts on my behalf.

## 3. Hard limits on cost

- Per-account caps and a global hard cap ($10 a month), with alerts at 50%, 80% and 100%.
- A cost estimate is shown before any cloud run, and the Costs page is checked against the provider's console.
- The AI router (LiteLLM) is pinned to an exact, vetted version, because the package had malicious releases on PyPI
  in 2026. Upgrades wait for a cooldown and an advisory check.

## 4. A boring stack on purpose

Python, FastAPI, server-rendered HTMX, SQLite with full-text search, and a SQLite-backed job queue. No separate
database server, no Redis, no front-end build step (the CSS is hand-written and HTMX is vendored). One old laptop
runs everything, so fewer moving parts means fewer 3 AM failures.

## 5. Built to recover on its own

- **Every step saves its output.** A job resumes from its last finished step after a crash or a power cut.
- **Catch-up.** Missed schedules run once after downtime, then one "while I was down" report.
- **Backups that are tested.** Nightly encrypted backups (restic, through rclone) to Google Drive, plus a monthly
  automatic restore test whose result is reported.
- **Safe deploys.** One command pulls, syncs, migrates, restarts the services and checks health. If the health check
  fails, it rolls back automatically. The first real deploy included a deliberate failed deploy to prove the rollback.

## 6. Small, reviewable steps

- A locked decision log: changing a decision needs an explicit, dated entry, so the AI assistant doesn't quietly
  re-argue settled choices.
- Tests run before every commit, on synthetic data only, and in GitHub Actions. Real recordings, transcripts and keys
  never go into git, logs, CI, or screenshots.
- "Build now, tune later, with measurement from day one": only blockers were fixed before building, and output quality
  is tuned against a small rated benchmark set after real use.
- A page-speed budget test keeps every main page under 1 second on the old server with a year or two of synthetic data.

## 7. Design choices that came from use

- **1:1 detection.** When my voice plus one other cover nearly all the talk time, the call is treated as a 1:1 and
  re-separated as two speakers, with a button to force it.
- **Audio archive.** After processing, a speech-optimized copy (HE-AAC, about half the size of the original) is
  kept, because it plays in every browser and on Android.
- **Notifications.** Discord carries alerts and "a recording is waiting" pings only, never content; everything else
  lives in the app's notification list.

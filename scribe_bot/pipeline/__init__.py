"""Recording pipeline (M2): ingest -> audio -> transcribe -> diarize -> identify -> ai_extract -> file -> notify.
Each stage is a queue handler (scribe_bot/core/queue.py); the pipeline is registered in scribe_bot/worker.py."""

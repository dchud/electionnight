import os

# Tests must never read real keys or settings from the repository's `.env`.
os.environ["MONITOR_ENV_FILE"] = os.devnull

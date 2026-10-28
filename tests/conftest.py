import os

# Test servers (the e2e suite copies this environment) must never call the real `claude` on the
# Max plan or queue notes on their own. Unit tests fake `claude` and turn these back on.
os.environ.setdefault("LECTURELAB_CLAUDE", "off")
os.environ.setdefault("LECTURELAB_AUTO_NOTES", "off")

"""Entry point for unattended scheduling (launchd/systemd target)."""
import logging
from registry import Registry
from scheduler import serve_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

if __name__ == "__main__":
    # fetch_fn=None until the live Meta connector is bolted on; clients bring CSVs
    # via their inbox in the meantime (run `python -c "from intake import process_inbox; process_inbox()"`
    # on a timer, or extend serve_forever to also drain inboxes).
    serve_forever(Registry())

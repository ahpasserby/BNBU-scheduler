"""Bounded lifecycle helpers for metadata retention."""
import logging
import threading


def start_cleaner(action):
    stop = threading.Event()

    def run():
        while not stop.wait(60):
            try:
                action()
            except Exception:
                logging.getLogger(__name__).warning('Print metadata cleanup could not complete')

    threading.Thread(target=run, name='print-metadata-cleanup', daemon=True).start()
    return stop

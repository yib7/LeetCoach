"""Start LeetCoach: ``python app.py`` (the README's run step; LeetCoach.cmd
runs the same line). The code lives in the ``leetcoach`` package next to this
file. This shim keeps the documented entry point (``leetcoach.launcher.main``)
and re-exports the Flask ``app`` / ``create_app`` so ``flask --app app run``
and WSGI servers that name ``app:app`` keep finding them."""
from leetcoach.app import app, create_app
from leetcoach.launcher import main

__all__ = ["app", "create_app", "main"]

if __name__ == "__main__":
    raise SystemExit(main())

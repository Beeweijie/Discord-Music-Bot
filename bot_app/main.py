"""Compatibility import; implementation lives in bot_app.app.main."""
if __name__ == "__main__":
    from bot_app.app.main import main
    raise SystemExit(main())
else:
    from importlib import import_module
    import sys
    sys.modules[__name__] = import_module("bot_app.app.main")

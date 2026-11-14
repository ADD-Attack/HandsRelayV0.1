"""Historical import/launcher alias for HandsRelay v4.

Normal imports return the current module itself, so integration patches
and state are shared rather than copied into a disconnected namespace.
"""
import sys
import hands_relay_v4_app as _app

if __name__ == "__main__":
    _app.main()
else:
    sys.modules[__name__] = _app

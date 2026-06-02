"""App-level settings for the pacioli ERP core app."""

import os

from py4web.core import required_folder

# mode (default or development)
MODE = os.environ.get("PY4WEB_MODE")

APP_FOLDER = os.path.dirname(__file__)
APP_NAME = os.path.split(APP_FOLDER)[-1]

# the vendored pacioli_erp package lives under apps/pacioli/packages/pacioli_erp/
# (a self-contained repo: pyproject.toml + the importable pacioli_erp/ package).
# That directory is put on sys.path so `import pacioli_erp` resolves the vendored
# copy.  Later this folder can become a git submodule and/or be pip-installed
# for users who do not want the py4web app.
VENDORED_PACKAGE_DIR = os.path.join(APP_FOLDER, "packages", "pacioli_erp")

DB_FOLDER = required_folder(APP_FOLDER, "databases")
DB_URI = "sqlite://storage.db"
DB_POOL_SIZE = 1
DB_MIGRATE = True
DB_FAKE_MIGRATE = False

STATIC_FOLDER = required_folder(APP_FOLDER, "static")
UPLOAD_FOLDER = required_folder(APP_FOLDER, "uploads")
T_FOLDER = required_folder(APP_FOLDER, "translations")

# --- API ---------------------------------------------------------------
# When True every /api/* call must present a valid bearer token (or be a
# logged-in session).  Defaults to open in development for easy local testing.
API_REQUIRE_AUTH = MODE != "development"

# auth
VERIFY_EMAIL = MODE != "development"
PASSWORD_ENTROPY = 0 if MODE == "development" else 50
REQUIRES_APPROVAL = False
LOGIN_AFTER_REGISTRATION = MODE == "development"
ALLOWED_ACTIONS = ["all"]

SMTP_SSL = False
SMTP_SERVER = None
SMTP_SENDER = "you@example.com"
SMTP_LOGIN = "username:password"
SMTP_TLS = False

SESSION_TYPE = "cookies"
SESSION_SECRET_KEY = "change-me-please-0123456789"

LOGGERS = ["warning:stdout"]
DEFAULT_LOGIN_ENABLED = True

USE_SCHEDULER = False
SCHEDULER_MAX_CONCURRENT_RUNS = 1

try:
    from .settings_private import *  # noqa: F401,F403
except (ImportError, ModuleNotFoundError):
    pass

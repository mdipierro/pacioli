"""Core fixtures for the ERP app (db, session, auth, token-based API auth).

This is a trimmed version of the _scaffold common.py: cookie sessions, local
auth, and the SimpleTokenPlugin enabled so the REST API can be called with
``Authorization: Bearer <token>``.
"""

import os
import sys

from py4web import DAL, Cache, Field, Session, Translator, action
from py4web.server_adapters.logging_utils import make_logger
from py4web.utils.auth import Auth, SimpleTokenPlugin
from py4web.utils.factories import ActionFactory
from py4web.utils.mailer import Mailer

from . import settings

# make the vendored pacioli_erp package importable from the app.
# (If pacioli_erp is also pip-installed, the installed copy is used instead;
# this insert is what lets the app run with no install at all.)
if settings.VENDORED_PACKAGE_DIR not in sys.path:
    sys.path.insert(0, settings.VENDORED_PACKAGE_DIR)

logger = make_logger("py4web:" + settings.APP_NAME, settings.LOGGERS)

db = DAL(
    settings.DB_URI,
    folder=settings.DB_FOLDER,
    pool_size=settings.DB_POOL_SIZE,
    migrate=settings.DB_MIGRATE,
    fake_migrate=settings.DB_FAKE_MIGRATE,
)

cache = Cache(size=1000)
T = Translator(settings.T_FOLDER)
session = Session(secret=settings.SESSION_SECRET_KEY)

auth = Auth(session, db, define_tables=False, T=T)
auth.use_username = True
auth.param.registration_requires_confirmation = settings.VERIFY_EMAIL
auth.param.registration_requires_approval = settings.REQUIRES_APPROVAL
auth.param.login_after_registration = settings.LOGIN_AFTER_REGISTRATION
auth.param.allowed_actions = settings.ALLOWED_ACTIONS
auth.param.login_expiration_time = 3600
auth.param.password_complexity = {"entropy": settings.PASSWORD_ENTROPY}
auth.param.block_previous_password_num = 3
auth.param.default_login_enabled = settings.DEFAULT_LOGIN_ENABLED
auth.define_tables()
auth.fix_actions()
auth.logger = logger

flash = auth.flash

if settings.SMTP_SERVER:
    auth.sender = Mailer(
        server=settings.SMTP_SERVER,
        sender=settings.SMTP_SENDER,
        login=settings.SMTP_LOGIN,
        tls=settings.SMTP_TLS,
        ssl=settings.SMTP_SSL,
    )

# Bearer-token API auth: create tokens in db.auth_simple_token (see the
# tokens management page in controllers.py).
simple_token_plugin = SimpleTokenPlugin(auth)
auth.token_plugins.append(simple_token_plugin)

auth.enable(uses=(session, T, db), env=dict(T=T))

unauthenticated = ActionFactory(db, session, T, flash, auth)
authenticated = ActionFactory(db, session, T, flash, auth.user)

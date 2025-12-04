# Copyright 2016-2017 Versada <https://versada.eu/>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import threading
import warnings
from collections import abc

import odoo.http
from odoo.service.server import server
from odoo.tools import config as odoo_config

from . import const
from .logutils import (
    InvalidGitRepository,
    SanitizeOdooCookiesProcessor,
    fetch_git_sha,
    get_extra_context,
)

_logger = logging.getLogger(__name__)
HAS_SENTRY_SDK = True
try:
    import sentry_sdk
    from sentry_sdk.integrations.logging import ignore_logger
    from sentry_sdk.integrations.threading import ThreadingIntegration
    from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
except ImportError:  # pragma: no cover
    HAS_SENTRY_SDK = False  # pragma: no cover
    _logger.debug(
        "Cannot import 'sentry-sdk'.\
                        Please make sure it is installed."
    )  # pragma: no cover


def _get_exception_qualified_name(hint):
    """Extract the qualified exception name from the hint.

    Returns the fully qualified name (module.ClassName) or None if not found.
    """
    exc_info = hint.get("exc_info")

    # Case 1: Exception captured with exc_info (e.g., cron jobs)
    if exc_info is not None:
        exc_type = exc_info[0]
        if exc_type is not None:
            return exc_type.__module__ + "." + exc_type.__name__

    # Case 2: Exception captured via log_record (e.g., HTTP requests)
    if "log_record" in hint:
        try:
            msg = hint["log_record"].msg
            return msg.__module__ + "." + msg.__class__.__name__
        except AttributeError:
            pass

    return None


def _should_ignore_exception(qualified_name, event):
    """Determine if an exception should be ignored based on context.

    For cron threads: uses sentry_ignore_cron_exceptions list
    For other contexts: uses sentry_ignore_exceptions list

    Returns True if the exception should be ignored (not sent to Sentry).
    """
    if not qualified_name:
        return False

    current_thread_name = threading.current_thread().name

    if current_thread_name.startswith("odoo.service.cron."):
        # Use cron-specific exception list
        ignore_cron_exceptions_tag = event.get("tags", {}).get(
            "ignore_cron_exceptions", ""
        )
        if ignore_cron_exceptions_tag:
            ignore_cron_exceptions = [
                exc.strip()
                for exc in ignore_cron_exceptions_tag.split(",")
                if exc.strip()
            ]
            if qualified_name in ignore_cron_exceptions:
                return True
    else:
        # Use regular exception list for non-cron execution
        if qualified_name in const.DEFAULT_IGNORED_EXCEPTIONS:
            return True

    return False


def before_send(event, hint):
    """Prevent the capture of any exceptions in
    the DEFAULT_IGNORED_EXCEPTIONS list
        -- or --
    Add context to event if include_context is True
    and sanitize sensitive data"""

    # Check if the exception should be ignored
    qualified_name = _get_exception_qualified_name(hint)
    if _should_ignore_exception(qualified_name, event):
        return None

    if event.setdefault("tags", {})["include_context"]:
        cxtest = get_extra_context(odoo.http.request)
        info_request = ["tags", "user", "extra", "request"]

        for item in info_request:
            info_item = event.setdefault(item, {})
            info_item.update(cxtest.setdefault(item, {}))

    raven_processor = SanitizeOdooCookiesProcessor()
    raven_processor.process(event)

    return event


def get_odoo_commit(odoo_dir):
    """Attempts to get Odoo git commit from :param:`odoo_dir`."""
    if not odoo_dir:
        return
    try:
        return fetch_git_sha(odoo_dir)
    except InvalidGitRepository:
        _logger.debug("Odoo directory: '%s' not a valid git repository", odoo_dir)


def initialize_sentry(config):
    """Setup an instance of :class:`sentry_sdk.Client`.
    :param config: Sentry configuration
    :param client: class used to instantiate the sentry_sdk client.
    """
    enabled = config.get("sentry_enabled", False)
    if not (HAS_SENTRY_SDK and enabled):
        return
    _logger.info("Initializing sentry...")
    if config.get("sentry_odoo_dir") and config.get("sentry_release"):
        _logger.debug(
            "Both sentry_odoo_dir and \
                       sentry_release defined, choosing sentry_release"
        )
    if config.get("sentry_transport"):
        warnings.warn(
            "`sentry_transport` has been deprecated.  "
            "Its not neccesary send it, will use `HttpTranport` by default.",
            DeprecationWarning,
        )
    options = {}
    for option in const.get_sentry_options():
        value = config.get("sentry_%s" % option.key, option.default)
        if isinstance(option.converter, abc.Callable):
            value = option.converter(value)
        options[option.key] = value

    exclude_loggers = const.split_multiple(
        config.get("sentry_exclude_loggers", const.DEFAULT_EXCLUDE_LOGGERS)
    )

    if not options.get("release"):
        options["release"] = config.get(
            "sentry_release", get_odoo_commit(config.get("sentry_odoo_dir"))
        )

    # Change name `ignore_exceptions` (with raven)
    # to `ignore_errors' (sentry_sdk)
    options["ignore_errors"] = options["ignore_exceptions"]
    del options["ignore_exceptions"]

    options["before_send"] = before_send

    options["integrations"] = [
        options["logging_level"],
        ThreadingIntegration(propagate_hub=True),
    ]
    # Remove logging_level, since in sentry_sdk is include in 'integrations'
    del options["logging_level"]

    # Store ignore_cron_exceptions separately since we need it in before_send
    ignore_cron_exceptions = options.get("ignore_cron_exceptions", [])
    del options["ignore_cron_exceptions"]

    client = sentry_sdk.init(**options)

    sentry_sdk.set_tag("include_context", config.get("sentry_include_context", True))
    sentry_sdk.set_tag("ignore_cron_exceptions", ",".join(ignore_cron_exceptions))

    if exclude_loggers:
        for item in exclude_loggers:
            ignore_logger(item)

    # The server app is already registered so patch it here
    if server:
        server.app = SentryWsgiMiddleware(server.app)

    # Patch the wsgi server in case of further registration
    odoo.http.Application = SentryWsgiMiddleware(odoo.http.Application)

    with sentry_sdk.push_scope() as scope:
        scope.set_extra("debug", False)
        sentry_sdk.capture_message("Starting Odoo Server", "info")

    return client


def post_load():
    initialize_sentry(odoo_config)

"""Switch the exit IP when Douban rate limits a bulk import.

Douban limits per IP: after roughly fifty detail calls it starts answering
``HTTP 400`` with ``code 1309 / subject_ip_rate_limit`` (see
``app.providers.douban_movie``). Waiting it out costs two minutes per film and
does not always help, so the importer borrows another address instead: the
local proxy pool (``.tools/douban_proxy_pool.py``) fronts one mixed port, and
``rotate`` moves it to the next exit IP.

The rule this module keeps:

- a film that is rate limited gets its exit IP switched and is asked again,
  at most ``MAX_IP_RETRIES`` times;
- when the pool is down or refuses, nothing is broken: the old behaviour is
  used instead (wait once, then let the batch stop and resume next run);
- when the switches do not help either, the film is skipped and the batch
  carries on, so one bad subject cannot end a whole run. Only a long run of
  such skips stops the batch, because at that point the limit is clearly on
  every exit IP and continuing would just hammer Douban.

Nothing in here raises on its own except the two errors the importers already
understand, so a pool that is not running can never break an import.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings

from app.models import Sources
from app.providers import douban_movie, services

logger = logging.getLogger(__name__)

# The pool lives next to the database, outside the source tree
PROXY_POOL = Path(settings.BASE_DIR).parent / ".tools" / "douban_proxy_pool.py"
VENV_PYTHON = Path(settings.BASE_DIR).parent / ".venv" / "Scripts" / "python.exe"

# How many exit IPs one film may burn before it is given up on
MAX_IP_RETRIES = 3
# How long the new exit IP is given to take effect before the film is asked again
IP_SWITCH_WAIT = 8
# A rotate that hangs must not hang the import
ROTATE_TIMEOUT = 90
# This many films skipped in a row means the limit is on every exit IP: stop the
# batch cleanly (progress is kept) instead of walking the rest of the collection
MAX_CONSECUTIVE_SKIPS = 8

_running = []


class DoubanStillLimitedError(Exception):
    """A film is still rate limited after every exit IP was tried.

    It is deliberately not a ``ProviderAPIError``: the importers catch that one
    as "the network is throttled, stop the batch", while this one means "this
    film is not coming in today", which is a per film failure.
    """


def reset():
    """Forget the skips of an earlier run. Called once per import."""
    _running.clear()


def rotate_ip():
    """Move the pool to the next exit IP. Return whether that worked.

    Never raises: a pool that is not installed, not running, refuses to switch
    or times out all end as ``False``, which the caller turns back into the
    plain "wait and retry" behaviour.
    """
    if not PROXY_POOL.is_file():
        logger.warning(
            "Douban proxy pool not found at %s, keeping the same IP",
            PROXY_POOL,
        )
        return False

    python = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    try:
        result = subprocess.run(  # noqa: S603
            [str(python), str(PROXY_POOL), "rotate"],
            capture_output=True,
            text=True,
            # The pool prints Chinese. Without this, Windows decodes it as GBK
            # and the reader thread dies on the first emoji or box character.
            encoding="utf-8",
            errors="replace",
            timeout=ROTATE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Could not rotate the Douban exit IP: %s", error)
        return False

    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        logger.warning(
            "Douban exit IP not switched (exit %s): %s",
            result.returncode,
            detail[-1] if detail else "no output",
        )
        return False

    detail = (result.stdout or "").strip().splitlines()
    logger.warning("Douban exit IP switched: %s", detail[-1] if detail else "ok")
    return True


def fetch_metadata(media_type, media_id):
    """Return the Douban metadata of one subject, switching IP when limited.

    A rate limit is answered by rotating the exit IP and asking again, up to
    ``MAX_IP_RETRIES`` times. A pool that will not rotate falls back to the old
    behaviour: one long wait, one retry, and then the batch is allowed to stop.

    Raises:
        DoubanStillLimitedError: this subject was limited on every exit IP. The
            importers skip it and carry on with the next one.
        services.ProviderAPIError: the limit is on every exit IP and no pool is
            available to move to, or the call failed for another reason.
    """
    if len(_running) >= MAX_CONSECUTIVE_SKIPS:
        logger.warning(
            "Douban kept rate limiting every exit IP for %s subjects in a row, "
            "stopping this batch so the rest can run later",
            len(_running),
        )
        raise _limit_error()

    switches = 0
    waited_without_pool = False

    while True:
        try:
            metadata = services.get_media_metadata(
                media_type,
                media_id,
                Sources.DOUBAN.value,
            )
        except services.ProviderAPIError as error:
            if not douban_movie.is_rate_limited_error(error):
                raise

            if switches >= MAX_IP_RETRIES:
                _running.append(str(media_id))
                logger.warning(
                    "Douban still rate limited %s after %s new exit IPs, "
                    "skipping it and carrying on",
                    media_id,
                    switches,
                )
                raise DoubanStillLimitedError(media_id) from error

            if not rotate_ip():
                if waited_without_pool:
                    # No pool and one wait did not help: stop like before
                    raise
                waited_without_pool = True
                logger.warning(
                    "No usable exit IP to switch to, waiting %s seconds before "
                    "asking for %s again",
                    douban_movie.RATE_LIMIT_WAIT,
                    media_id,
                )
                time.sleep(douban_movie.RATE_LIMIT_WAIT)
                continue

            switches += 1
            logger.warning(
                "Douban rate limited %s, new exit IP %s/%s, retrying in %s seconds",
                media_id,
                switches,
                MAX_IP_RETRIES,
                IP_SWITCH_WAIT,
            )
            time.sleep(IP_SWITCH_WAIT)
        else:
            _running.clear()
            return metadata


def _limit_error():
    """Return a provider error the importers read as "stop the whole batch"."""
    return services.ProviderAPIError(
        Sources.DOUBAN.value,
        douban_movie.RATE_LIMIT_MESSAGE,
        douban_movie.RATE_LIMIT_MESSAGE,
    )

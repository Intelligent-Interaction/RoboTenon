"""Process limits for parallel workers and model services (inherited by children)."""
import logging

try:
    import resource
except ImportError:  # Windows has no POSIX resource limits.
    resource = None

_LOG = logging.getLogger(__name__)


def ensure_file_descriptor_limit(target=65536):
    """Raise only the soft RLIMIT_NOFILE; never lower or alter the hard limit."""
    if resource is None:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY or soft >= target:
            return soft
        desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
            soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            _LOG.info("File descriptor soft limit raised to %s (hard=%s)", soft, hard)
        if soft < target:
            _LOG.warning("File descriptor limit is %s, below parallel runtime target %s; "
                         "system hard limit is %s", soft, target, hard)
        return soft
    except (OSError, ValueError) as error:
        _LOG.warning("Unable to raise file descriptor limit to %s: %s", target, error)
        return None

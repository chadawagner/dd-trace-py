import abc
import enum
import typing  # noqa:F401

import attr
import six

from . import forksafe


class ServiceStatus(enum.Enum):
    """A Service status."""

    STOPPED = "stopped"
    RUNNING = "running"


class ServiceStatusError(RuntimeError):
    def __init__(
        self,
        service_cls,  # type: typing.Type[Service]
        current_status,  # type: ServiceStatus
    ):
        # type: (...) -> None
        self.current_status = current_status
        super(ServiceStatusError, self).__init__(
            "%s is already in status %s" % (service_cls.__name__, current_status.value)
        )


@attr.s(eq=False)
class Service(six.with_metaclass(abc.ABCMeta)):
    """A service that can be started or stopped."""

    status = attr.ib(default=ServiceStatus.STOPPED, type=ServiceStatus, init=False, eq=False)
    _service_lock = attr.ib(factory=forksafe.Lock, repr=False, init=False, eq=False)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        self.join()

    def start(
        self,
        *args,  # type: typing.Any
        **kwargs,  # type: typing.Any
    ):
        # type: (...) -> None
        """Start the service."""
        # Use a lock so we're sure that if 2 threads try to start the service at the same time, one of them will raise
        # an error.
        with self._service_lock:
            if self.status == ServiceStatus.RUNNING:
                raise ServiceStatusError(self.__class__, self.status)
            self._start_service(*args, **kwargs)
            self.status = ServiceStatus.RUNNING

    @abc.abstractmethod
    def _start_service(
        self,
        *args,  # type: typing.Any
        **kwargs,  # type: typing.Any
    ):
        # type: (...) -> None
        """Start the service for real.

        This method uses the internal lock to be sure there's no race conditions and that the service is really started
        once start() returns.

        """

    def stop(
        self,
        *args,  # type: typing.Any
        **kwargs,  # type: typing.Any
    ):
        # type: (...) -> None
        """Stop the service."""
        with self._service_lock:
            if self.status == ServiceStatus.STOPPED:
                raise ServiceStatusError(self.__class__, self.status)
            self._stop_service(*args, **kwargs)
            self.status = ServiceStatus.STOPPED

    @abc.abstractmethod
    def _stop_service(
        self,
        *args,  # type: typing.Any
        **kwargs,  # type: typing.Any
    ):
        # type: (...) -> None
        """Stop the service for real.

        This method uses the internal lock to be sure there's no race conditions and that the service is really stopped
        once start() returns.

        """

    def join(
        self, timeout=None  # type: typing.Optional[float]
    ):
        # type: (...) -> None
        """Join the service once stopped."""

import abc
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401

import attr
import six

from ddtrace import Span  # noqa:F401
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


@attr.s
class SpanProcessor(six.with_metaclass(abc.ABCMeta)):
    """A Processor is used to process spans as they are created and finished by a tracer."""

    __processors__ = []  # type: List["SpanProcessor"]

    def __attrs_post_init__(self):
        # type: () -> None
        """Default post initializer which logs the representation of the
        Processor at the ``logging.DEBUG`` level.

        The representation can be modified with the ``repr`` argument to the
        attrs attribute::

            @attr.s
            class MyProcessor(Processor):
                field_to_include = attr.ib(repr=True)
                field_to_exclude = attr.ib(repr=False)
        """
        log.debug("initialized processor %r", self)

    @abc.abstractmethod
    def on_span_start(self, span):
        # type: (Span) -> None
        """Called when a span is started.

        This method is useful for making upfront decisions on spans.

        For example, a sampling decision can be made when the span is created
        based on its resource name.
        """
        pass

    @abc.abstractmethod
    def on_span_finish(self, span):
        # type: (Span) -> None
        """Called with the result of any previous processors or initially with
        the finishing span when a span finishes.

        It can return any data which will be passed to any processors that are
        applied afterwards.
        """
        pass

    def shutdown(self, timeout):
        # type: (Optional[float]) -> None
        """Called when the processor is done being used.

        Any clean-up or flushing should be performed with this method.
        """
        pass

    def register(self):
        # type: () -> None
        """Register the processor with the global list of processors."""
        SpanProcessor.__processors__.append(self)

    def unregister(self):
        # type: () -> None
        """Unregister the processor from the global list of processors."""
        try:
            SpanProcessor.__processors__.remove(self)
        except ValueError:
            raise ValueError("Span processor %r not registered" % self)

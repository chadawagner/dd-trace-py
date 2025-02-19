from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401

import starlette
from starlette.middleware import Middleware

from ddtrace import config
from ddtrace.contrib.asgi.middleware import TraceMiddleware
from ddtrace.ext import http
from ddtrace.internal.constants import HTTP_REQUEST_BLOCKED
from ddtrace.internal.logger import get_logger
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils.wrappers import unwrap as _u
from ddtrace.span import Span  # noqa:F401
from ddtrace.vendor.wrapt import ObjectProxy
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

from ...internal import core
from .. import trace_utils


log = get_logger(__name__)

config._add(
    "starlette",
    dict(
        _default_service=schematize_service_name("starlette"),
        request_span_name="starlette.request",
        distributed_tracing=True,
    ),
)


def get_version():
    # type: () -> str
    return getattr(starlette, "__version__", "")


def traced_init(wrapped, instance, args, kwargs):
    mw = kwargs.pop("middleware", [])
    mw.insert(0, Middleware(TraceMiddleware, integration_config=config.starlette))
    kwargs.update({"middleware": mw})

    wrapped(*args, **kwargs)


def patch():
    if getattr(starlette, "_datadog_patch", False):
        return

    starlette._datadog_patch = True

    _w("starlette.applications", "Starlette.__init__", traced_init)

    # We need to check that Fastapi instrumentation hasn't already patched these
    if not isinstance(starlette.routing.Route.handle, ObjectProxy):
        _w("starlette.routing", "Route.handle", traced_handler)
    if not isinstance(starlette.routing.Mount.handle, ObjectProxy):
        _w("starlette.routing", "Mount.handle", traced_handler)


def unpatch():
    if not getattr(starlette, "_datadog_patch", False):
        return

    starlette._datadog_patch = False

    _u(starlette.applications.Starlette, "__init__")

    # We need to check that Fastapi instrumentation hasn't already unpatched these
    if isinstance(starlette.routing.Route.handle, ObjectProxy):
        _u(starlette.routing.Route, "handle")

    if isinstance(starlette.routing.Mount.handle, ObjectProxy):
        _u(starlette.routing.Mount, "handle")


def traced_handler(wrapped, instance, args, kwargs):
    # Since handle can be called multiple times for one request, we take the path of each instance
    # Then combine them at the end to get the correct resource names
    scope = get_argument_value(args, kwargs, 0, "scope")  # type: Optional[Dict[str, Any]]
    if not scope:
        return wrapped(*args, **kwargs)

    # Our ASGI TraceMiddleware has not been called, skip since
    # we won't have a request span to attach this information onto
    # DEV: This can happen if patching happens after the app has been created
    if "datadog" not in scope:
        log.warning("datadog context not present in ASGI request scope, trace middleware may be missing")
        return wrapped(*args, **kwargs)

    # Add the path to the resource_paths list
    if "resource_paths" not in scope["datadog"]:
        scope["datadog"]["resource_paths"] = [instance.path]
    else:
        scope["datadog"]["resource_paths"].append(instance.path)

    request_spans = scope["datadog"].get("request_spans", [])  # type: List[Span]
    resource_paths = scope["datadog"].get("resource_paths", [])  # type: List[str]

    if len(request_spans) == len(resource_paths):
        # Iterate through the request_spans and assign the correct resource name to each
        for index, span in enumerate(request_spans):
            # We want to set the full resource name on the first request span
            # And one part less of the full resource name for each proceeding request span
            # e.g. full path is /subapp/hello/{name}, first request span gets that as resource name
            # Second request span gets /hello/{name}
            path = "".join(resource_paths[index:])

            if scope.get("method"):
                span.resource = "{} {}".format(scope["method"], path)
            else:
                span.resource = path
            # route should only be in the root span
            if index == 0:
                span.set_tag_str(http.ROUTE, path)
    # at least always update the root asgi span resource name request_spans[0].resource = "".join(resource_paths)
    elif request_spans and resource_paths:
        route = "".join(resource_paths)
        if scope.get("method"):
            request_spans[0].resource = "{} {}".format(scope["method"], route)
        else:
            request_spans[0].resource = route
        request_spans[0].set_tag_str(http.ROUTE, route)
    else:
        log.debug(
            "unable to update the request span resource name, request_spans:%r, resource_paths:%r",
            request_spans,
            resource_paths,
        )
    if request_spans:
        trace_utils.set_http_meta(request_spans[0], "starlette", request_path_params=scope.get("path_params"))
    core.dispatch("asgi.start_request", "starlette")
    if core.get_item(HTTP_REQUEST_BLOCKED):
        raise trace_utils.InterruptException("starlette")

    return wrapped(*args, **kwargs)

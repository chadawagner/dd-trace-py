import asyncio
import os
import sys

import aioredis

from ddtrace import config
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.utils.wrappers import unwrap as _u
from ddtrace.pin import Pin
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import SPAN_KIND
from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanKind
from ...ext import SpanTypes
from ...ext import db
from ...ext import net
from ...ext import redis as redisx
from ...internal.schema import schematize_cache_operation
from ...internal.schema import schematize_service_name
from ...internal.utils.formats import CMD_MAX_LEN
from ...internal.utils.formats import asbool
from ...internal.utils.formats import stringify_cache_args
from .. import trace_utils
from ..redis.asyncio_patch import _run_redis_command_async
from ..trace_utils_redis import ROW_RETURNING_COMMANDS
from ..trace_utils_redis import _trace_redis_cmd
from ..trace_utils_redis import _trace_redis_execute_pipeline
from ..trace_utils_redis import determine_row_count


try:
    from aioredis.commands.transaction import _RedisBuffer
except ImportError:
    _RedisBuffer = None

config._add(
    "aioredis",
    dict(
        _default_service=schematize_service_name("redis"),
        cmd_max_length=int(os.getenv("DD_AIOREDIS_CMD_MAX_LENGTH", CMD_MAX_LEN)),
        resource_only_command=asbool(os.getenv("DD_REDIS_RESOURCE_ONLY_COMMAND", True)),
    ),
)

aioredis_version_str = getattr(aioredis, "__version__", "")
aioredis_version = tuple([int(i) for i in aioredis_version_str.split(".")])


def get_version():
    # type: () -> str
    return aioredis_version_str


def patch():
    if getattr(aioredis, "_datadog_patch", False):
        return
    aioredis._datadog_patch = True
    pin = Pin()
    if aioredis_version >= (2, 0):
        _w("aioredis.client", "Redis.execute_command", traced_execute_command)
        _w("aioredis.client", "Redis.pipeline", traced_pipeline)
        _w("aioredis.client", "Pipeline.execute", traced_execute_pipeline)
        pin.onto(aioredis.client.Redis)
    else:
        _w("aioredis", "Redis.execute", traced_13_execute_command)
        _w("aioredis", "Redis.pipeline", traced_13_pipeline)
        _w("aioredis.commands.transaction", "Pipeline.execute", traced_13_execute_pipeline)
        pin.onto(aioredis.Redis)


def unpatch():
    if not getattr(aioredis, "_datadog_patch", False):
        return

    aioredis._datadog_patch = False
    if aioredis_version >= (2, 0):
        _u(aioredis.client.Redis, "execute_command")
        _u(aioredis.client.Redis, "pipeline")
        _u(aioredis.client.Pipeline, "execute")
    else:
        _u(aioredis.Redis, "execute")
        _u(aioredis.Redis, "pipeline")
        _u(aioredis.commands.transaction.Pipeline, "execute")


async def traced_execute_command(func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return await func(*args, **kwargs)

    with _trace_redis_cmd(pin, config.aioredis, instance, args) as span:
        return await _run_redis_command_async(span=span, func=func, args=args, kwargs=kwargs)


def traced_pipeline(func, instance, args, kwargs):
    pipeline = func(*args, **kwargs)
    pin = Pin.get_from(instance)
    if pin:
        pin.onto(pipeline)
    return pipeline


async def traced_execute_pipeline(func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return await func(*args, **kwargs)

    cmds = [stringify_cache_args(c, cmd_max_len=config.aioredis.cmd_max_length) for c, _ in instance.command_stack]
    with _trace_redis_execute_pipeline(pin, config.aioredis, cmds, instance):
        return await func(*args, **kwargs)


def traced_13_pipeline(func, instance, args, kwargs):
    pipeline = func(*args, **kwargs)
    pin = Pin.get_from(instance)
    if pin:
        pin.onto(pipeline)
    return pipeline


def traced_13_execute_command(func, instance, args, kwargs):
    # If we have a _RedisBuffer then we are in a pipeline
    if isinstance(instance.connection, _RedisBuffer):
        return func(*args, **kwargs)

    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    # Don't activate the span since this operation is performed as a future which concludes sometime later on in
    # execution so subsequent operations in the stack are not necessarily semantically related
    # (we don't want this span to be the parent of all other spans created before the future is resolved)
    parent = pin.tracer.current_span()
    query = stringify_cache_args(args, cmd_max_len=config.aioredis.cmd_max_length)
    span = pin.tracer.start_span(
        schematize_cache_operation(redisx.CMD, cache_provider="redis"),
        service=trace_utils.ext_service(pin, config.aioredis),
        resource=query.split(" ")[0] if config.aioredis.resource_only_command else query,
        span_type=SpanTypes.REDIS,
        activate=False,
        child_of=parent,
    )
    # set span.kind to the type of request being performed
    span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

    span.set_tag_str(COMPONENT, config.aioredis.integration_name)
    span.set_tag_str(db.SYSTEM, redisx.APP)
    span.set_tag(SPAN_MEASURED_KEY)
    span.set_tag_str(redisx.RAWCMD, query)
    if pin.tags:
        span.set_tags(pin.tags)

    span.set_tags(
        {
            net.TARGET_HOST: instance.address[0],
            net.TARGET_PORT: instance.address[1],
            redisx.DB: instance.db or 0,
        }
    )
    span.set_metric(redisx.ARGS_LEN, len(args))
    # set analytics sample rate if enabled
    span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, config.aioredis.get_analytics_sample_rate())

    def _finish_span(future):
        try:
            # Accessing the result will raise an exception if:
            #   - The future was cancelled (CancelledError)
            #   - There was an error executing the future (`future.exception()`)
            #   - The future is in an invalid state
            redis_command = span.resource.split(" ")[0]
            future.result()
            if redis_command in ROW_RETURNING_COMMANDS:
                determine_row_count(redis_command=redis_command, span=span, result=future.result())
        # CancelledError exceptions extend from BaseException as of Python 3.8, instead of usual Exception
        except BaseException:
            span.set_exc_info(*sys.exc_info())
            if redis_command in ROW_RETURNING_COMMANDS:
                span.set_metric(db.ROWCOUNT, 0)
        finally:
            span.finish()

    task = func(*args, **kwargs)
    # Execute command returns a coroutine when no free connections are available
    # https://github.com/aio-libs/aioredis-py/blob/v1.3.1/aioredis/pool.py#L191
    task = asyncio.ensure_future(task)
    task.add_done_callback(_finish_span)
    return task


async def traced_13_execute_pipeline(func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return await func(*args, **kwargs)

    cmds = []
    for _, cmd, cmd_args, _ in instance._pipeline:
        parts = [cmd]
        parts.extend(cmd_args)
        cmds.append(stringify_cache_args(parts, cmd_max_len=config.aioredis.cmd_max_length))

    resource = cmds_string = "\n".join(cmds)
    if config.aioredis.resource_only_command:
        resource = "\n".join([cmd.split(" ")[0] for cmd in cmds])

    with pin.tracer.trace(
        schematize_cache_operation(redisx.CMD, cache_provider="redis"),
        resource=resource,
        service=trace_utils.ext_service(pin, config.aioredis),
        span_type=SpanTypes.REDIS,
    ) as span:
        # set span.kind to the type of request being performed
        span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

        span.set_tag_str(COMPONENT, config.aioredis.integration_name)
        span.set_tag_str(db.SYSTEM, redisx.APP)
        span.set_tags(
            {
                net.TARGET_HOST: instance._pool_or_conn.address[0],
                net.TARGET_PORT: instance._pool_or_conn.address[1],
                redisx.DB: instance._pool_or_conn.db or 0,
            }
        )

        span.set_tag(SPAN_MEASURED_KEY)
        span.set_tag_str(redisx.RAWCMD, cmds_string)
        span.set_metric(redisx.PIPELINE_LEN, len(instance._pipeline))
        # set analytics sample rate if enabled
        span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, config.aioredis.get_analytics_sample_rate())

        return await func(*args, **kwargs)

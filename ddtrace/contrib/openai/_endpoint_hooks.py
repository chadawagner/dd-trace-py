from .utils import _compute_prompt_token_count
from .utils import _format_openai_api_key
from .utils import _is_async_generator
from .utils import _is_generator
from .utils import _tag_tool_calls


API_VERSION = "v1"


class _EndpointHook:
    """
    Base class for all OpenAI endpoint hooks.
    Each new endpoint hook should declare `_request_arg_params` and `_request_kwarg_params`,
    which will be tagged automatically by _EndpointHook._record_request().
    For endpoint-specific request/response parameters that requires special casing, add that logic to
    the endpoint hook's `_record_request()` after a super call to the base `_EndpointHook._record_request()`.
    """

    # _request_arg_params must include the names of arg parameters in order.
    # If a given arg requires special casing, replace with `None` to avoid automatic tagging.
    _request_arg_params = ()
    # _request_kwarg_params must include the names of kwarg parameters to tag automatically.
    # If a given kwarg requires special casing, remove from this tuple to avoid automatic tagging.
    _request_kwarg_params = ()
    # _response_attrs is used to automatically tag specific response attributes.
    _response_attrs = ()
    _base_level_tag_args = ("api_base", "api_type", "api_version")
    ENDPOINT_NAME = "openai"
    HTTP_METHOD_TYPE = ""
    OPERATION_ID = ""  # Each endpoint hook must provide an operationID as specified in the OpenAI API specs:
    # https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml

    def _record_request(self, pin, integration, span, args, kwargs):
        """
        Set base-level openai tags, as well as request params from args and kwargs.
        All inherited EndpointHook classes should include a super call to this method before performing
        endpoint-specific request tagging logic.
        """
        endpoint = self.ENDPOINT_NAME
        if endpoint is None:
            endpoint = "%s" % args[0].OBJECT_NAME
        span.set_tag_str("openai.request.endpoint", "/%s/%s" % (API_VERSION, endpoint))
        span.set_tag_str("openai.request.method", self.HTTP_METHOD_TYPE)

        if self._request_arg_params and len(self._request_arg_params) > 1:
            for idx, arg in enumerate(self._request_arg_params, 1):
                if idx >= len(args):
                    break
                if arg is None or args[idx] is None:
                    continue
                if arg in self._base_level_tag_args:
                    span.set_tag_str("openai.%s" % arg, str(args[idx]))
                elif arg == "organization":
                    span.set_tag_str("openai.organization.id", args[idx])
                elif arg == "api_key":
                    span.set_tag_str("openai.user.api_key", _format_openai_api_key(args[idx]))
                else:
                    span.set_tag_str("openai.request.%s" % arg, str(args[idx]))
        for kw_attr in self._request_kwarg_params:
            if kw_attr not in kwargs:
                continue
            if isinstance(kwargs[kw_attr], dict):
                for k, v in kwargs[kw_attr].items():
                    span.set_tag_str("openai.request.%s.%s" % (kw_attr, k), str(v))
            elif kw_attr == "engine":  # Azure OpenAI requires using "engine" instead of "model"
                span.set_tag_str("openai.request.model", str(kwargs[kw_attr]))
            else:
                span.set_tag_str("openai.request.%s" % kw_attr, str(kwargs[kw_attr]))

    def handle_request(self, pin, integration, span, args, kwargs):
        self._record_request(pin, integration, span, args, kwargs)
        resp, error = yield
        return self._record_response(pin, integration, span, args, kwargs, resp, error)

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        for resp_attr in self._response_attrs:
            if hasattr(resp, resp_attr):
                span.set_tag_str("openai.response.%s" % resp_attr, str(getattr(resp, resp_attr, "")))
        return resp


class _BaseCompletionHook(_EndpointHook):
    """
    Share streamed response handling logic between Completion and ChatCompletion endpoints.
    """

    _request_arg_params = ("api_key", "api_base", "api_type", "request_id", "api_version", "organization")

    def _handle_streamed_response(self, integration, span, args, kwargs, resp):
        """Handle streamed response objects returned from endpoint calls.

        This method helps with streamed responses by wrapping the generator returned with a
        generator that traces the reading of the response.
        """

        def shared_gen():
            try:
                num_prompt_tokens = span.get_metric("openai.response.usage.prompt_tokens") or 0
                num_completion_tokens = yield
                span.set_metric("openai.response.usage.completion_tokens", num_completion_tokens)
                total_tokens = num_prompt_tokens + num_completion_tokens
                span.set_metric("openai.response.usage.total_tokens", total_tokens)
                if span.get_metric("openai.request.prompt_tokens_estimated") == 0:
                    integration.metric(span, "dist", "tokens.prompt", num_prompt_tokens)
                else:
                    integration.metric(span, "dist", "tokens.prompt", num_prompt_tokens, tags=["openai.estimated:true"])
                integration.metric(
                    span, "dist", "tokens.completion", num_completion_tokens, tags=["openai.estimated:true"]
                )
                integration.metric(span, "dist", "tokens.total", total_tokens, tags=["openai.estimated:true"])
            finally:
                span.finish()
                integration.metric(span, "dist", "request.duration", span.duration_ns)

        num_prompt_tokens = 0
        estimated = False
        prompt = kwargs.get("prompt", None)
        messages = kwargs.get("messages", None)
        if prompt is not None:
            if isinstance(prompt, str) or isinstance(prompt, list) and isinstance(prompt[0], int):
                prompt = [prompt]
            for p in prompt:
                estimated, prompt_tokens = _compute_prompt_token_count(p, kwargs.get("model"))
                num_prompt_tokens += prompt_tokens
        if messages is not None:
            for m in messages:
                estimated, prompt_tokens = _compute_prompt_token_count(m.get("content", ""), kwargs.get("model"))
                num_prompt_tokens += prompt_tokens
        span.set_metric("openai.request.prompt_tokens_estimated", int(estimated))
        span.set_metric("openai.response.usage.prompt_tokens", num_prompt_tokens)

        # A chunk corresponds to a token:
        #  https://community.openai.com/t/how-to-get-total-tokens-from-a-stream-of-completioncreaterequests/110700
        #  https://community.openai.com/t/openai-api-get-usage-tokens-in-response-when-set-stream-true/141866
        if _is_async_generator(resp):

            async def traced_streamed_response():
                g = shared_gen()
                g.send(None)
                num_completion_tokens = 0
                try:
                    async for chunk in resp:
                        num_completion_tokens += 1
                        yield chunk
                finally:
                    try:
                        g.send(num_completion_tokens)
                    except StopIteration:
                        pass

            return traced_streamed_response()

        elif _is_generator(resp):

            def traced_streamed_response():
                g = shared_gen()
                g.send(None)
                num_completion_tokens = 0
                try:
                    for chunk in resp:
                        num_completion_tokens += 1
                        yield chunk
                finally:
                    try:
                        g.send(num_completion_tokens)
                    except StopIteration:
                        pass

            return traced_streamed_response()

        return resp


class _CompletionHook(_BaseCompletionHook):
    _request_kwarg_params = (
        "model",
        "engine",
        "suffix",
        "max_tokens",
        "temperature",
        "top_p",
        "n",
        "stream",
        "logprobs",
        "echo",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "best_of",
        "logit_bias",
        "user",
    )
    _response_attrs = ("created", "id", "model")
    ENDPOINT_NAME = "completions"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createCompletion"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if integration.is_pc_sampled_span(span):
            prompt = kwargs.get("prompt", "")
            if isinstance(prompt, str):
                prompt = [prompt]
            for idx, p in enumerate(prompt):
                span.set_tag_str("openai.request.prompt.%d" % idx, integration.trunc(str(p)))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        if kwargs.get("stream"):
            return self._handle_streamed_response(integration, span, args, kwargs, resp)
        for choice in resp.choices:
            span.set_tag_str("openai.response.choices.%d.finish_reason" % choice.index, str(choice.finish_reason))
            if integration.is_pc_sampled_span(span):
                span.set_tag_str("openai.response.choices.%d.text" % choice.index, integration.trunc(choice.text))
        integration.record_usage(span, resp.usage)
        if integration.is_pc_sampled_log(span):
            log_choices = resp.choices
            if hasattr(resp.choices[0], "model_dump"):
                log_choices = [choice.model_dump() for choice in resp.choices]
            attrs_dict = {"prompt": kwargs.get("prompt", ""), "choices": log_choices}
            integration.log(
                span, "info" if error is None else "error", "sampled %s" % self.OPERATION_ID, attrs=attrs_dict
            )
        return resp


class _ChatCompletionHook(_BaseCompletionHook):
    _request_kwarg_params = (
        "model",
        "engine",
        "temperature",
        "top_p",
        "n",
        "stream",
        "stop",
        "max_tokens",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "user",
    )
    _response_attrs = ("created", "id", "model")
    ENDPOINT_NAME = "chat/completions"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createChatCompletion"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        for idx, m in enumerate(kwargs.get("messages", [])):
            if integration.is_pc_sampled_span(span):
                span.set_tag_str("openai.request.messages.%d.content" % idx, integration.trunc(m.get("content", "")))
            span.set_tag_str("openai.request.messages.%d.role" % idx, m.get("role", ""))
            span.set_tag_str("openai.request.messages.%d.name" % idx, m.get("name", ""))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        if kwargs.get("stream"):
            return self._handle_streamed_response(integration, span, args, kwargs, resp)
        for choice in resp.choices:
            idx = choice.index
            message = choice.message
            span.set_tag_str("openai.response.choices.%d.finish_reason" % idx, str(choice.finish_reason))
            span.set_tag_str("openai.response.choices.%d.message.role" % idx, choice.message.role)
            if integration.is_pc_sampled_span(span):
                span.set_tag_str(
                    "openai.response.choices.%d.message.content" % idx, integration.trunc(message.content or "")
                )
            if getattr(message, "function_call", None):
                _tag_tool_calls(integration, span, [message.function_call], idx)
            if getattr(message, "tool_calls", None):
                _tag_tool_calls(integration, span, message.tool_calls, idx)
        integration.record_usage(span, resp.usage)
        if integration.is_pc_sampled_log(span):
            log_choices = resp.choices
            if hasattr(resp.choices[0], "model_dump"):
                log_choices = [choice.model_dump() for choice in resp.choices]
            attrs_dict = {"messages": kwargs.get("messages", []), "completion": log_choices}
            integration.log(
                span, "info" if error is None else "error", "sampled %s" % self.OPERATION_ID, attrs=attrs_dict
            )
        return resp


class _EmbeddingHook(_EndpointHook):
    _request_arg_params = ("api_key", "api_base", "api_type", "request_id", "api_version", "organization")
    _request_kwarg_params = ("model", "engine", "user")
    _response_attrs = ("model",)
    ENDPOINT_NAME = "embeddings"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createEmbedding"

    def _record_request(self, pin, integration, span, args, kwargs):
        """
        Embedding endpoint allows multiple inputs, each of which we specify a request tag for, so have to
        manually set them in _pre_response().
        """
        super()._record_request(pin, integration, span, args, kwargs)
        embedding_input = kwargs.get("input", "")
        if integration.is_pc_sampled_span(span):
            if isinstance(embedding_input, str) or isinstance(embedding_input[0], int):
                embedding_input = [embedding_input]
            for idx, inp in enumerate(embedding_input):
                span.set_tag_str("openai.request.input.%d" % idx, integration.trunc(str(inp)))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        span.set_metric("openai.response.embeddings_count", len(resp.data))
        span.set_metric("openai.response.embedding-length", len(resp.data[0].embedding))
        integration.record_usage(span, resp.usage)
        return resp


class _ListHook(_EndpointHook):
    """
    Hook for openai.ListableAPIResource, which is used by Model.list, File.list, and FineTune.list.
    """

    _request_arg_params = ("api_key", "request_id", "api_version", "organization", "api_base", "api_type")
    _request_kwarg_params = ("user",)
    ENDPOINT_NAME = None
    HTTP_METHOD_TYPE = "GET"
    OPERATION_ID = "list"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        endpoint = span.get_tag("openai.request.endpoint")
        if endpoint.endswith("/models"):
            span.resource = "listModels"
        elif endpoint.endswith("/files"):
            span.resource = "listFiles"
        elif endpoint.endswith("/fine-tunes"):
            span.resource = "listFineTunes"

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        span.set_metric("openai.response.count", len(resp.data or []))
        return resp


class _ModelListHook(_ListHook):
    """
    Hook for openai.resources.models.Models.list (v1)
    """

    ENDPOINT_NAME = "models"
    OPERATION_ID = "listModels"


class _FileListHook(_ListHook):
    """
    Hook for openai.resources.files.Files.list (v1)
    """

    ENDPOINT_NAME = "files"
    OPERATION_ID = "listFiles"


class _FineTuneListHook(_ListHook):
    """
    Hook for openai.resources.fine_tunes.FineTunes.list (v1)
    """

    ENDPOINT_NAME = "fine-tunes"
    OPERATION_ID = "listFineTunes"


class _RetrieveHook(_EndpointHook):
    """Hook for openai.APIResource, which is used by Model.retrieve, File.retrieve, and FineTune.retrieve."""

    _request_arg_params = (None, "api_key", "request_id", "request_timeout")
    _request_kwarg_params = ("user",)
    _response_attrs = (
        "id",
        "owned_by",
        "model",
        "parent",
        "root",
        "bytes",
        "created",
        "created_at",
        "purpose",
        "filename",
        "fine_tuned_model",
        "status",
        "status_details",
        "updated_at",
    )
    ENDPOINT_NAME = None
    HTTP_METHOD_TYPE = "GET"
    OPERATION_ID = "retrieve"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        endpoint = span.get_tag("openai.request.endpoint")
        if endpoint.endswith("/models"):
            span.resource = "retrieveModel"
            span.set_tag_str("openai.request.model", args[1] if len(args) >= 2 else kwargs.get("model", ""))
        elif endpoint.endswith("/files"):
            span.resource = "retrieveFile"
            span.set_tag_str("openai.request.file_id", args[1] if len(args) >= 2 else kwargs.get("file_id", ""))
        elif endpoint.endswith("/fine-tunes"):
            span.resource = "retrieveFineTune"
            span.set_tag_str(
                "openai.request.fine_tune_id", args[1] if len(args) >= 2 else kwargs.get("fine_tune_id", "")
            )
        span.set_tag_str("openai.request.endpoint", "%s/*" % endpoint)

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        if hasattr(resp, "hyperparams"):
            for hyperparam in ("batch_size", "learning_rate_multiplier", "n_epochs", "prompt_loss_weight"):
                val = getattr(resp.hyperparams, hyperparam, "")
                span.set_tag_str("openai.response.hyperparams.%s" % hyperparam, str(val))
        for resp_attr in ("result_files", "training_files", "validation_files"):
            if hasattr(resp, resp_attr):
                span.set_metric("openai.response.%s_count" % resp_attr, len(getattr(resp, resp_attr, [])))
        if hasattr(resp, "events"):
            span.set_metric("openai.response.events_count", len(resp.events))
        return resp


class _ModelRetrieveHook(_RetrieveHook):
    """
    Hook for openai.resources.models.Models.retrieve
    """

    ENDPOINT_NAME = "models"
    OPERATION_ID = "retrieveModel"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.model", args[1] if len(args) >= 2 else kwargs.get("model", ""))


class _FileRetrieveHook(_RetrieveHook):
    """
    Hook for openai.resources.files.Files.retrieve
    """

    ENDPOINT_NAME = "files"
    OPERATION_ID = "retrieveFile"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.file_id", args[1] if len(args) >= 2 else kwargs.get("file_id", ""))


class _FineTuneRetrieveHook(_RetrieveHook):
    """
    Hook for openai.resources.fine_tunes.FineTunes.retrieve
    """

    ENDPOINT_NAME = "fine-tunes"
    OPERATION_ID = "retrieveFineTune"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.fine_tune_id", args[1] if len(args) >= 2 else kwargs.get("fine_tune_id", ""))


class _DeleteHook(_EndpointHook):
    """Hook for openai.DeletableAPIResource, which is used by File.delete, and Model.delete."""

    _request_arg_params = (None, "api_type", "api_version")
    _request_kwarg_params = ("user",)
    ENDPOINT_NAME = None
    HTTP_METHOD_TYPE = "DELETE"
    OPERATION_ID = "delete"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        endpoint = span.get_tag("openai.request.endpoint")
        if endpoint.endswith("/models"):
            span.resource = "deleteModel"
            span.set_tag_str("openai.request.model", args[1] if len(args) >= 2 else kwargs.get("model", ""))
        elif endpoint.endswith("/files"):
            span.resource = "deleteFile"
            span.set_tag_str("openai.request.file_id", args[1] if len(args) >= 2 else kwargs.get("file_id", ""))
        span.set_tag_str("openai.request.endpoint", "%s/*" % endpoint)

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        if hasattr(resp, "data"):
            if resp._headers.get("openai-organization"):
                span.set_tag_str("openai.organization.name", resp._headers.get("openai-organization"))
            span.set_tag_str("openai.response.id", resp.data.get("id", ""))
            span.set_tag_str("openai.response.deleted", str(resp.data.get("deleted", "")))
        else:
            span.set_tag_str("openai.response.id", str(resp.id))
            span.set_tag_str("openai.response.deleted", str(resp.deleted))
        return resp


class _FileDeleteHook(_DeleteHook):
    """
    Hook for openai.resources.files.Files.delete
    """

    ENDPOINT_NAME = "files"


class _ModelDeleteHook(_DeleteHook):
    """
    Hook for openai.resources.models.Models.delete
    """

    ENDPOINT_NAME = "models"


class _EditHook(_EndpointHook):
    _request_arg_params = ("api_key", "api_base", "api_type", "request_id", "api_version", "organization")
    _request_kwarg_params = ("model", "n", "temperature", "top_p", "user")
    _response_attrs = ("created",)
    ENDPOINT_NAME = "edits"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createEdit"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if integration.is_pc_sampled_span(span):
            instruction = kwargs.get("instruction")
            input_text = kwargs.get("input", "")
            span.set_tag_str("openai.request.instruction", integration.trunc(instruction))
            span.set_tag_str("openai.request.input", integration.trunc(input_text))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        choices = resp.choices
        if integration.is_pc_sampled_span(span):
            for choice in choices:
                idx = choice.index
                span.set_tag_str("openai.response.choices.%d.text" % idx, integration.trunc(str(choice.text)))
        integration.record_usage(span, resp.usage)
        if integration.is_pc_sampled_log(span):
            log_choices = resp.choices
            if hasattr(resp.choices[0], "model_dump"):
                log_choices = [choice.model_dump() for choice in resp.choices]
            integration.log(
                span,
                "info" if error is None else "error",
                "sampled %s" % self.OPERATION_ID,
                attrs={
                    "instruction": kwargs.get("instruction"),
                    "input": kwargs.get("input", ""),
                    "choices": log_choices,
                },
            )
        return resp


class _ImageHook(_EndpointHook):
    _response_attrs = ("created",)
    ENDPOINT_NAME = "images"
    HTTP_METHOD_TYPE = "POST"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.model", "dall-e")

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        choices = resp.data
        span.set_metric("openai.response.images_count", len(choices))
        if integration.is_pc_sampled_span(span):
            for idx, choice in enumerate(choices):
                if getattr(choice, "b64_json", None) is not None:
                    span.set_tag_str("openai.response.images.%d.b64_json" % idx, "returned")
                else:
                    span.set_tag_str("openai.response.images.%d.url" % idx, integration.trunc(choice.url))
        if integration.is_pc_sampled_log(span):
            attrs_dict = {}
            if kwargs.get("response_format", "") == "b64_json":
                attrs_dict.update({"choices": [{"b64_json": "returned"} for _ in resp.data]})
            else:
                log_choices = resp.data
                if hasattr(resp.data[0], "model_dump"):
                    log_choices = [choice.model_dump() for choice in resp.data]
                attrs_dict.update({"choices": log_choices})
            if "prompt" in self._request_kwarg_params:
                attrs_dict.update({"prompt": kwargs.get("prompt", "")})
            if "image" in self._request_kwarg_params:
                image = args[1] if len(args) >= 2 else kwargs.get("image", "")
                attrs_dict.update({"image": image.name.split("/")[-1]})
            if "mask" in self._request_kwarg_params:
                mask = args[2] if len(args) >= 3 else kwargs.get("mask", "")
                attrs_dict.update({"mask": mask.name.split("/")[-1]})
            integration.log(
                span, "info" if error is None else "error", "sampled %s" % self.OPERATION_ID, attrs=attrs_dict
            )
        return resp


class _ImageCreateHook(_ImageHook):
    _request_arg_params = ("api_key", "api_base", "api_type", "api_version", "organization")
    _request_kwarg_params = ("prompt", "n", "size", "response_format", "user")
    ENDPOINT_NAME = "images/generations"
    OPERATION_ID = "createImage"


class _ImageEditHook(_ImageHook):
    _request_arg_params = (None, None, "api_key", "api_base", "api_type", "api_version", "organization")
    _request_kwarg_params = ("prompt", "n", "size", "response_format", "user", "image", "mask")
    ENDPOINT_NAME = "images/edits"
    OPERATION_ID = "createImageEdit"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if not integration.is_pc_sampled_span:
            return
        image = args[1] if len(args) >= 2 else kwargs.get("image", "")
        mask = args[2] if len(args) >= 3 else kwargs.get("mask", "")
        if image:
            if hasattr(image, "name"):
                span.set_tag_str("openai.request.image", integration.trunc(image.name.split("/")[-1]))
            else:
                span.set_tag_str("openai.request.image", "")
        if mask:
            if hasattr(mask, "name"):
                span.set_tag_str("openai.request.mask", integration.trunc(mask.name.split("/")[-1]))
            else:
                span.set_tag_str("openai.request.mask", "")


class _ImageVariationHook(_ImageHook):
    _request_arg_params = (None, "api_key", "api_base", "api_type", "api_version", "organization")
    _request_kwarg_params = ("n", "size", "response_format", "user", "image")
    ENDPOINT_NAME = "images/variations"
    OPERATION_ID = "createImageVariation"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if not integration.is_pc_sampled_span:
            return
        image = args[1] if len(args) >= 2 else kwargs.get("image", "")
        if image:
            if hasattr(image, "name"):
                span.set_tag_str("openai.request.image", integration.trunc(image.name.split("/")[-1]))
            else:
                span.set_tag_str("openai.request.image", "")


class _BaseAudioHook(_EndpointHook):
    _request_arg_params = ("model", None, "api_key", "api_base", "api_type", "api_version", "organization")
    _response_attrs = ("language", "duration")
    ENDPOINT_NAME = "audio"
    HTTP_METHOD_TYPE = "POST"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if not integration.is_pc_sampled_span:
            return
        audio_file = args[2] if len(args) >= 3 else kwargs.get("file", "")
        if audio_file and hasattr(audio_file, "name"):
            span.set_tag_str("openai.request.filename", integration.trunc(audio_file.name.split("/")[-1]))
        else:
            span.set_tag_str("openai.request.filename", "")

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        resp_to_tag = resp.model_dump() if hasattr(resp, "model_dump") else resp
        if isinstance(resp_to_tag, str):
            text = resp
        elif isinstance(resp_to_tag, dict):
            text = resp_to_tag.get("text", "")
            if "segments" in resp_to_tag:
                span.set_metric("openai.response.segments_count", len(resp_to_tag.get("segments")))
        else:
            text = ""
        if integration.is_pc_sampled_span(span):
            span.set_tag_str("openai.response.text", integration.trunc(text))
        if integration.is_pc_sampled_log(span):
            file_input = args[2] if len(args) >= 3 else kwargs.get("file", "")
            integration.log(
                span,
                "info" if error is None else "error",
                "sampled %s" % self.OPERATION_ID,
                attrs={
                    "file": getattr(file_input, "name", "").split("/")[-1],
                    "prompt": kwargs.get("prompt", ""),
                    "language": kwargs.get("language", ""),
                    "text": text,
                },
            )
        return resp


class _AudioTranscriptionHook(_BaseAudioHook):
    _request_kwarg_params = (
        "prompt",
        "response_format",
        "temperature",
        "language",
        "user",
    )
    ENDPOINT_NAME = "audio/transcriptions"
    OPERATION_ID = "createTranscription"


class _AudioTranslationHook(_BaseAudioHook):
    _request_kwarg_params = (
        "prompt",
        "response_format",
        "temperature",
        "user",
    )
    ENDPOINT_NAME = "audio/translations"
    OPERATION_ID = "createTranslation"


class _ModerationHook(_EndpointHook):
    _request_arg_params = ("input", "model", "api_key")
    _request_kwarg_params = ("input", "model")
    _response_attrs = ("id", "model")
    _response_categories = (
        "hate",
        "hate/threatening",
        "harassment",
        "harassment/threatening",
        "self-harm",
        "self-harm/intent",
        "self-harm/instructions",
        "sexual",
        "sexual/minors",
        "violence",
        "violence/graphic",
    )
    ENDPOINT_NAME = "moderations"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createModeration"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        results = resp.results[0]
        categories = results.categories
        scores = results.category_scores
        for category in self._response_categories:
            span.set_metric("openai.response.category_scores.%s" % category, getattr(scores, category, 0))
            span.set_metric("openai.response.categories.%s" % category, int(getattr(categories, category)))
        span.set_metric("openai.response.flagged", int(results.flagged))
        return resp


class _BaseFileHook(_EndpointHook):
    ENDPOINT_NAME = "files"


class _FileCreateHook(_BaseFileHook):
    _request_arg_params = (
        None,
        "purpose",
        "model",
        "api_key",
        "api_base",
        "api_type",
        "api_version",
        "organization",
        "user_provided_filename",
    )
    _request_kwarg_params = ("purpose",)
    _response_attrs = ("id", "bytes", "created_at", "filename", "purpose", "status", "status_details")
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createFile"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        fp = args[1] if len(args) >= 2 else kwargs.get("file", "")
        if fp and hasattr(fp, "name"):
            span.set_tag_str("openai.request.filename", fp.name.split("/")[-1])
        else:
            span.set_tag_str("openai.request.filename", "")

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        return resp


class _FileDownloadHook(_BaseFileHook):
    _request_arg_params = (None, "api_key", "api_base", "api_type", "api_version", "organization")
    HTTP_METHOD_TYPE = "GET"
    OPERATION_ID = "downloadFile"
    ENDPOINT_NAME = "files/*/content"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.file_id", args[1] if len(args) >= 2 else kwargs.get("file_id", ""))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        if isinstance(resp, bytes) or isinstance(resp, str):
            span.set_metric("openai.response.total_bytes", len(resp))
        else:
            span.set_metric("openai.response.total_bytes", getattr(resp, "total_bytes", 0))
        return resp


class _BaseFineTuneHook(_EndpointHook):
    _response_attrs = ("id", "model", "fine_tuned_model", "status", "created_at", "updated_at")

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        span.set_metric("openai.response.events_count", len(resp.events))
        span.set_metric("openai.response.result_files_count", len(resp.result_files))
        span.set_metric("openai.response.training_files_count", len(resp.training_files))
        span.set_metric("openai.response.validation_files_count", len(resp.validation_files))
        hyperparams = resp.hyperparams
        for hyperparam in ("batch_size", "learning_rate_multiplier", "n_epochs", "prompt_loss_weight"):
            span.set_tag_str("openai.response.hyperparams.%s" % hyperparam, str(getattr(hyperparams, hyperparam, "")))

        return resp


class _FineTuneCreateHook(_BaseFineTuneHook):
    _request_arg_params = ("api_key", "api_base", "api_type", "request_id", "api_version", "organization")
    _request_kwarg_params = (
        "training_file",
        "validation_file",
        "model",
        "n_epochs",
        "batch_size",
        "learning_rate_multiplier",
        "prompt_loss_weight",
        "compute_classification_metrics",
        "classification_n_classes",
        "classification_positive_class",
        "suffix",
    )
    ENDPOINT_NAME = "fine-tunes"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "createFineTune"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        if "classification_betas" in kwargs:
            classification_betas = kwargs.get("classification_betas", [])
            if classification_betas:
                span.set_metric("openai.request.classification_betas_count", len(classification_betas))
            else:
                span.set_metric("openai.request.classification_betas_count", 0)


class _FineTuneCancelHook(_BaseFineTuneHook):
    _request_arg_params = (None, "api_key", "api_type", "request_id", "api_version")
    _request_kwarg_params = ("user",)
    ENDPOINT_NAME = "fine-tunes/*/cancel"
    HTTP_METHOD_TYPE = "POST"
    OPERATION_ID = "cancelFineTune"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.fine_tune_id", args[1] if len(args) >= 2 else kwargs.get("fine_tune_id", ""))


class _FineTuneListEventsHook(_EndpointHook):
    _request_kwarg_params = ("stream", "user")
    ENDPOINT_NAME = "fine-tunes/*/events"
    HTTP_METHOD_TYPE = "GET"
    OPERATION_ID = "listFineTuneEvents"

    def _record_request(self, pin, integration, span, args, kwargs):
        super()._record_request(pin, integration, span, args, kwargs)
        span.set_tag_str("openai.request.fine_tune_id", args[1] if len(args) >= 2 else kwargs.get("fine_tune_id", ""))

    def _record_response(self, pin, integration, span, args, kwargs, resp, error):
        resp = super()._record_response(pin, integration, span, args, kwargs, resp, error)
        if not resp:
            return
        span.set_metric("openai.response.count", len(resp.data))
        return resp

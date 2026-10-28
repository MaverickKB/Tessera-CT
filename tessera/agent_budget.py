"""Per-claim provider limits; uncertain usage is charged, never invented as zero."""

from copy import deepcopy
import time

from .backend import Backend, BackendError
from .rate_limits import number, usage_totals


class AgentBackend(Backend):
    def __init__(self, settings, context, observe, *, delegate=None):
        super().__init__(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        self.context = context
        self.observe = observe
        self.delegate = delegate

    def cancel(self):
        if self.delegate is not None:
            cancel = getattr(self.delegate, 'cancel', None)
            return bool(callable(cancel) and cancel())
        return super().cancel()

    def chat(self, messages, tools=None, **options):
        limits = self.context()
        remaining = int(limits['tokens'])
        seconds = limits['seconds']
        if remaining <= 0 or seconds <= 0:
            raise BackendError('Agent execution budget exhausted', 'quota')
        self.extra['retries'] = 0
        self.extra['timeout'] = min(float(self.extra.get('timeout', seconds)), seconds)
        if self.worker:
            self.worker.settings.timeout = min(self.worker.settings.timeout, seconds)
        else:
            parameters = deepcopy(self.extra.get('parameters', {}))
            for source in (self.extra, parameters, options):
                value = source.get('max_tokens')
                if type(value) is int and value > 0:
                    remaining = min(remaining, value)
            parameters['max_tokens'] = remaining
            self.extra['parameters'] = parameters
            self.extra['max_tokens'] = remaining
            options['max_tokens'] = remaining
        started = time.monotonic()
        try:
            response = (self.delegate.chat(messages, tools=tools, **options) if self.delegate is not None
                        else super().chat(messages, tools=tools, **options))
        except Exception:
            self.observe({'tokens': limits['tokens'], 'seconds': time.monotonic() - started}, False)
            raise
        usage = usage_totals(response.get('usage'))
        raw = response.get('usage') or {}
        reported = (number(raw.get('total_tokens', raw.get('tokens'))) is not None
                    or (number(raw.get('input_tokens', raw.get('prompt_tokens'))) is not None
                        and number(raw.get('output_tokens', raw.get('completion_tokens'))) is not None))
        if not reported:
            usage['tokens'] = limits['tokens']
        usage['seconds'] = time.monotonic() - started
        self.observe(usage, reported)
        if usage['tokens'] > limits['tokens'] or usage['seconds'] > seconds:
            error = BackendError('Agent execution exceeded its budget', 'quota')
            error.usage = usage
            raise error
        return response

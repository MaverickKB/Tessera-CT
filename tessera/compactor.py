"""Background workers consuming ready leaves and ready sibling parents."""

from queue import Empty
import math
import re
import threading
import time

from .prompts import (COMPACTION_FAITHFULNESS_RETRY_TEMPLATE,
                      COMPACTION_RETRY_TEMPLATE, COMPACTION_TASK_TEMPLATE)
from .tree import utf8_prefix


class Compactor:
    def __init__(self, tree, view, backend, system_prompt, tools_schema, workers=4,
                 *, retries=2, context_bytes=16000, judge=None, judge_threshold=.8):
        if workers <= 0 or retries < 0 or context_bytes < 0:
            raise ValueError('Invalid compactor worker, retry or context budget')
        if judge is not None and not callable(judge):
            raise ValueError('judge must be callable or None')
        if (isinstance(judge_threshold, bool) or not isinstance(judge_threshold, (int, float))
                or not math.isfinite(judge_threshold) or not 0 <= judge_threshold <= 1):
            raise ValueError('judge_threshold must be a finite probability')
        self.tree = tree
        self.view = view
        self.backend = backend
        self.system_prompt = system_prompt
        self.tools_schema = tools_schema
        self.workers = workers
        self.retries = retries
        self.context_bytes = min(context_bytes, max(0, view.low_bytes // 2))
        self.judge = judge
        self.judge_threshold = float(judge_threshold)
        self.threads = []
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self.errors = []

    def start(self):
        with self._lifecycle:
            for thread in self.threads:
                if thread.is_alive():
                    return
            # A crash can occur between a durable tree append and its View
            # callback. Resume from those built nodes even with empty queues.
            self.view.on_node(0, 0)
            self._stop.clear()
            self.threads = []
            for index in range(self.workers):
                thread = threading.Thread(target=self._worker,
                                          name=f'tessera-compactor-{index}', daemon=True)
                self.threads.append(thread)
                thread.start()

    def stop(self):
        with self._lifecycle:
            self._stop.set()
            with self.tree.changed:
                self.tree.changed.notify_all()
            for thread in self.threads:
                thread.join()

    def _next_job(self):
        # Prefer recent leaves; parents enter the second queue exactly when
        # both children finish. No runtime walk of the log or tree is needed.
        for queue in (self.tree.ready_leaves(), self.tree.ready_merges()):
            try:
                return queue, queue.get_nowait()
            except Empty:
                continue
        return None, None

    def _worker(self):
        while not self._stop.is_set():
            with self.tree.changed:
                queue, key = self._next_job()
                if key is None:
                    self.tree.changed.wait(timeout=.25)
                    continue
            try:
                if not self.tree.is_built(*key):
                    text, metadata = self._summarize(*key)
                    self.tree.set_node(*key, text, meta=metadata)
            except Exception as error:
                # Storage failures stay visible and wake waiters. Backend
                # failures are handled by _summarize's extractive fallback.
                with self.tree.changed:
                    self.errors.append((key, type(error).__name__))
                    self.tree.changed.notify_all()
            finally:
                queue.task_done()

    def _source(self, level, index):
        if level == 0:
            message = self.tree.log.get(index)
            return f'[{message.kind}] {message.text}'
        parts = []
        for child in (index * 2, index * 2 + 1):
            text = self.tree.node(level - 1, child)
            if level == 1:
                kind = self.tree.log.get(child).kind
                if not text.startswith(f'[{kind}]'):
                    text = f'[{kind}] {text}'
            parts.append(text)
        return '\n'.join(parts)

    def _fallback(self, source):
        fragments = re.split(r'(?=\[(?:user|agent|tool|echo|work|note)\])', source)
        user_parts = []
        other_parts = []
        for fragment in fragments:
            fragment = ' '.join(fragment.split())
            if not fragment:
                continue
            if fragment.startswith('[user]'):
                user_parts.append(fragment)
            else:
                other_parts.append(fragment)
        return utf8_prefix(' '.join(user_parts + other_parts), self.tree.node_bytes)

    def _summarize(self, level, index):
        source = self._source(level, index)
        if self.backend is None:
            return self._fallback(source), {}
        budget = self.tree.node_bytes
        ruler = utf8_prefix('1234567890' * ((budget + 9) // 10), budget)
        task = COMPACTION_TASK_TEMPLATE.format(start=index * (1 << level),
                                               count=1 << level, node_bytes=budget,
                                               ruler=ruler, source=source)
        context = utf8_prefix(self.view.render(), self.context_bytes)
        messages = [{'role': 'system', 'content': self.system_prompt},
                    {'role': 'user', 'content': context},
                    {'role': 'user', 'content': task}]
        text, generated = self._model_line(messages, source, self.retries)
        if self.judge is None or not generated:
            return text, {}
        score, error = self._faithfulness(text, source)
        metadata = {'faithfulness_score': score, 'faithfulness_scores': [score],
                    'faithfulness_threshold': self.judge_threshold,
                    'faithfulness_retried': False, 'faithfulness_passed': None}
        if error:
            metadata['faithfulness_error'] = error
            return text, metadata
        metadata['faithfulness_passed'] = score >= self.judge_threshold
        if metadata['faithfulness_passed']:
            return text, metadata
        metadata['faithfulness_retried'] = True
        repair = COMPACTION_FAITHFULNESS_RETRY_TEMPLATE.format(
            score=score, threshold=self.judge_threshold, node_bytes=budget)
        retry_messages = messages + [{'role': 'assistant', 'content': text},
                                     {'role': 'user', 'content': repair}]
        candidate, generated = self._model_line(retry_messages, source, 0)
        if not generated:
            metadata['faithfulness_retry_failed'] = True
            return text, metadata
        retry_score, error = self._faithfulness(candidate, source)
        metadata['faithfulness_scores'].append(retry_score)
        if error:
            metadata['faithfulness_error'] = error
            return text, metadata
        if retry_score >= score:
            text = candidate
            metadata['faithfulness_score'] = retry_score
            metadata['faithfulness_passed'] = retry_score >= self.judge_threshold
        return text, metadata

    def _faithfulness(self, text, source):
        try:
            score = self.judge(text, source)
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1):
                raise ValueError('Judge must return a finite probability')
            return float(score), None
        except Exception as error:
            # Exceptions can contain credentials or endpoint bodies. Persist
            # only the type and leave the score explicitly unavailable.
            return None, type(error).__name__

    def _model_line(self, messages, source, retries):
        budget = self.tree.node_bytes
        shortest = None
        for attempt in range(retries + 1):
            try:
                response = self.backend.chat(messages, tools=self.tools_schema,
                                             tool_choice='none')
                text = ' '.join(response['content'].split())
                if not text:
                    raise ValueError('Empty summary')
            except Exception:
                if shortest is None:
                    return self._fallback(source), False
                break
            size = len(text.encode('utf-8'))
            if shortest is None or size < len(shortest.encode('utf-8')):
                shortest = text
            if size <= budget:
                return text, True
            if attempt < retries:
                cut_text = utf8_prefix(text, budget)
                retry = COMPACTION_RETRY_TEMPLATE.format(
                    actual_bytes=size, node_bytes=budget, cut_text=cut_text,
                    overflow=text[len(cut_text):])
                # A fresh list lets backends retain an accurate request trace.
                messages = messages + [{'role': 'assistant', 'content': text},
                                       {'role': 'user', 'content': retry}]
        return utf8_prefix(shortest, budget), True

    def wait_until_summarized(self, upto_i, timeout):
        if type(upto_i) is not int or upto_i < -1 or upto_i >= self.tree.log.count():
            raise ValueError('Summary wait index is outside the chat')
        deadline = time.monotonic() + max(0, timeout)
        with self.tree.changed:
            while not self.tree.prefix_built(upto_i):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.errors:
                    return False
                self.tree.changed.wait(timeout=remaining)
        # Node persistence precedes view notification. Waiters need the view
        # updated too, so explicitly finish any currently eligible batch.
        self.view.on_node(0, upto_i)
        return True

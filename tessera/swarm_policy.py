"""Synchronous admission policies, with durable usage supplied by the task board."""

import random as random_module
import threading
import time

from .rate_limits import day_key, number, usage_totals


class AccountPolicy:
    def __init__(self, accounts=None, settings=None, *, enabled=False, clock=time.time, random=random_module.random):
        self.enabled = enabled
        self.accounts = accounts or {}
        self.settings = settings
        self.clock = clock
        self.random = random
        self._states = {}
        self._lock = threading.RLock()

    def _state(self, account):
        if account not in self._states:
            self._states[account] = {'failures': 0, 'open_until': 0, 'rates': {}}
        return self._states[account]

    @staticmethod
    def _refresh_rates(rates, now):
        for resource in ('requests', 'tokens', 'input_tokens', 'output_tokens'):
            deadline = rates.get(resource + '_reset_at')
            if deadline is not None and deadline <= now:
                rates.pop(resource + '_remaining', None)
                rates.pop(resource + '_reset_at', None)
        if rates.get('retry_at', float('inf')) <= now:
            rates.pop('retry_at', None)

    def rate_ready(self, account, now):
        if not self.enabled:
            return True
        with self._lock:
            rates = self._state(account)['rates']
            self._refresh_rates(rates, now)
            if rates.get('retry_at', 0) > now:
                return False
            for resource in ('requests', 'tokens', 'input_tokens', 'output_tokens'):
                if rates.get(resource + '_remaining', 1) <= 0:
                    deadline = rates.get(resource + '_reset_at')
                    if deadline is None or deadline > now:
                        return False
            return True

    def budget_allows(self, task):
        return self.admit(task, [])

    def admit(self, task, tasks):
        if not self.enabled:
            return True
        with self._lock:
            rates = self._state(task['account'])['rates']
            self._refresh_rates(rates, self.clock())
            if not self.rate_ready(task['account'], self.clock()):
                return False
            for resource in ('tokens', 'input_tokens', 'output_tokens'):
                remaining = rates.get(resource + '_remaining')
                if remaining is not None:
                    if 'tokens' not in task['budget']:
                        return False
                    requested = max(0, task['budget']['tokens'] - task.get('usage', {}).get('tokens', 0))
                    if requested > remaining:
                        return False
        used = task.get('usage', {})
        for unit in ('tokens', 'money', 'seconds'):
            if unit in task['budget'] and used.get(unit, 0) >= task['budget'][unit]:
                return False
        account = self.accounts.get(task['account'])
        if account is None:
            return False
        today = day_key(self.clock())
        for unit in ('tokens', 'money', 'seconds'):
            for period in ('total', 'day'):
                option = unit if period == 'total' else unit + '_per_day'
                limit = account.budget.get(option)
                if limit is None:
                    continue
                if unit not in task['budget']:
                    return False
                requested = max(0, task['budget'][unit] - used.get(unit, 0))
                spent = 0
                for other in tasks:
                    if other['account'] != task['account']:
                        continue
                    if period == 'total':
                        spent += other.get('usage', {}).get(unit, 0)
                    else:
                        spent += other.get('daily_usage', {}).get(today, {}).get(unit, 0)
                    if other['status'] == 'running':
                        spent += max(0, other['budget'].get(unit, 0) - other.get('usage', {}).get(unit, 0))
                if spent + requested > limit:
                    return False
        return True

    def claimed(self, claim):
        if not self.enabled:
            return
        with self._lock:
            rates = self._state(claim['account'])['rates']
            for resource in ('requests', 'tokens', 'input_tokens', 'output_tokens'):
                key = resource + '_remaining'
                if key in rates:
                    reservation = 1 if resource == 'requests' else claim.get('reservation', {}).get('tokens', 0)
                    rates[key] = max(0, rates[key] - reservation)

    def circuit_allows(self, account):
        if not self.enabled:
            return True
        with self._lock:
            return self.clock() >= self._state(account)['open_until']

    def retry_delay(self, task, category):
        if not self.enabled or category not in ('rate_limit', 'timeout', 'connection', 'provider'):
            return None
        exponent = min(max(0, task['attempt'] - 1), 30)
        ceiling = min(self.settings.retry_cap, self.settings.retry_base * (2 ** exponent))
        return ceiling * self.random()

    def observe(self, account, success, category=None, usage=None, rate_limits=None):
        if not self.enabled:
            return
        with self._lock:
            state = self._state(account)
            metadata = rate_limits if isinstance(rate_limits, dict) else {}
            for key, value in metadata.items():
                if key == 'retry_at' or key in ('requests_remaining', 'requests_reset_at', 'tokens_remaining', 'tokens_reset_at',
                                                'input_tokens_remaining', 'input_tokens_reset_at', 'output_tokens_remaining', 'output_tokens_reset_at'):
                    normalized = number(value)
                    if normalized is not None:
                        state['rates'][key] = normalized
            if success:
                state['failures'] = 0
                state['open_until'] = 0
            else:
                if category in ('rate_limit', 'timeout', 'connection', 'provider'):
                    state['failures'] += 1
                    if state['failures'] >= self.settings.circuit_failures:
                        state['open_until'] = self.clock() + self.settings.circuit_cooldown
                elif category in ('authentication', 'permission', 'quota', 'configuration'):
                    state['open_until'] = self.clock() + self.settings.circuit_cooldown

    def charge(self, claim, usage=None):
        if not self.enabled:
            return None
        totals = usage_totals(usage)
        for unit in ('tokens', 'money', 'seconds'):
            if unit not in totals and unit in claim['budget']:
                totals[unit] = max(0, claim['budget'][unit] - claim.get('usage', {}).get(unit, 0))
        return totals

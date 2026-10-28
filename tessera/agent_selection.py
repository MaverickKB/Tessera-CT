"""Operator-ranked worker backends, selected against a declared task level."""

LEVELS = {'routine': 1, 'standard': 2, 'complex': 3}


def backend_catalog(config):
    rows = []
    for name, policy in config.agents.backend_policy.items():
        backend = config.backends[name]
        rows.append({'backend': name, 'model': backend.model,
                     'capability': policy['capability'], 'cost_rank': policy['cost_rank'],
                     'description': policy.get('description', ''),
                     'enabled': backend.enabled})
    return sorted(rows, key=lambda row: (row['cost_rank'], row['backend']))


def select_backend(config, backend=None, capability='routine'):
    if not isinstance(capability, str) or capability not in LEVELS:
        raise ValueError('Agent capability must be routine, standard or complex')
    if backend is not None:
        if not isinstance(backend, str) or backend not in config.backends or not config.backends[backend].enabled:
            raise ValueError('Agent backend is missing or disabled')
        return backend
    if config.agents.backend_policy:
        for row in backend_catalog(config):
            if row['enabled'] and LEVELS[row['capability']] >= LEVELS[capability]:
                return row['backend']
        raise ValueError('No enabled agent backend meets the requested capability')
    return config.agents.worker_backend or config.roles.get('main')

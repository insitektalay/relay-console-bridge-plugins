"""Publish only the model selected in this Hermes installation."""
import re


def configured_model_catalog(config, observed_at):
    selected = config.get('model') if isinstance(config, dict) else None
    model = selected.get('default') if isinstance(selected, dict) else selected
    provider = selected.get('provider') if isinstance(selected, dict) else None
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,200}', model):
        return None
    entry = {'id': model, 'model': model, 'label': model, 'availability': 'unknown'}
    if isinstance(provider, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,100}', provider):
        entry['provider'] = provider
    return {'runtimeType': 'hermes', 'defaultModel': model, 'models': [model],
            'entries': [entry], 'source': 'hermes-configured-model', 'observedAt': observed_at}

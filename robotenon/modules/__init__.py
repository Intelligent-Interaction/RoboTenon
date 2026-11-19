"""Lazy compatibility exports; resource construction belongs to bootstrap."""
from importlib import import_module

_EXPORTS = {'EchoModule': ('.builtin', 'EchoModule'),
 'TerminalEvaluator': ('.builtin', 'TerminalEvaluator'),
 'WaitAgent': ('.builtin', 'WaitAgent'),
 'BaseModule': ('.base', 'BaseModule'),
 'Module': ('.base', 'Module'),
 'ModuleContext': ('.base', 'ModuleContext'),
 'ModuleResult': ('.base', 'ModuleResult'),
 'ModuleRuntime': ('.base', 'ModuleRuntime')}

_EXPORTS.update({
    "NodeResult": ("..contracts.module", "NodeResult"),
    "GraphModule": ("..contracts.module", "GraphModule"),
    "CallableModule": ("..contracts.module", "CallableModule"),
})
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value

"""Qwen3.5 vision-language reasoner for a separate Transformers 5 runtime.

Shares the message, session, batching and JSON contracts with Qwen3-VL while
requiring the Qwen3.5 architecture explicitly. Does not load model dependencies
in the simulator or the Xiaomi runtime.
"""

from .backend import Provider as BaseProvider


class Provider(BaseProvider):
    model_type = 'qwen3_5'
    model_class_name = 'Qwen3_5ForConditionalGeneration'

"""Real Qwen3-VL batch Provider; no weights/dependencies imported in the simulator."""
import json
import os
import time
from pathlib import Path


def _has_complete_json_object(text):
    """Return true once the first top-level JSON object is complete."""
    start = text.find('{')
    if start < 0:
        return False
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict)


def _answer_text(text, thinking=False):
    """Exclude the model's reasoning section from its public JSON response."""
    if not thinking:
        return text
    if '</think>' not in text:
        return None
    return text.rsplit('</think>', 1)[1].strip()


class _JsonCompletionLogitsProcessor:
    """Force EOS per sample immediately after a complete JSON object."""

    def __init__(self, tokenizer, prompt_length, eos_token_id, thinking=False):
        self.tokenizer = tokenizer
        self.prompt_length = prompt_length
        self.eos_token_id = eos_token_id
        self.thinking = thinking

    def __call__(self, input_ids, scores):
        generated = input_ids[:, self.prompt_length:]
        texts = self.tokenizer.batch_decode(
            generated, skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        for row, value in enumerate(texts):
            answer = _answer_text(value, self.thinking)
            if answer is not None and _has_complete_json_object(answer):
                scores[row].fill_(float('-inf'))
                scores[row, self.eos_token_id] = 0
        return scores


def messages_for(payload, previous_images=None):
    import numpy as np
    from PIL import Image
    purpose = payload.get('purpose')
    if purpose not in {'planner', 'verifier'}:
        raise ValueError('Qwen reasoner requires purpose=planner or verifier')
    observation = payload['observation']
    images = observation.get('images', {})
    if not images:
        raise ValueError('Qwen reasoner requires real observation images')
    content = []

    def append_images(values, temporal_label):
        for name, array in values.items():
            image = np.asarray(array)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[-1] != 3:
                raise ValueError(f'{name}: expected uint8 HWC RGB')
            content.extend([
                {'type':'text','text':f'{temporal_label} — camera: {name}'},
                {'type':'image','image':Image.fromarray(image.copy())},
            ])

    if purpose == 'planner' and previous_images:
        append_images(previous_images, 'Previous planner observation')
    append_images(images, 'Current observation')
    content.append({'type':'text', 'text':payload['prompt']})
    return [{'role':'system','content':[{'type':'text','text':payload.get('system_prompt','Return exactly one JSON object.')}]},
            {'role':'user','content':content}]


def parse_output(text, purpose):
    from robotenon.tools.qwen35_9b import _extract_json
    if purpose not in {'planner', 'verifier'}:
        raise ValueError('unknown reasoner purpose')
    # af74484a: the Tool parses one JSON object. PlanModule owns coercion
    # and defaults; VerifyModule owns its boolean/verdict validation.
    return _extract_json(text)


class Provider:
    model_type = 'qwen3_vl'
    model_class_name = 'Qwen3VLForConditionalGeneration'

    def __init__(self, model_path, max_new_tokens=256, max_response_retries=2, metrics_dir=None,
                 enable_thinking=False):
        import torch
        import transformers
        from transformers import AutoProcessor
        model_path = str(Path(model_path).resolve())
        if json.loads((Path(model_path)/'config.json').read_text()).get('model_type') != self.model_type:
            raise ValueError(f'This backend requires model_type={self.model_type}')
        if max_new_tokens < 1 or max_response_retries < 0:
            raise ValueError('invalid generation limits')
        model_class = getattr(transformers, self.model_class_name, None)
        if model_class is None:
            raise ImportError(f'{self.model_class_name} is unavailable; install the backend-specific Transformers requirements')
        self.processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
        self.processor.tokenizer.padding_side = 'left'
        self.model = model_class.from_pretrained(model_path,
            dtype=torch.bfloat16, attn_implementation='sdpa', local_files_only=True).to('cuda').eval()
        self.max_new_tokens = max_new_tokens
        self.enable_thinking = bool(enable_thinking)
        self.max_response_retries = max_response_retries
        self.metrics_dir = Path(metrics_dir) if metrics_dir else None
        self.sessions = {}
        self.counts = {'planner':0,'verifier':0,'forward_batches':0,'response_retries':0}
        self.last_results = []
        self.last_failures = []
        self._write_metrics('running')

    def open_session(self, session_id, metadata):
        if session_id in self.sessions:
            raise ValueError('duplicate session')
        self.sessions[session_id] = {'metadata': metadata, 'previous_planner_images': None}

    def _generate(self, conversations, token_budget):
        import torch
        from transformers import LogitsProcessorList
        thinking = getattr(self, 'enable_thinking', False)
        try:
            inputs = self.processor.apply_chat_template(conversations, tokenize=True,
                add_generation_prompt=True, return_dict=True, return_tensors='pt',
                enable_thinking=thinking, processor_kwargs={'padding': True})
            inputs = inputs.to(self.model.device)
            eos = self.model.generation_config.eos_token_id
            eos = eos[0] if isinstance(eos, list) else eos
            if eos is None:
                eos = self.processor.tokenizer.eos_token_id
            json_completion = _JsonCompletionLogitsProcessor(
                self.processor.tokenizer, inputs['input_ids'].shape[1], int(eos), thinking=thinking
            )
            with torch.inference_mode():
                outputs = self.model.generate(
                    **inputs,
                    max_new_tokens=token_budget,
                    do_sample=False,
                    logits_processor=LogitsProcessorList([json_completion]),
                )
        except Exception:
            # Batch service errors cross a process/socket boundary. Preserve the
            # originating model traceback in its log so failures remain
            # diagnosable without serializing model internals to simulators.
            import traceback
            traceback.print_exc()
            raise
        generated = outputs[:, inputs['input_ids'].shape[1]:].cpu()
        text = self.processor.batch_decode(generated, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        # Reject truncated JSON even if an inner object can be parsed.
        eos = self.model.generation_config.eos_token_id
        eos = [eos] if isinstance(eos,int) else (eos or [])
        truncated = [len(row)>=token_budget and not any(int(t) in eos for t in row) for row in generated.tolist()]
        answers = [_answer_text(value, thinking) for value in text]
        return [(answer if answer is not None else raw, cut or answer is None)
                for answer, raw, cut in zip(answers, text, truncated)]

    def infer_batch(self, requests):
        if len({r['request_id'] for r in requests}) != len(requests):
            raise ValueError('duplicate request ids')
        pending=[]
        for request in requests:
            if request['session_id'] not in self.sessions:
                raise ValueError('unknown session')
            payload = request['observation']
            session = self.sessions[request['session_id']]
            previous = session.get('previous_planner_images') if payload.get('purpose') == 'planner' else None
            pending.append((request, messages_for(payload, previous), self.max_new_tokens))
            if payload.get('purpose') == 'planner':
                import numpy as np
                session['previous_planner_images'] = {
                    name: np.asarray(value).copy()
                    for name, value in payload['observation'].get('images', {}).items()
                }
        results={}
        decisions=[]
        for attempt in range(self.max_response_retries+1):
            retry=[]
            # Retry only failed requests; preserve the original prompt. Group
            # token budgets so one truncated sample does not change other limits.
            for budget in dict.fromkeys(item[2] for item in pending):
                group=[item for item in pending if item[2]==budget]
                self.counts['forward_batches']+=1
                generated=self._generate([messages for _,messages,_ in group],budget)
                if len(generated)!=len(group):
                    raise RuntimeError('Qwen returned the wrong batch length')
                for (request,messages,_),(text,truncated) in zip(group,generated):
                    purpose=request['observation']['purpose']
                    try:
                        if truncated:raise ValueError('completion reached token limit')
                        value=parse_output(text,purpose)
                    except (ValueError,TypeError) as error:
                        self.last_failures=getattr(self,'last_failures',[])+[{
                            'purpose':purpose, 'request_id':request['request_id'],
                            'session_id':request['session_id'], 'attempt':attempt+1,
                            'token_budget':budget, 'truncated':truncated,
                            'error':str(error), 'raw_output':text[:16384]}]
                        self.last_failures=self.last_failures[-16:]
                        self._write_metrics('running')
                        if attempt==self.max_response_retries:
                            raise RuntimeError(f'{purpose} returned invalid JSON after retries: {error}') from error
                        self.counts['response_retries']+=1
                        next_budget=min(budget*2,self.max_new_tokens*4) if truncated else budget
                        retry.append((request,messages,next_budget))
                        continue
                    results[request['request_id']]={'request_id':request['request_id'],'output':value}
                    self.counts[purpose]+=1
                    decisions.append({
                        'purpose': purpose, 'request_id': request['request_id'],
                        'session_id': request['session_id'],
                        'task_info': {key: self.sessions[request['session_id']].get('metadata', {}).get(key)
                                      for key in ('benchmark_id', 'episode_id', 'task')},
                        'prompt': request['observation']['prompt'], 'output': value,
                        'image_count': sum(part.get('type') == 'image'
                                           for message in messages for part in message['content']),
                    })
                    self.last_results.append({'purpose':purpose,'request_id':request['request_id'],
                        'image_count':len(request['observation']['observation']['images']),'output':value})
                    self.last_results=self.last_results[-8:]
            pending=retry
            if not pending:break
        if self.metrics_dir and decisions:
            # One append per inference batch; retain model decisions for paired
            # error analysis without serializing observations or model tensors.
            self.metrics_dir.mkdir(parents=True, exist_ok=True)
            with (self.metrics_dir / f'{os.getpid()}.decisions.jsonl').open('a') as stream:
                stream.write(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in decisions))
        self._write_metrics('running')
        return [results[r['request_id']] for r in requests]

    def close_session(self,session_id,result):
        self.sessions.pop(session_id,None)

    def _write_metrics(self,status):
        if self.metrics_dir:
            self.metrics_dir.mkdir(parents=True,exist_ok=True)
            path=self.metrics_dir/f'{os.getpid()}.json';temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'status':status,'counts':self.counts,
                'sessions':len(self.sessions),'updated_at':time.time(),'last_results':self.last_results,
                'last_failures':getattr(self,'last_failures',[])},indent=2))
            temporary.replace(path)

    def close(self):
        self.sessions.clear()
        self._write_metrics('closed')
        self.model = self.processor = None

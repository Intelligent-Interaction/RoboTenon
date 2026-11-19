"""Exercise a real benchmark with an explicit synthetic action, without a model.

This checks reset/action/observation transport, not task success or score parity.
Run each simulator in its own process and environment. No simulator is mocked.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import traceback
import numpy as np
from .integration import BenchmarkEnvironment, load_class
from .codec import unpack_observation, json_value
from ..schemas import Action, ActionType, SessionSpec


def fingerprint(value):
    digest = hashlib.sha256()
    def visit(item):
        if isinstance(item, dict):
            for key in sorted(item):
                digest.update(str(key).encode()); visit(item[key])
        elif isinstance(item, (list, tuple)):
            for child in item: visit(child)
        elif isinstance(item, np.ndarray):
            digest.update(str(item.shape).encode()); digest.update(str(item.dtype).encode())
            digest.update(item.tobytes())
        else:
            digest.update(str(item).encode())
    visit(value)
    return digest.hexdigest()


def simulator_counters(native):
    """Read actual simulator counters when exposed; no inferred physics claim."""
    result, seen = {}, set()
    def visit(obj, prefix, depth):
        if obj is None or id(obj) in seen or depth > 4: return
        seen.add(id(obj))
        for name in ('time', 'timestep', 'elapsed_steps', '_elapsed_steps', 'take_action_cnt'):
            try:
                value = getattr(obj, name, None)
                if value is not None and not callable(value):
                    array = np.asarray(value)
                    if array.size == 1 and np.issubdtype(array.dtype, np.number):
                        result[prefix+'.'+name] = float(array.reshape(-1)[0])
            except (AttributeError, TypeError, ValueError): pass
        for name in ('_env', 'env', 'unwrapped', 'sim', 'data', '_env_state'):
            try: visit(getattr(obj, name, None), prefix+'.'+name, depth+1)
            except (AttributeError, TypeError, ValueError): pass
    visit(native, 'native', 0)
    return result


def simulator_health(native):
    issues, seen = [], set()
    def visit(obj, prefix, depth):
        if obj is None or id(obj) in seen or depth > 5: return
        seen.add(id(obj))
        for name in ('qpos', 'qvel', 'ctrl'):
            value = getattr(obj, name, None)
            if value is not None and not np.isfinite(np.asarray(value)).all():
                issues.append(prefix+'.'+name+': nonfinite values')
        warning = getattr(obj, 'warning', None)
        if warning is not None and hasattr(warning, 'number'):
            numbers = np.asarray(warning.number)
            if np.any(numbers): issues.append(prefix+': MuJoCo warnings '+str(numbers.tolist()))
        for name in ('_env', 'env', 'unwrapped', 'sim', 'data'):
            visit(getattr(obj, name, None), prefix+'.'+name, depth+1)
    visit(native, 'native', 0)
    return issues


def image_evidence(observation, previous=None, directory=None, prefix='reset'):
    images = observation.get('images', {})
    evidence = {}
    for index, (camera, value) in enumerate(images.items()):
        array = np.asarray(value)
        valid = array.ndim == 3 and array.shape[-1] in (3, 4) and array.size > 0
        valid = valid and bool(np.isfinite(array).all())
        item = {'shape': list(array.shape), 'dtype': str(array.dtype), 'valid': valid,
                'sha256': fingerprint(array)}
        old = (previous or {}).get(camera)
        if old is not None and old.shape == array.shape:
            item['changed_fraction'] = float(np.mean(old != array))
        if directory is not None and valid and array.dtype == np.uint8:
            from PIL import Image
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{prefix}-camera-{index}.png'
            Image.fromarray(array).save(path)
            item['file'] = str(path)
        evidence[camera] = item
    return evidence


async def probe(class_reference, params, action, steps=3, task_index=0, frames_dir=None):
    if steps < 1: raise ValueError('steps must be positive')
    environment = None
    report = {'class': class_reference, 'params': params, 'action': action,
              'requested_steps': steps, 'accepted_steps': 0, 'status': 'error', 'phase': 'import'}
    try:
        cls = load_class(class_reference)
        environment_class = type('SmokeEnvironment', (BenchmarkEnvironment,),
                                 {'benchmark_class': cls, 'benchmark_id': 'smoke_benchmark'})
        environment = environment_class(params=params)
        report['phase'] = 'open'
        session = await environment.open(SessionSpec(task_id='smoke', environment_id='smoke_benchmark',
            parameters={'native_task_index': task_index, 'native_episode': 0, 'native_max_steps': steps}))
        report['phase'] = 'reset'
        await session.reset(session.spec)
        report['initial_observation_sha256'] = fingerprint(session.native_observation)
        report['initial_images'] = image_evidence(session.native_observation, directory=frames_dir)
        previous_images = {k: np.asarray(v).copy() for k, v in session.native_observation.get('images', {}).items()}
        report['counters_before'] = simulator_counters(session.native)
        report['physics_issues'] = simulator_health(session.native)
        report['phase'] = 'step'
        records = []
        for index in range(steps):
            command = Action(session_id=session.session_id, action_type=ActionType.CUSTOM,
                payload={'format': 'smoke_benchmark', 'native_action': action})
            feedback = await session.execute_native(command)
            if session.steps != index+1: raise AssertionError('native step count did not advance')
            report['accepted_steps'] = index+1
            report['physics_issues'].extend(simulator_health(session.native))
            decoded = unpack_observation(feedback.observation)
            if fingerprint(decoded) != fingerprint(session.native_observation):
                raise AssertionError('observation transport changed native values')
            images = image_evidence(decoded, previous_images, frames_dir, f'step-{index+1}')
            previous_images = {k: np.asarray(v).copy() for k, v in decoded.get('images', {}).items()}
            records.append({'step': index+1, 'terminal': feedback.terminal,
                            'images': images, 'observation_keys': sorted(decoded),
                            'environment_info': json_value(feedback.info),
                            'feedback_success': feedback.success,
                            'observation_refreshed': feedback.info.get('reason') != 'environment_terminal',
                            'observation_sha256': fingerprint(session.native_observation),
                            'simulator_counters': simulator_counters(session.native)})
            if feedback.terminal: break
        report['steps'] = records
        after = simulator_counters(session.native)
        report['counters_after'] = after
        before = report['counters_before']
        report['simulator_counter_advanced'] = any(after[k] > v for k, v in before.items() if k in after)
        report['observation_changed'] = records[-1]['observation_sha256'] != report['initial_observation_sha256']
        report['image_stream_present'] = bool(report['initial_images']) and all(r['images'] for r in records)
        report['images_valid'] = report['image_stream_present'] and all(
            v['valid'] for r in records for v in r['images'].values())
        report['image_changed_each_step'] = report['images_valid'] and all(
            any(v.get('changed_fraction', 0) > 0 for v in r['images'].values()) for r in records)
        report['success_signal_readable'] = isinstance((session.result or {}).get('success'), bool)
        report['terminal_metrics'] = session.result
        report['terminal_success_matches_metrics'] = (report['success_signal_readable'] and
            records[-1]['feedback_success'] == session.result['success'])
        report['status'] = 'passed' if report['accepted_steps'] == steps else 'early_terminal'
        if not (report['simulator_counter_advanced'] or report['observation_changed']):
            report['status'] = 'advancement_unconfirmed'
        if report['physics_issues']:
            report['physics_issues'] = sorted(set(report['physics_issues']))
            report['status'] = 'physics_warning'
        report['phase'] = 'complete'
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error'] = str(error)
        report['traceback'] = traceback.format_exc()
    finally:
        try:
            if environment is not None: await environment.close()
        except Exception as error:
            report['cleanup_error'] = str(error)
            report['status'] = 'error'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--class', dest='class_reference', required=True)
    parser.add_argument('--params', default='{}')
    parser.add_argument('--action', required=True, help='Native action mapping as JSON')
    parser.add_argument('--steps', type=int, default=3)
    parser.add_argument('--task-index', type=int, default=0)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--save-frames', action='store_true')
    args = parser.parse_args()
    report = asyncio.run(probe(args.class_reference, json.loads(args.params),
        json.loads(args.action), args.steps, args.task_index,
        args.output.with_suffix('') / 'frames' if args.save_frames else None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    if report['status'] != 'passed': raise SystemExit(1)

if __name__ == '__main__': main()

"""Framework BatchProvider contract: arbitrary structured outputs, no actions."""
class Provider:
    def __init__(self, role='planner'):
        if role not in ('planner', 'verifier'):
            raise ValueError('role must be planner or verifier')
        self.role = role
        self.sessions = {}

    def open_session(self, session_id, metadata):
        if session_id in self.sessions:
            raise ValueError('duplicate session')
        self.sessions[session_id] = metadata

    def infer_batch(self, requests):
        results = []
        for request in requests:
            if request['session_id'] not in self.sessions:
                raise ValueError('unknown session')
            task = request['observation']['task']
            if self.role == 'planner':
                output = {'decision': 'act', 'subgoal': task, 'completed': [], 'remaining': [task], 'reason': 'fake planner'}
            else:
                output = {'subtask_success': False, 'physical_belief_update': {}}
            results.append({'request_id': request['request_id'], 'output': output})
        # Intentionally reverse results to exercise ID-based routing.
        return list(reversed(results))

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        self.sessions.clear()

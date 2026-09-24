"""Bounded Hermes cron editor. Import the selected harness; never emulate its scheduler."""
import contextlib
import hashlib
import json
import sys

MAX_BYTES = 256_000
FIELDS = ('name', 'prompt', 'schedule', 'next_run_at', 'enabled')


def revision(job):
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def snapshot(api):
    rows = api.load_jobs()
    if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows):
        raise ValueError('Native cron inventory is invalid or too large')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Native cron job IDs are ambiguous')
    return rows


def supported(api):
    return all(callable(getattr(api, name, None)) for name in ('load_jobs', 'update_job', 'parse_schedule', 'compute_next_run', '_jobs_lock', '_fire_job_lock'))


def reply(rows, can_edit):
    return {'status': 'confirmed', 'runtimeType': 'hermes', 'jobs': [dict(row, relayRevision=revision(row)) for row in rows],
            'canEdit': can_edit, 'editableFields': list(FIELDS) if can_edit else []}


def schedule_text(job):
    schedule = job.get('schedule')
    if isinstance(job.get('schedule_display'), str):
        return job['schedule_display']
    if isinstance(schedule, str):
        return schedule
    if isinstance(schedule, dict):
        for key in ('display', 'expr', 'expression'):
            if isinstance(schedule.get(key), str):
                return schedule[key]
        if isinstance(schedule.get('minutes'), (int, float)):
            return f"every {schedule['minutes']} minutes"
        return schedule.get('run_at')
    return None


def apply(api, command):
    can_edit = supported(api)
    if command.get('action') == 'list':
        return reply(snapshot(api), can_edit)
    if command.get('action') != 'update' or not can_edit:
        raise ValueError('This Hermes version supports read-only cron access')
    job_id = command.get('jobId')
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise ValueError('Select an existing cron job')
    # Match the harness lock order, including its per-job execution fence.
    with api._fire_job_lock(job_id) as acquired:
        if acquired is not True:
            raise ValueError('Native cron job lock is unavailable; refresh before another change')
        with api._jobs_lock():
            rows = snapshot(api)
            original = next((row for row in rows if row['id'] == job_id), None)
            if original is None:
                raise ValueError('The native cron job no longer exists')
            expected = command.get('baseVersion')
            if isinstance(command.get('expectedJob'), dict):
                expected = revision(command['expectedJob'])
            if expected != revision(original):
                raise ValueError('The native cron job changed. Refresh before saving')
            fields = command.get('updates')
            if not isinstance(fields, dict) or set(fields) != set(FIELDS):
                raise ValueError('Unsupported native cron fields')
            if not isinstance(fields['enabled'], bool) or any(not isinstance(fields[name], str) for name in FIELDS if name != 'enabled'):
                raise ValueError('Invalid native cron field types')
            if not fields['name'].strip() or len(fields['name']) > 200 or not fields['schedule'].strip() or len(fields['schedule']) > 200 or len(fields['next_run_at']) > 100 or len(fields['prompt'].encode()) > 64_000:
                raise ValueError('Enter a name, schedule and bounded prompt')
            changes = {'name': fields['name'].strip(), 'prompt': fields['prompt']}
            # Retain the existing artifact contract if the editor displays only the prompt.
            marker, end = '[Relay Console cron artifact contract]', '[End Relay Console cron artifact contract]'
            old_prompt = original.get('prompt') or ''
            if marker not in changes['prompt'] and marker in old_prompt and end in old_prompt:
                contract = old_prompt[old_prompt.index(marker):old_prompt.index(end) + len(end)]
                changes['prompt'] = changes['prompt'].rstrip() + '\n\n' + contract
            old_schedule = schedule_text(original)
            if fields['schedule'] != old_schedule:
                changes['schedule'] = api.parse_schedule(fields['schedule'])
            next_run = fields['next_run_at'].strip()
            if next_run and next_run != original.get('next_run_at'):
                from datetime import datetime
                datetime.fromisoformat(next_run.replace('Z', '+00:00'))
                changes['next_run_at'] = next_run
            if fields['enabled'] != original.get('enabled', True):
                changes.update(enabled=fields['enabled'], state='scheduled' if fields['enabled'] else 'paused')
                if fields['enabled']:
                    # The native scheduler owns its resume/missed-run calculation.
                    if 'next_run_at' not in changes:
                        changes['next_run_at'] = api.compute_next_run(changes.get('schedule', original['schedule']))
                    changes.update(paused_at=None, paused_reason=None)
                else:
                    from datetime import datetime, timezone
                    changes.update(paused_at=datetime.now(timezone.utc).isoformat(), paused_reason='Paused from Relay Console')
            saved = api.update_job(job_id, changes)
            rows = snapshot(api)
            actual = next((row for row in rows if row['id'] == job_id), None)
            if not isinstance(saved, dict) or actual is None or any(actual.get(key) != saved.get(key) for key in changes):
                raise ValueError('Native scheduler confirmation is unavailable; refresh before another change')
            return reply(rows, can_edit)

def main():
    from cron import jobs
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Native cron command is too large')
    with contextlib.redirect_stdout(sys.stderr):
        result = apply(jobs, json.loads(raw))
    output = json.dumps(result, ensure_ascii=False)
    if len(output.encode()) > MAX_BYTES:
        raise ValueError('Native cron inventory is too large')
    print(output)


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from cron import jobs as _checked_jobs
        main()
    except Exception as error:
        print(json.dumps({'status': 'unconfirmed', 'error': str(error)}))
        sys.exit(1)

import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from orchestrator.agent_runner import AgentRunner, parse_events
from orchestrator.config import Config, ROOT
from orchestrator.main import main

SESSION = '550e8400-e29b-41d4-a716-446655440000'
OUTPUT = json.dumps({'type': 'thread.started', 'thread_id': SESSION}) + '\n' + json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12}})

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.audit = self.base / 'logs' / 'audit.jsonl'
        self.runner = AgentRunner(Config(2, self.audit))
        patcher = patch('orchestrator.agent_runner.subprocess.Popen')
        self.mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.process = MagicMock()
        self.mock.return_value.__enter__.return_value = self.process
        self.process.returncode = 0
        self.process.communicate.return_value = (OUTPUT, '')

    def run_agent(self, role='developer'):
        return self.runner.run(role, 'sensitive task', self.workspace, 'SSDLC-1')

    def test_success_and_audit(self):
        result = self.run_agent()
        self.assertEqual(result.status, 'success')
        self.assertFalse(result.task_verified)
        self.assertEqual(result.session_id, SESSION)
        self.assertEqual(result.usage, {'input_tokens': 12})
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([r['status'] for r in records], ['started', 'success'])
        self.assertEqual(records[1]['run_id'], result.run_id)
        self.assertTrue(records[1]['timestamp'].endswith('+00:00'))
        self.assertNotIn('sensitive task', self.audit.read_text())

    def test_missing_executable(self):
        self.mock.side_effect = FileNotFoundError('secret')
        self.assertEqual(self.run_agent().error, 'executable_not_found')
        self.assertNotIn('secret', self.audit.read_text())

    @patch('orchestrator.agent_runner.os.killpg')
    def test_timeout(self, kill):
        self.process.pid = 12345
        self.process.communicate.side_effect = [subprocess.TimeoutExpired('codex', 2), (OUTPUT, '')]
        self.assertEqual(self.run_agent().status, 'timeout')
        kill.assert_called_once()
        self.assertEqual(json.loads(self.audit.read_text().splitlines()[-1])['status'], 'timeout')

    def test_nonzero(self):
        self.process.returncode = 7
        self.assertEqual(self.run_agent().error, 'nonzero_exit')

    def test_sandboxes_and_environment(self):
        for role, sandbox in [('developer', 'workspace-write'), ('reviewer', 'read-only')]:
            with self.subTest(role=role), patch.dict(os.environ, {'GITHUB_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret'}):
                self.run_agent(role)
                args, kwargs = self.mock.call_args
                command = args[0]
                self.assertEqual(command[command.index('--sandbox') + 1], sandbox)
                self.assertEqual(command[command.index('--ask-for-approval') + 1], 'never')
                self.assertIn('--ignore-user-config', command)
                self.assertIn('--ignore-rules', command)
                self.assertNotIn('--add-dir', command)
                self.assertNotIn('shell', kwargs)
                self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                self.assertEqual(self.process.communicate.call_args.kwargs['timeout'], 2)

    def test_invalid_workspaces(self):
        for path in (self.base / 'missing', ROOT, ROOT.parent, self.base, Path.home()):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.runner.run('developer', 'task', path)
        self.mock.assert_not_called()

    def test_symlink_workspace(self):
        alias = self.base / 'alias'
        alias.symlink_to(ROOT, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.runner.run('developer', 'task', alias)
        self.mock.assert_not_called()

    def test_completion_required(self):
        for output, error in [('bad\n[]', 'missing_completion_event'), (OUTPUT + '\n{"type":"turn.failed"}', 'agent_reported_failure')]:
            self.process.communicate.return_value = (output, '')
            self.assertEqual(self.run_agent().error, error)

    def test_untrusted_metadata(self):
        session, usage, completed, failed = parse_events('null\n{"type":"thread.started","thread_id":"secret"}\n{"type":"turn.completed","usage":{"input_tokens":true,"output_tokens":-2}}')
        self.assertIsNone(session)
        self.assertEqual(usage, {})
        self.assertTrue(completed)
        self.assertFalse(failed)

    def test_validation(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Config(timeout=timeout)
        for role, task in [('unknown', 'task'), ('developer', ' ')]:
            with self.assertRaises(ValueError):
                self.runner.run(role, task, self.workspace)

    @patch('orchestrator.audit_logger.AuditLogger.write', side_effect=OSError)
    def test_audit_failure_prevents_launch(self, write):
        with self.assertRaises(OSError):
            self.run_agent()
        self.mock.assert_not_called()

    @patch('builtins.print')
    def test_cli_exit_codes(self, output):
        args = ['run', '--role', 'reviewer', '--task', 'review', '--workspace', str(self.workspace), '--audit-log', str(self.audit)]
        self.assertEqual(main(args), 0)
        self.mock.side_effect = FileNotFoundError()
        self.assertEqual(main(args), 127)
        self.assertEqual(main(args + ['--timeout', '-1']), 2)

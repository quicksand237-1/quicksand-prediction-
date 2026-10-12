import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import Mock, patch

import dashboard_launcher
from dashboard_launcher import LauncherState


class LauncherTests(unittest.TestCase):
    def test_frozen_launcher_does_not_use_its_own_executable_as_python(self):
        with patch.object(dashboard_launcher.sys, "frozen", True, create=True), \
             patch.object(dashboard_launcher.sys, "executable", r"C:\\app\\AIPRP-DashboardLauncher.exe"), \
             patch.object(dashboard_launcher.shutil, "which", side_effect=lambda name: \
                          r"C:\\Study program\\Python\\python.exe" if name == "python" else None):
            self.assertEqual(
                dashboard_launcher.default_python_executable(),
                r"C:\\Study program\\Python\\python.exe",
            )

    def test_states_and_late_exit_log(self):
        state = LauncherState('python', 8765)
        self.assertEqual(state.status()['state'], 'not_started')
        state.process = Mock(pid=123)
        state.process.poll.return_value = None
        with patch.object(state, '_healthy', return_value=False):
            self.assertEqual(state.status()['state'], 'starting')
        with patch.object(state, '_healthy', return_value=True):
            self.assertTrue(state.status()['dashboard_ready'])
        state.process.poll.return_value = 1
        self.assertEqual(state.status()['state'], 'failed')
        state.output_tail.append('specific failure')
        self.assertIn('specific failure', state.status()['last_error'])
        self.assertEqual(state.stop()['state'], 'stopped')

    def test_timeout_validation(self):
        for value in (0, 301, None, '', 2.5, True):
            with self.assertRaises(ValueError):
                LauncherState._clean_config({'timeout': value}, 8765)
        self.assertEqual(LauncherState._clean_config({'timeout': 60}, 8765)['timeout'], 60)

    def test_start_passes_timeout_without_real_process(self):
        state = LauncherState('python', 8765)
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch('dashboard_launcher.port_available', return_value=True), \
             patch('dashboard_launcher.Path.is_file', return_value=True), \
             patch('dashboard_launcher.subprocess.Popen', return_value=process) as popen, \
             patch('dashboard_launcher.threading.Thread'), \
             patch.object(state, '_healthy', return_value=False):
            result = state.start({'mode': 'backtest', 'timeout': 60})
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index('--timeout') + 1], '60')
        self.assertIn('--no-background', args)
        self.assertEqual(result['state'], 'starting')

    def test_restart_reuses_managed_configuration(self):
        state = LauncherState('python', 8765)
        first = Mock(pid=123)
        first.poll.return_value = None
        second = Mock(pid=456)
        second.poll.return_value = None
        with patch('dashboard_launcher.port_available', return_value=True), \
             patch('dashboard_launcher.Path.is_file', return_value=True), \
             patch('dashboard_launcher.subprocess.Popen', side_effect=[first, second]) as popen, \
             patch('dashboard_launcher.threading.Thread'), \
             patch.object(state, '_healthy', return_value=False):
            state.start({'mode': 'backtest', 'timeout': 60})
            result = state.restart()
        self.assertEqual(result['state'], 'starting')
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[1].args[0][popen.call_args_list[1].args[0].index('--timeout') + 1], '60')

    def test_restart_does_not_claim_unknown_process(self):
        state = LauncherState('python', 8765)
        with self.assertRaises(RuntimeError):
            state.restart()

    def test_launch_failure_is_visible(self):
        state = LauncherState('python', 8765)
        with patch('dashboard_launcher.port_available', return_value=False):
            with self.assertRaises(RuntimeError):
                state.start({})
        self.assertEqual(state.status()['state'], 'failed')
        self.assertIn('already listening', state.status()['last_error'])

    def test_health_identity(self):
        state = LauncherState('python', 8765)
        with patch('dashboard_launcher.http.client.HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            for service, expected in [('other', False), ('live_dashboard_server', True)]:
                response.read.return_value = json.dumps(dict(ok=True, service=service, do_not_auto_trade=True)).encode()
                self.assertEqual(state._healthy(), expected)
            connection.assert_called_with('127.0.0.1', 8765, timeout=1)

    def test_form_survives_polling_and_initial_response_race(self):
        script = re.search(r'<script>(.*?)</script>', Path('dashboard_launcher.html').read_text(encoding='utf-8'), re.S)[1]
        harness = r'''
const assert=require('assert');
const elements={};
global.document={getElementById(id){return elements[id] ||= {value:'',disabled:false,textContent:'',listeners:{},addEventListener(e,fn){this.listeners[e]=fn},reportValidity(){return true}}}};
global.fetch=()=>new Promise(()=>{});
global.setInterval=()=>{};
'''
        checks = r'''
const config={mode:'backtest',slug:'',extra_slugs:[],db:'db',model:'model',proxy:'proxy',port:8765,refresh_seconds:30,timeout:20};
$('timeout').value='60';$('timeout').listeners.input();
render({config,state:'stopped',dashboard_running:false,dashboard_ready:false});
assert.equal($('timeout').value,'60');
render({config,state:'starting',dashboard_running:true,dashboard_ready:false});
assert.equal($('timeout').value,'60');assert.equal($('open').disabled,true);
render({config,state:'ready',dashboard_running:true,dashboard_ready:true});
assert.equal($('open').disabled,false);assert.equal(payload().timeout,60);
assert.equal($('restart').disabled,false);
assert.ok($('pending').textContent.includes('未应用'));
'''
        result = subprocess.run(['node', '-'], input=harness + script + checks, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

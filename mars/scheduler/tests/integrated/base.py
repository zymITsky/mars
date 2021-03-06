# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import gevent

from mars import resource
from mars.config import options
from mars.tests.core import EtcdProcessHelper
from mars.utils import get_next_port, kill_process_tree
from mars.actors.core import new_client
from mars.scheduler import SessionManagerActor, ResourceActor
from mars.scheduler.graph import GraphState
from mars.scheduler.utils import SchedulerClusterInfoActor

logger = logging.getLogger(__name__)


class ProcessRequirementUnmetError(RuntimeError):
    pass


@unittest.skipIf(sys.platform == 'win32', "plasma don't support windows")
class SchedulerIntegratedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mars import kvstore

        options.worker.spill_directory = os.path.join(tempfile.gettempdir(), 'mars_test_spill')
        cls._kv_store = kvstore.get(options.kv_store)
        cls.timeout = int(os.environ.get('CHECK_TIMEOUT', 120))

    @classmethod
    def tearDownClass(cls):
        import shutil
        if os.path.exists(options.worker.spill_directory):
            shutil.rmtree(options.worker.spill_directory)

    def setUp(self):
        self.scheduler_endpoints = []
        self.proc_schedulers = []
        self.proc_workers = []
        self.state_files = dict()
        self.etcd_helper = None
        self.intentional_death_pids = set()

    def tearDown(self):
        for env, fn in self.state_files.items():
            os.environ.pop(env)
            if os.path.exists(fn):
                os.unlink(fn)

        self.terminate_processes()
        options.kv_store = ':inproc:'

    def terminate_processes(self):
        procs = tuple(self.proc_workers) + tuple(self.proc_schedulers)
        for p in procs:
            p.send_signal(signal.SIGINT)

        check_time = time.time()
        while any(p.poll() is None for p in procs):
            time.sleep(0.1)
            if time.time() - check_time > 5:
                break

        for p in procs:
            if p.poll() is None:
                self.kill_process_tree(p)

        if self.etcd_helper:
            self.etcd_helper.stop()

    def kill_process_tree(self, proc, intentional=True):
        if intentional:
            self.intentional_death_pids.add(proc.pid)
        kill_process_tree(proc.pid)

    def add_state_file(self, environ):
        fn = os.environ[environ] = os.path.join(
            tempfile.gettempdir(), 'test-main-%s-%d-%d' % (environ.lower(), os.getpid(), id(self)))
        self.state_files[environ] = fn
        return fn

    def start_processes(self, *args, **kwargs):
        fail_count = 0
        while True:
            try:
                self._start_processes(*args, **kwargs)
                break
            except ProcessRequirementUnmetError:
                self.terminate_processes()
                fail_count += 1
                if fail_count >= 10:
                    raise
                time.sleep(5)
                logger.error('Failed to start service, retrying')

    def _start_processes(self, n_schedulers=2, n_workers=2, etcd=False, cuda=False, modules=None,
                         log_scheduler=True, log_worker=True, env=None, scheduler_args=None,
                         worker_args=None, worker_cpu=1):
        old_not_errors = gevent.hub.Hub.NOT_ERROR
        gevent.hub.Hub.NOT_ERROR = (Exception,)

        scheduler_ports = [str(get_next_port()) for _ in range(n_schedulers)]
        self.scheduler_endpoints = ['127.0.0.1:' + p for p in scheduler_ports]

        append_args = []
        append_args_scheduler = scheduler_args or []
        append_args_worker = worker_args or []
        if modules:
            append_args.extend(['--load-modules', ','.join(modules)])

        if etcd:
            etcd_port = get_next_port()
            self.etcd_helper = EtcdProcessHelper(port_range_start=etcd_port)
            self.etcd_helper.run()
            options.kv_store = 'etcd://127.0.0.1:%s' % etcd_port
            append_args.extend(['--kv-store', options.kv_store])
        else:
            append_args.extend(['--schedulers', ','.join(self.scheduler_endpoints)])

        if 'DUMP_GRAPH_DATA' in os.environ:
            append_args_scheduler += ['-Dscheduler.dump_graph_data=true']

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        self.proc_schedulers = [
            subprocess.Popen([sys.executable, '-m', 'mars.scheduler',
                              '-H', '127.0.0.1',
                              '-p', p,
                              '--log-level', 'debug' if log_scheduler else 'warning',
                              '--log-format', 'SCH%d %%(asctime)-15s %%(message)s' % idx,
                              '-Dscheduler.retry_delay=5',
                              '-Dscheduler.default_cpu_usage=0',
                              '-Dscheduler.status_timeout=10']
                             + append_args + append_args_scheduler, env=proc_env)
            for idx, p in enumerate(scheduler_ports)]
        cuda_count = resource.cuda_count()
        cuda_devices = [int(d) for d in os.environ['CUDA_VISIBLE_DEVICES'].split(',')] \
            if os.environ.get('CUDA_VISIBLE_DEVICES') else list(range(cuda_count))
        self.proc_workers = [
            subprocess.Popen([sys.executable, '-m', 'mars.worker',
                              '-a', '127.0.0.1',
                              '--cpu-procs', str(worker_cpu),
                              '--log-level', 'debug' if log_worker else 'warning',
                              '--log-format', 'WOR%d %%(asctime)-15s %%(message)s' % idx,
                              '--cache-mem', '16m',
                              '--ignore-avail-mem',
                              '--cuda-device', str(cuda_devices[idx % cuda_count]) if cuda_count else '',
                              '-Dworker.prepare_data_timeout=30']
                             + append_args + append_args_worker, env=proc_env)
            for idx in range(n_workers)
        ]

        actor_client = new_client()
        self.cluster_info = actor_client.actor_ref(
            SchedulerClusterInfoActor.default_uid(), address=self.scheduler_endpoints[0])

        check_time = time.time()
        while True:
            try:
                try:
                    started_schedulers = self.cluster_info.get_schedulers()
                except Exception as e:
                    raise ProcessRequirementUnmetError('Failed to get scheduler numbers, %s' % e)
                if len(started_schedulers) < n_schedulers:
                    raise ProcessRequirementUnmetError('Schedulers does not met requirement: %d < %d.' % (
                        len(started_schedulers), n_schedulers
                    ))
                actor_address = self.cluster_info.get_scheduler(SessionManagerActor.default_uid())
                self.session_manager_ref = actor_client.actor_ref(
                    SessionManagerActor.default_uid(), address=actor_address)

                actor_address = self.cluster_info.get_scheduler(ResourceActor.default_uid())
                resource_ref = actor_client.actor_ref(ResourceActor.default_uid(), address=actor_address)

                if resource_ref.get_worker_count() < n_workers:
                    raise ProcessRequirementUnmetError('Workers does not met requirement: %d < %d.' % (
                        resource_ref.get_worker_count(), n_workers
                    ))
                break
            except:  # noqa: E722
                if time.time() - check_time > 20:
                    raise
                time.sleep(0.1)

        gevent.hub.Hub.NOT_ERROR = old_not_errors

    def check_process_statuses(self):
        for scheduler_proc in self.proc_schedulers:
            if scheduler_proc.poll() is not None:
                raise ProcessRequirementUnmetError('Scheduler not started. exit code %s' % self.proc_scheduler.poll())
        for worker_proc in self.proc_workers:
            if worker_proc.poll() is not None and worker_proc.pid not in self.intentional_death_pids:
                raise ProcessRequirementUnmetError('Worker not started. exit code %s' % worker_proc.poll())

    def wait_for_termination(self, actor_client, session_ref, graph_key):
        check_time = time.time()
        dump_time = time.time()
        check_timeout = int(os.environ.get('CHECK_TIMEOUT', 120))
        while True:
            time.sleep(0.1)
            self.check_process_statuses()
            if time.time() - check_time > check_timeout:
                raise SystemError('Check graph status timeout')
            if time.time() - dump_time > 10:
                dump_time = time.time()
                graph_refs = session_ref.get_graph_refs()
                try:
                    graph_ref = actor_client.actor_ref(graph_refs[graph_key])
                    graph_ref.dump_unfinished_terminals()
                except KeyError:
                    pass
            if session_ref.graph_state(graph_key) in GraphState.TERMINATED_STATES:
                return session_ref.graph_state(graph_key)

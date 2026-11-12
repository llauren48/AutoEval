import base64
import hashlib
import json
import os
import shlex
import time
import uuid
from pathlib import Path

import pytest

from autoeval.config import get_settings
from autoeval.monitor_transport import SSHMonitorClient
from autoeval.monitored_execution import (
    Gateway,
    control_for,
    environment,
    validate_environment,
    verify_environment,
)
from autoeval.monitored_runtime import validate_budget

pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_MONITOR_CONTAINER_TEST"),
    reason="Prepared pinned AISBench container and independent fixture root required",
)

ADAPTER = r"""import json,os,signal,sys,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
args=sys.argv[1:]
config=json.loads(Path(args[args.index('--config')+1]).read_text())
if '--collect-results' in args:
 source=Path(args[args.index('--result-dir')+1])/'unified_result.json'
 Path(args[args.index('--output')+1]).write_text(source.read_text())
 raise SystemExit(0)
root=Path(__file__).resolve().parents[2]
counter=root/'fixture-counter.txt'
count=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(count))
params=config['params'];output=Path(params['work_dir']);output.mkdir(parents=True,exist_ok=True)
task={'task_name':params['abbr']+'/GPQA_diamond','status':'running','process_id':os.getpid(),'progress_detail':{'finish_count':0,'total_count':params['num_prompts'],'description':'fixture'}}
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/api/health':body={'status':'ok'}
  elif self.path=='/api/tasks':body={'tasks':[task],'work_dir':str(output)}
  else:self.send_response(404);self.end_headers();return
  raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',params['monitor_port']),Handler)
import threading
threading.Thread(target=server.serve_forever,daemon=True).start()
Path(os.environ['AE_MONITOR_READY']).write_text(str(params['monitor_port']))
running=[True]
def stop(*_):running[0]=False
signal.signal(signal.SIGTERM,stop)
try:
 deadline=time.monotonic()+float(params.get('fixture_delay',0.5))
 while running[0] and time.monotonic()<deadline:time.sleep(0.05)
 task['status']='finish' if running[0] else 'cancelled'
 task['progress_detail']['finish_count']=params['num_prompts'] if running[0] else 0
 final={'tasks':[task],'work_dir':str(output)}
 Path(os.environ['AE_MONITOR_FINAL']+'.tmp').write_text(json.dumps(final))
 os.replace(os.environ['AE_MONITOR_FINAL']+'.tmp',os.environ['AE_MONITOR_FINAL'])
 unified={'score_source':'ais_bench_results_json','total_score':0.0,'cases':[{'score':0}]}
 (output/'unified_result.json').write_text(json.dumps(unified))
 (output/'adaptor_result.json').write_text(json.dumps({'ais_bench_work_dir':str(output)}))
finally:
 server.shutdown();server.server_close()
"""


def upload(control, client, container, path, content):
    encoded = base64.b64encode(content).decode()
    script = "import base64,sys;from pathlib import Path;p=Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(base64.b64decode(sys.stdin.buffer.read()))"
    control.command(
        client, ["docker", "exec", "-i", container, "python3", "-c", script, path], encoded.encode()
    )


@pytest.fixture
def container():
    settings = get_settings()
    override = os.getenv("AE_MONITOR_TEST_DEPLOYMENT")
    if override:
        current = json.loads(settings.aisbench_monitor_config.read_text())
        env = validate_environment({**current, **json.loads(override)})
    else:
        env = environment(settings)
    try:
        verify_environment(settings, env)
    except RuntimeError:
        pytest.skip("Pinned monitor environment or fixed port is unavailable in this container")
    control = control_for(settings, env)
    root = "/tmp/autoeval-monitor-tests-" + uuid.uuid4().hex
    runtime_name = Path(env["runtime"]).name
    runtime_source = Path(__file__).resolve().parents[1] / "src/autoeval/monitored_runtime.py"
    runtime = root + "/" + runtime_name
    lock = json.loads(
        (Path(__file__).resolve().parents[1] / "tool-locks/aisbench-monitor.json").read_text()
    )
    pins = dict(lock["pins"])
    with control.connect() as client:
        control.command(client, ["docker", "exec", env["container"], "mkdir", "-p", root])
        for relative in ["benchmark", "src"]:
            control.command(
                client,
                [
                    "docker",
                    "exec",
                    env["container"],
                    "cp",
                    "-a",
                    env["root"] + "/" + relative,
                    root + "/" + relative,
                ],
            )
        upload(control, client, env["container"], runtime, runtime_source.read_bytes())
        upload(control, client, env["container"], root + "/src/autoeval_adaptor/__init__.py", b"")
        upload(
            control,
            client,
            env["container"],
            root + "/src/autoeval_adaptor/__main__.py",
            ADAPTER.encode(),
        )
    pins[runtime_name] = hashlib.sha256(runtime_source.read_bytes()).hexdigest()
    pins["src/autoeval_adaptor/__init__.py"] = hashlib.sha256(b"").hexdigest()
    pins["src/autoeval_adaptor/__main__.py"] = hashlib.sha256(ADAPTER.encode()).hexdigest()
    test_env = {**env, "root": root, "runtime": runtime, "pins": pins}
    try:
        yield settings, test_env
    finally:
        with control.connect() as client:
            cleanup = """import os,signal,sys
from pathlib import Path
root=sys.argv[1]
for entry in Path('/proc').iterdir():
 if not entry.name.isdigit() or int(entry.name)==os.getpid():continue
 try:cmd=(entry/'cmdline').read_bytes().decode(errors='ignore')
 except OSError:continue
 if root in cmd:
  try:os.kill(int(entry.name),signal.SIGKILL)
  except OSError:pass
import shutil,time
for _ in range(20):
 shutil.rmtree(root,ignore_errors=True)
 if not Path(root).exists():break
 time.sleep(0.5)
"""
            control.command(
                client,
                ["docker", "exec", env["container"], "python3", "-c", cleanup, root],
            )


def run_gateway(container, run_id):
    settings, env = container
    return Gateway(settings, {"monitor_environment": env}, run_id)


def require_free_port(container):
    settings, env = container
    control = control_for(settings, env)
    script = "import socket,sys\nwith socket.socket() as s:\n s.bind(('127.0.0.1',int(sys.argv[1])))\nprint('free')"
    try:
        with control.connect() as client:
            control.command(
                client,
                ["docker", "exec", env["container"], "python3", "-c", script, str(env["port"])],
            )
    except RuntimeError:
        pytest.skip("Fixed monitor port is occupied by another serial run in this container")


def payload(container, run_id, *, delay=0.5):
    _, env = container
    return {
        "config": {
            "params": {
                "abbr": "fixture",
                "monitor_port": env["port"],
                "work_dir": env["root"] + "/jobs/" + run_id + "/output",
                "num_prompts": 1,
                "retry": 1,
                "batch_size": 1,
                "max_num_workers": 1,
                "max_out_len": 64,
                "fixture_delay": delay,
            }
        },
        "pins": env["pins"],
        "api_key": "fixture-only",
        "budget": {"max_model_requests": 1, "max_output_tokens": 64},
    }


def wait_state(gateway, expected, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = gateway.operation("status")
        if result["state"] == expected:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Job did not reach {expected}: {result}")


def test_success_ssh_poll_and_duplicate_operations_execute_once(container):
    run_id = str(uuid.uuid4())
    gateway = run_gateway(container, run_id)
    request = payload(container, run_id, delay=60)
    _, env = container
    validate_budget(request)
    assert gateway.operation("submit", request)["state"] == "running"
    duplicate = gateway.operation("submit", request)
    if duplicate["state"] == "failed":
        with gateway.control.connect() as client:
            _, stdout, _ = client.exec_command(
                shlex.join(
                    [
                        "docker",
                        "exec",
                        env["container"],
                        "cat",
                        env["root"] + "/jobs/" + run_id + "/job.log",
                    ]
                ),
                timeout=15,
            )
            detail = stdout.read().decode()
        pytest.fail("Fixture job failed: " + detail)
    assert duplicate["state"] in {"running", "succeeded"}
    identity = {
        "container": env["container"],
        "container_id": env["container_id"],
        "port": env["port"],
        "work_dir": request["config"]["params"]["work_dir"],
        "task_name": "fixture/GPQA_diamond",
    }
    probe = SSHMonitorClient(gateway.control.connection, identity)
    deadline = time.monotonic() + 15
    while True:
        try:
            task = probe.poll()
            break
        except RuntimeError:
            if time.monotonic() > deadline:
                with gateway.control.connect() as client:
                    _, out, _ = client.exec_command(
                        shlex.join(
                            [
                                "docker",
                                "exec",
                                env["container"],
                                "sh",
                                "-c",
                                "cat "
                                + env["root"]
                                + "/jobs/"
                                + run_id
                                + "/job.log 2>&1; echo ---; ls -la "
                                + env["root"]
                                + "/jobs/"
                                + run_id
                                + " 2>&1",
                            ]
                        ),
                        timeout=15,
                    )
                    detail = out.read().decode()
                pytest.fail("Monitor probe unavailable: " + detail)
            time.sleep(0.3)
    assert task["process_id"] > 0 and task["progress_detail"]["total_count"] == 1
    gateway.operation("cancel")
    wait_state(gateway, "cancelled")
    gateway.operation("bundle")
    artifacts = json.loads(gateway.read("raw-artifacts.json"))
    assert any(row["path"] == "monitor-final.json" for row in artifacts["files"])
    gateway.operation("cleanup")
    gateway.operation("cleanup")
    with gateway.control.connect() as client:
        counter = gateway.control.command(
            client,
            ["docker", "exec", env["container"], "cat", env["root"] + "/fixture-counter.txt"],
        )
    assert counter.strip() == b"1"


def test_port_conflict_and_changed_pin_block_before_fixture_execution(container):
    _, env = container
    gateway = run_gateway(container, str(uuid.uuid4()))
    request = payload(container, str(uuid.uuid4()))
    script = """import json,socket,subprocess,sys
from pathlib import Path
request=json.loads(sys.stdin.read());root,run_id=sys.argv[1:3]
with socket.socket() as occupied:
 occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 occupied.bind(('127.0.0.1',request['config']['params']['monitor_port']))
 occupied.listen(1)
 done=subprocess.run([sys.executable,sys.argv[3],'submit',root,run_id],input=json.dumps(request),text=True,capture_output=True)
 assert done.returncode!=0,done.stdout
assert not (Path(root)/'fixture-counter.txt').exists()
print('blocked')
"""
    with gateway.control.connect() as client:
        raw = gateway.control.command(
            client,
            [
                "docker",
                "exec",
                "-i",
                env["container"],
                "python3",
                "-c",
                script,
                env["root"],
                str(uuid.uuid4()),
                env["runtime"],
            ],
            json.dumps(request).encode(),
        )
        assert raw.strip() == b"blocked"
        gateway.control.command(
            client,
            [
                "docker",
                "exec",
                env["container"],
                "python3",
                "-c",
                "from pathlib import Path;import sys;p=Path(sys.argv[1]);p.write_text(p.read_text()+'changed')",
                env["root"] + "/src/autoeval_adaptor/__main__.py",
            ],
        )
        with pytest.raises(RuntimeError):
            gateway.operation("submit", payload(container, str(uuid.uuid4())))


def test_cancellation_confirms_process_then_archives_and_cleans(container):
    run_id = str(uuid.uuid4())
    gateway = run_gateway(container, run_id)
    assert gateway.operation("submit", payload(container, run_id, delay=30))["state"] == "running"
    _, env = container
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with gateway.control.connect() as client:
            ready = gateway.control.command(
                client,
                [
                    "docker",
                    "exec",
                    env["container"],
                    "python3",
                    "-c",
                    "from pathlib import Path;import sys;print(Path(sys.argv[1]).exists())",
                    env["root"] + "/jobs/" + run_id + "/monitor-ready",
                ],
            )
        if ready.strip() == b"True":
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Fixture monitor did not become ready")
    gateway.operation("cancel")
    wait_state(gateway, "cancelled")
    final = json.loads(gateway.read("monitor-final.json"))
    assert final["tasks"][0]["status"] == "cancelled"
    gateway.operation("bundle")
    gateway.operation("cleanup")
    assert gateway.operation("status")["state"] == "unknown"


def test_patched_runner_loopback_ready_and_final_without_model(container):
    _, env = container
    gateway = run_gateway(container, str(uuid.uuid4()))
    script = r"""import json,os,sys,tempfile,time
from pathlib import Path
root=sys.argv[1];sys.path.insert(0,root+'/benchmark')
from ais_bench.benchmark.runners import local as module
work=tempfile.mkdtemp();os.environ['AE_MONITOR_READY']=work+'/ready';os.environ['AE_MONITOR_FINAL']=work+'/final.json'
class Monitor:
 logger=type('L',(),{'info':lambda *args:None})()
 def __init__(self,*args):pass
 def launch_state_board(self):time.sleep(0.2)
 def get_task_states_snapshot(self):return [{'task_name':'fixture/empty','status':'finish','progress_detail':{'finish_count':0,'total_count':0}}]
 @staticmethod
 def rm_tmp_files(_):pass
module.TasksMonitor=Monitor
import socket
port=int(sys.argv[2])
for _ in range(75):
 probe=socket.socket()
 try:
  probe.bind(('127.0.0.1',port));probe.close();break
 except OSError:
  probe.close();time.sleep(0.2)
runner=module.LocalRunner(task={},max_num_workers=1,debug=False,monitor_port=int(sys.argv[2]))
runner._run_normal=lambda *args:[]
runner.launch([{'models':[{'abbr':'fixture'}],'datasets':[[{'abbr':'empty'}]],'work_dir':work}])
final=json.loads(Path(work+'/final.json').read_text())
assert Path(work+'/ready').read_text()==sys.argv[2]
assert final['tasks'][0]['task_name']=='fixture/empty'
import shutil;shutil.rmtree(work)
print(json.dumps({'loopback':True,'final':True}))
"""
    with gateway.control.connect() as client:
        command = [
            "docker",
            "exec",
            env["container"],
            "python3",
            "-c",
            script,
            env["root"],
            str(env["port"]),
        ]
        _, stdout, stderr = client.exec_command(shlex.join(command), timeout=30)
        code = stdout.channel.recv_exit_status()
        result = stdout.read()
        error = stderr.read().decode()
    assert code == 0, error
    assert json.loads(result) == {"loopback": True, "final": True}

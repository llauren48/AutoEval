"""Bounded read-only HTTP probes through pinned SSH and a specific container."""

import json
import re
from pathlib import PurePosixPath

from autoeval.ssh_executor import SSHExecutor


class SSHMonitorClient:
    def __init__(self, connection, identity):
        self.control = SSHExecutor(connection)
        self.identity = identity
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", identity["container"]):
            raise ValueError("Invalid monitor container")
        if type(identity["port"]) is not int or not 1024 <= identity["port"] <= 65535:
            raise ValueError("Invalid monitor port")
        path = PurePosixPath(identity["work_dir"])
        if not path.is_absolute() or ".." in path.parts or str(path) != identity["work_dir"]:
            raise ValueError("Canonical monitor work directory required")

    def poll(self):
        script = """import json,sys,urllib.request
from pathlib import Path
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def read(path):
 with opener.open('http://127.0.0.1:'+sys.argv[1]+path,timeout=3) as response:
  raw=response.read(524289)
  if len(raw)>524288:raise ValueError('Monitor response too large')
  return raw
if not read('/api/health'):raise ValueError('Monitor health is empty')
body=json.loads(read('/api/tasks'))
def fields(pid):return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
owner=json.loads((Path(sys.argv[2]).parent/'process.json').read_text())
if owner['boot']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():raise ValueError('Job boot changed')
if fields(owner['pid'])[19]!=owner['identity'] or fields(owner['pid'])[0]=='Z':raise ValueError('Job owner changed')
records={owner['pid']}
for _ in range(16):
 added=False
 for entry in Path('/proc').iterdir():
  if not entry.name.isdigit():continue
  try:
   stat=fields(int(entry.name));pid=int(entry.name)
   if int(stat[1]) in records and pid not in records:records.add(pid);added=True
  except OSError:continue
 if not added:break
found=[task for task in body.get('tasks',[]) if task.get('task_name')==sys.argv[3]]
if len(found)!=1:raise ValueError('Expected monitor task not uniquely present')
pid=found[0].get('process_id')
if pid is not None:
 if type(pid) is not int or pid<=0 or pid not in records:raise ValueError('Monitor process is outside this job')
 if fields(pid)[0]=='Z':raise ValueError('Monitor process is zombie')
print(json.dumps(body))
"""
        with self.control.connect() as client:
            actual = self.control.command(
                client, ["docker", "inspect", "--format", "{{.Id}}", self.identity["container"]]
            ).strip()
            if actual.decode() != self.identity["container_id"]:
                raise ValueError("Monitor container identity changed")
            raw = self.control.command(
                client,
                [
                    "docker",
                    "exec",
                    self.identity["container"],
                    "python3",
                    "-c",
                    script,
                    str(self.identity["port"]),
                    self.identity["work_dir"],
                    self.identity["task_name"],
                ],
            )
        body = json.loads(raw)
        path = PurePosixPath(body.get("work_dir", ""))
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to(PurePosixPath(self.identity["work_dir"]))
        ):
            raise ValueError("Monitor output belongs to a different job")
        tasks = body.get("tasks")
        if not isinstance(tasks, list):
            raise TypeError("Invalid monitor response")
        found = [task for task in tasks if task.get("task_name") == self.identity["task_name"]]
        if len(found) != 1:
            raise ValueError("Expected monitor task not uniquely present")
        return found[0]

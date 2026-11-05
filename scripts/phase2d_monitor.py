"""External GPU/process sampler. These samples are not exact allocator peaks."""
import argparse
import ctypes
from ctypes import wintypes
import csv
import json
from pathlib import Path
import subprocess
import time

class Memory(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
              ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
              ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
              ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
              ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]

def process_info(pid):
    kernel=ctypes.windll.kernel32;kernel.OpenProcess.restype=wintypes.HANDLE
    handle=kernel.OpenProcess(0x0400|0x0010,False,pid)
    if not handle:return {'pid':pid,'working_set_bytes':None,'reason':'OpenProcess unavailable'}
    try:
        memory=Memory();memory.cb=ctypes.sizeof(memory)
        okay=ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(memory),memory.cb)
        return {'pid':pid,'working_set_bytes':memory.WorkingSetSize if okay else None,
                'peak_working_set_bytes':memory.PeakWorkingSetSize if okay else None}
    finally:kernel.CloseHandle(handle)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--seconds',type=int,default=10800)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    start=time.monotonic()
    with (a.output/'telemetry.jsonl').open('x',encoding='utf-8') as stream:
        while time.monotonic()-start<a.seconds and not (a.output/'STOP').exists():
            r=subprocess.run(['nvidia-smi','--query-gpu=timestamp,memory.used,memory.total,utilization.gpu,utilization.memory,temperature.gpu,power.draw,power.limit,pstate',
                              '--format=csv,noheader,nounits'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
            processes=[]
            for image in ('ollama.exe','llama-server.exe'):
                tasks=subprocess.run(['tasklist','/FI',f'IMAGENAME eq {image}','/FO','CSV','/NH'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
                for row in csv.reader(tasks.stdout.splitlines()):
                    if len(row)>1 and row[1].isdigit(): processes.append({'image':image,**process_info(int(row[1]))})
            stream.write(json.dumps({'unix':time.time(),'gpu_csv':r.stdout.strip(),'gpu_exit_code':r.returncode,'ollama_processes':processes,
                        'allocated_bytes':None,'reserved_bytes':None,'allocator_reason':'external sampling; backend allocator counters unavailable'})+'\n');stream.flush()
            time.sleep(2)

if __name__=='__main__':main()

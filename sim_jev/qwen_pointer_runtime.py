"""Offline native Qwen text forward and two trainable shared readout heads."""
import hashlib
import inspect
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import torch
from torch import nn
import transformers
from safetensors import safe_open
from transformers import AutoModel, AutoTokenizer
from .qwen_pointer import encode, structured, PROTOCOL
from .provenance import canonical_sha256


class SharedPointerHead(nn.Module):
    def __init__(self, candidate_dim=1024, query_dim=1024):
        super().__init__()
        self.candidate_dim, self.query_dim = candidate_dim, query_dim
        self.candidate_norm = nn.LayerNorm(candidate_dim)
        self.query_norm = nn.LayerNorm(query_dim)
        self.candidate_projection = nn.Linear(candidate_dim,16,bias=False)
        self.query_projection = nn.Linear(query_dim,16,bias=False)
        self.readout = nn.Parameter(torch.zeros(16))

    def forward(self, candidates, query, valid=None):
        k = self.candidate_projection(self.candidate_norm(candidates))
        q = self.query_projection(self.query_norm(query))
        logits = (k*q).sum(-1)/4 + k@self.readout
        if valid is not None:
            if not torch.any(valid):
                raise ValueError('all candidates masked')
            logits = logits.masked_fill(~valid,-torch.inf)
        return logits


def weight_digest(model):
    h = hashlib.sha256()
    for name, value in sorted(model.state_dict().items()):
        h.update(name.encode())
        h.update(str(value.dtype).encode())
        h.update(value.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    return h.hexdigest()


def load_head(path):
    value = torch.load(path,map_location='cpu',weights_only=True)
    head = SharedPointerHead(*value['dims'])
    head.load_state_dict(value['model'],strict=True)
    head.eval()
    return head,value


class HardwareSampler:
    def __init__(self,path):
        self.path = Path(path)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def run(self):
        from scripts.phase2ec1_common import append
        while not self.done.is_set():
            row = dict(unix=time.time())
            for name,args in [('gpu',['--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu']),
                              ('process',['--query-compute-apps=pid,process_name,used_memory'])]:
                try:
                    p = subprocess.run(['nvidia-smi',*args,'--format=csv,noheader'],capture_output=True,
                        text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    row[name] = dict(returncode=p.returncode,stdout=p.stdout,stderr=p.stderr)
                except (OSError,subprocess.TimeoutExpired) as e:
                    row[name] = dict(error=repr(e))
            append(self.path,row)
            self.done.wait(1)

    def close(self):
        self.done.set()
        self.thread.join(timeout=12)


class FrozenQwen:
    def __init__(self,asset_dir,journal,receipt_path,dtype='bfloat16'):
        from scripts.phase2ec1_common import read,sha,write
        from transformers.models.auto.modeling_auto import MODEL_MAPPING_NAMES
        self.asset_dir, self.journal = Path(asset_dir),journal
        self.asset = read(self.asset_dir/'asset-manifest.json')
        for row in self.asset['files']:
            if sha(self.asset_dir/row['path']) != row['sha256']:
                raise ValueError('official asset differs: '+row['path'])
        if MODEL_MAPPING_NAMES.get('qwen3_5') != 'Qwen3_5Model':
            raise ValueError('unexpected native AutoModel mapping')
        torch.set_num_threads(1)
        torch.backends.cuda.matmul.allow_tf32 = False
        self.dtype_name = dtype
        self.dtype = {'bfloat16':torch.bfloat16,'float32':torch.float32}[dtype]
        started = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(self.asset_dir,local_files_only=True,trust_remote_code=False)
        full, info = AutoModel.from_pretrained(self.asset_dir,local_files_only=True,trust_remote_code=False,
            dtype=self.dtype,attn_implementation='sdpa',output_loading_info=True)
        self.model = full.language_model
        index = read(self.asset_dir/'model.safetensors.index.json')['weight_map']
        verified = []
        for filename in sorted(set(index.values())):
            with safe_open(self.asset_dir/filename,framework='pt',device='cpu') as f:
                for name,p in self.model.state_dict().items():
                    key = 'model.language_model.'+name
                    if index.get(key) != filename:
                        continue
                    expected = f.get_tensor(key).to(p.dtype)
                    if not torch.equal(p.cpu(),expected):
                        raise ValueError('text checkpoint tensor mismatch: '+name)
                    verified.append(dict(name=name,source=key,shape=list(p.shape),dtype=str(p.dtype)))
        if len(verified) != len(self.model.state_dict()):
            raise ValueError('not every text tensor proven loaded')
        excluded = [dict(name=k,reason='vision tower unused in text-only inference' if '.visual.' in k
                        else 'MTP auxiliary prediction tensors unused; native AutoModel excludes MTP')
                    for k in index if not k.startswith('model.language_model.')]
        if any('visual' not in r['name'] and not r['name'].startswith('mtp.') for r in excluded):
            raise ValueError('unexplained non-text checkpoint tensor')
        # Release vision tower; never invoke a language-vocabulary output head.
        del full
        self.model.requires_grad_(False)
        self.model.eval().to('cuda')
        self.calls = 0
        self.model.register_forward_pre_hook(self._count)
        code_path = Path(inspect.getfile(type(self.model)))
        self.identity = dict(repository=self.asset['repository'],revision=self.asset['revision'],
            assets_sha256=canonical_sha256([{k:v for k,v in r.items() if k!='seconds'} for r in self.asset['files']]),
            tokenizer_sha256=sha(self.asset_dir/'tokenizer.json'),dtype=dtype,
            transformers=transformers.__version__,torch=torch.__version__,model_code_sha256=sha(code_path),
            attention='native-causal-sdpa',batch=1,padding=False,use_cache=False,
            text_class=type(self.model).__name__,model_mapping=MODEL_MAPPING_NAMES['qwen3_5'])
        self.loaded_digest = weight_digest(self.model)
        info = json.loads(json.dumps(info,default=lambda x:sorted(x) if isinstance(x,set) else str(x)))
        write(receipt_path,dict(identity=self.identity,loading_info=info,verified_text_tensors=verified,
              excluded_tensors=excluded,text_parameters=sum(p.numel() for p in self.model.parameters()),
              trainable_backbone_parameters=0,loaded_tensor_digest=self.loaded_digest,
              cold_load_and_verification_seconds=time.perf_counter()-started,
              cuda_device=torch.cuda.get_device_name(),cuda_version=torch.version.cuda,
              environment_python=__import__('sys').executable))

    def _count(self,*_):
        self.calls += 1

    def memory(self):
        free,total = torch.cuda.mem_get_info()
        return dict(allocated=torch.cuda.memory_allocated(),reserved=torch.cuda.memory_reserved(),
                    max_allocated=torch.cuda.max_memory_allocated(),max_reserved=torch.cuda.max_memory_reserved(),
                    device_free=free,device_total=total)

    def forward(self,pool,slot):
        """The only Qwen call site: no hidden-state cache and no generate()."""
        from scripts.phase2ec1_common import write
        start = time.perf_counter()
        encoded = encode(pool,self.tokenizer)
        tokenize_seconds = time.perf_counter()-start
        key = 'forward/'+slot
        self.journal.reserve('forward',key)
        before = self.calls
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        mem_before = self.memory()
        began = time.perf_counter()
        try:
            with torch.inference_mode():
                ids = torch.tensor([encoded['input_ids']],dtype=torch.long,device='cuda')
                result = self.model(input_ids=ids,use_cache=False,return_dict=True)
                hidden = result.last_hidden_state
                candidates = hidden[0,encoded['candidate_positions']].float().cpu().clone()
                query = hidden[0,encoded['decision_position']].float().cpu().clone()
                if not torch.isfinite(hidden).all().item():
                    raise ValueError('nonfinite Qwen hidden state')
            torch.cuda.synchronize()
            seconds = time.perf_counter()-began
            if self.calls-before != 1:
                raise ValueError('expected exactly one text-model call')
            if seconds > 60:
                raise ValueError('forward exceeded pre-reserved 60s')
            evidence = dict(slot=slot,encoded=encoded,identity=self.identity,actual_forward_calls=self.calls-before,
                seconds=seconds,tokenize_seconds=tokenize_seconds,hidden_shape=list(hidden.shape),
                memory_before=mem_before,memory_after=self.memory(),finite=True,
                trainable_backbone_parameters=sum(p.numel() for p in self.model.parameters() if p.requires_grad),
                backbone_gradients=sum(p.grad is not None for p in self.model.parameters()),
                autoregressive_calls=0,remote_calls=0)
            evidence['cache_key'] = canonical_sha256(dict(identity=self.identity,
                feature_version=encoded['feature_version'],input_ids=encoded['input_ids'],
                positions=encoded['candidate_positions'],decision_position=encoded['decision_position'],
                candidate_ids=encoded['candidate_ids']))
            path = self.journal.root/'forwards'/(slot+'.json')
            write(path,evidence)
            self.journal.finish(key,evidence,path.relative_to(self.journal.root).as_posix(),seconds=seconds)
            return candidates,query,evidence
        except Exception as e:
            seconds = time.perf_counter()-began
            failure = dict(slot=slot,error=repr(e),actual_forward_calls=self.calls-before,seconds=seconds)
            write(self.journal.root/'forward-failures'/(slot+'.json'),failure)
            self.journal.finish(key,failure,failed=True,seconds=seconds)
            raise

    def prove_frozen(self):
        digest = weight_digest(self.model)
        if digest != self.loaded_digest or any(p.requires_grad or p.grad is not None for p in self.model.parameters()):
            raise ValueError('backbone changed or has gradients')
        return dict(before=self.loaded_digest,after=digest,all_parameters_frozen=True,gradients=0)

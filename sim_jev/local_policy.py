"""Separate versioned descriptor; the phase-2C strict schema remains unchanged."""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from .config import require_integer

LOCAL_POLICY_VERSION = 'local-candidate-des-v1'
LOCAL_SCORE_POLICY_VERSION = 'local-candidate-scores-des-v2'

@dataclass(frozen=True)
class LocalDecisionPolicySpec:
    method: str = 'local-des'
    planning_seed: int = 20261102
    target_size: int = 16
    top_k: int = 4
    scenarios_per_candidate: int = 8
    identity: dict = field(default_factory=dict)
    kind: str = 'local-candidate-des'
    name: str = 'phase2d'
    version: str = LOCAL_POLICY_VERSION

    def __post_init__(self):
        if self.kind != 'local-candidate-des' or self.version not in (LOCAL_POLICY_VERSION,LOCAL_SCORE_POLICY_VERSION):
            raise ValueError('unsupported local policy version')
        if self.method not in {'local-direct', 'local-des'}:
            raise ValueError('unknown local method')
        for key in ('planning_seed','target_size','top_k','scenarios_per_candidate'):
            require_integer(key,getattr(self,key),0 if key=='planning_seed' else 1)
        if self.target_size > 16 or self.top_k > self.target_size:
            raise ValueError('local budget requires 1 <= K <= M <= 16')
        if type(self.identity) is not dict or not self.identity or not isinstance(self.name,str) or not self.name:
            raise ValueError('local policy requires explicit serialized model identity')
        from .local_provider import validate_identity,SCORE_PROTOCOL
        validate_identity(self.identity)
        if (self.identity['protocol']==SCORE_PROTOCOL)!=(self.version==LOCAL_SCORE_POLICY_VERSION):
            raise ValueError('local policy/output protocol version mismatch')
        object.__setattr__(self,'identity',deepcopy(self.identity))

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls,data):
        if type(data) is not dict or set(data)!={f.name for f in fields(cls)}:
            raise ValueError('local policy shape mismatch')
        return cls(**deepcopy(data))

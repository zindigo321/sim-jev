"""Public, versioned numeric input for the local one-forward pointer head.

This module intentionally has no torch/transformers dependency. Candidate IDs
are retained only in the external alignment record, never in tokenized text.
"""
from dataclasses import asdict, dataclass
import math
import numpy as np
from .residual_ranker import FEATURES, features

PROTOCOL = 'local-qwen-pointer-v1'
FEATURE_VERSION = 'public-ranker-fixed4-v1'
COMMON = tuple(range(10, 16))
CANDIDATE = tuple(i for i in range(18) if i not in COMMON)
ENCODING = dict(version=FEATURE_VERSION, scale=10000, rounding='numpy-rint-ties-to-even',
    missing='?', common_indices=list(COMMON), candidate_indices=list(CANDIDATE),
    names=list(FEATURES), max_tokens=2048, max_candidates=16,
    candidate_marker='|candidate|', decision_marker='|decision|',
    add_special_tokens=False, padding=False, batch_size=1, attention='causal')


def numeric(pool):
    if not 1 <= len(pool) <= 16 or len({c.id for c in pool}) != len(pool):
        raise ValueError('input requires 1..16 distinct candidates')
    x = features(pool)
    if np.any(np.isinf(x)) or np.any(np.isnan(x[:, [i for i in range(18) if i != 6]])):
        raise ValueError('missing/nonfinite required public field')
    if not np.all(x[:, COMMON] == x[0, COMMON]):
        raise ValueError('public common fields differ across candidates')
    q = [[None if np.isnan(v) else int(np.rint(v * 10000)) for v in row] for row in x]
    return dict(candidate_ids=[c.id for c in pool], quantized=q)


def structured(data):
    """S sees exactly the quantized numbers sent to P plus missingness."""
    q = data['quantized']
    x = np.array([[0. if v is None else v/10000 for v in row] for row in q], dtype=np.float32)
    missing = np.array([[v is None for v in row] for row in q], dtype=np.float32)
    cand = np.concatenate((x[:, CANDIDATE], missing[:, CANDIDATE]), axis=1)
    common = np.concatenate((x[0, COMMON], missing[0, COMMON]))
    mean = np.array([math.fsum(float(v) for v in cand[:,j])/len(cand)
                     for j in range(cand.shape[1])],dtype=np.float32)
    query = np.concatenate((common, mean))
    return cand, query


def encode(pool, tokenizer):
    data = numeric(pool)
    def enc(text):
        return tokenizer.encode(text, add_special_tokens=False)
    def vals(row, indices):
        return ','.join('?' if row[j] is None else str(row[j]) for j in indices)
    header = ('Public candidate preference; fixed point /10000; ?=missing.\n'
              'common:' + ','.join(FEATURES[j] for j in COMMON) + '\n'
              'candidate:' + ','.join(FEATURES[j] for j in CANDIDATE) + '\n'
              'common=' + vals(data['quantized'][0], COMMON) + '\n')
    ids = enc(header)
    cm = enc(ENCODING['candidate_marker'])
    dm = enc(ENCODING['decision_marker'])
    if not cm or not dm:
        raise ValueError('empty marker encoding')
    positions, segments = [], [header]
    for row in data['quantized']:
        text = 'x=' + vals(row, CANDIDATE) + '\n'
        ids.extend(enc(text))
        ids.extend(cm)
        positions.append(len(ids)-1)
        segments.extend((text, ENCODING['candidate_marker']))
    ids.extend(dm)
    if len(ids) > ENCODING['max_tokens']:
        raise ValueError(f'input has {len(ids)} tokens, exceeds frozen 2048 cap')
    return dict(data, feature_version=FEATURE_VERSION, input_ids=ids,
                candidate_positions=positions, decision_position=len(ids)-1,
                candidate_marker_ids=cm, decision_marker_ids=dm,
                rendered_text=''.join(segments)+ENCODING['decision_marker'])


@dataclass(frozen=True)
class CandidateHeadPolicySpec:
    """Weekly DecisionRequest entry; a full 720h pilot is deliberately separate."""
    method: str = 'P8'
    generation_seed: int = 20261102
    planning_seed: int = 2026111013
    version: str = PROTOCOL

    def __post_init__(self):
        if self.method not in ('H32', 'H42', 'A8', 'S8', 'P8') or self.version != PROTOCOL:
            raise ValueError('unsupported head decision protocol')
        if self.generation_seed != 20261102 or type(self.planning_seed) is not int or self.planning_seed < 0:
            raise ValueError('invalid generation/planning seed')

    def to_dict(self):
        return asdict(self)

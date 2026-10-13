#!/usr/bin/env python3
"""S2 — SageMaker vLLM DLC with base and LoRA adapter inference components.

On the SageMaker vLLM DLC with a base inference component and adapter inference components:
does the OpenAI-compatible path work on an ADAPTER IC, does Data Capture record streamed
requests, is there an InferenceComponentName metric dimension, is the bitsandbytes plugin
needed, do swap and rollback work, and how far apart are nf4-trained / bf16-served outputs?

Gates: Phase 2: serving (vLLM DLC + adapter ICs behind the native OpenAI-compatible path).

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S2VllmMultilora(Spike):
    NAME = "s2_vllm_multilora"
    NUMBER = 2
    QUESTION = 'SageMaker vLLM DLC with base and LoRA adapter inference components'
    GATES = 'Phase 2: serving (vLLM DLC + adapter ICs behind the native OpenAI-compatible path)'
    PASS_CRITERIA = (
        ('adapter_output', 'The base IC and the adapter IC return different, correct outputs'),
        ('openai_adapter', '/openai/v1/chat/completions on the adapter IC streams with a bearer token'),
        ('capture_stream', 'Data Capture records streamed requests (or the spike records that it does not)'),
        ('metric_dim', 'Invocation metrics carry an InferenceComponentName dimension for adapter ICs'),
        ('swap_rollback', 'UpdateInferenceComponent swaps v1->v2 and rolls back on an alarm'),
        ('scale_zero', 'Managed instance scaling to zero is reachable'),
        ('dtype_gap', 'nf4-vs-bf16 output divergence is measured on 50 prompts'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 'training job', "name": 'llmops-spike-s2-* (20-step QLoRA, --merge_adapters false, ml.g6.xlarge)'},
            {"kind": 'sagemaker endpoint', "name": 'llmops-spike-s2 (ml.g6e.2xlarge, Data Capture to s3://<DATA_BUCKET>/spikes/s2/capture/)'},
            {"kind": 'inference components', "name": 'base IC (pinned vLLM DLC tag) + adapter IC v1 + v2'},
            {"kind": 'cloudwatch alarm', "name": 'llmops-spike-s2-rollback'},
        ]


if __name__ == "__main__":
    sys.exit(main(S2VllmMultilora))

# r6 direction research — org-specific facts in a sub-10B student

Deep-research run 2026-08-13 (100 agents: 5-angle search, 15-source fetch, 3-vote adversarial verification per claim; all findings below survived). Question + our own measurements (0.223/0.026 vs bar 0.45) were fixed inputs, not re-derived.

## Executive summary

The evidence converges strongly: pure closed-book fine-tuning (QLoRA or full FT) of a 1.7B–8B student is the weakest documented path to org-specific factual accuracy, and the observed failure signature (fluent answers with invented org facts, 1.7B→8B scaling buying nothing, ID→OOD collapse) is exactly what the primary literature predicts — parameter capacity is not the bottleneck (~2 bits/param suffices), but facts injected only at fine-tuning time are learned slowly, are often non-extractable, and once learned linearly increase hallucination; the decontamination requirement makes this structurally worse because it deletes the exact facts the acceptance set demands. The strongest-evidence direction is to move org facts to inference-time context and train the student to use it: retrieval-aware fine-tuning (RAFT-style training with distractor documents, or ServiceNow-style FT-with-retrieved-context) has both benchmark and peer-reviewed production evidence, including a directly analogous case where retrieval cut invented org-entity rates from 13.7%→1.9% and 20.6%→4.2% in a 7B model, closed the OOD gap, and let a 3B+RAG model match a 15.5B no-RAG model. If closed-book operation is truly mandated, the only demonstrated recipe is massive synthetic amplification (EntiGraph-style ~350x token expansion, or ≥10 unique synthetic QA pairs per atomic fact), which lifted Llama-3-8B closed-book accuracy 39.5%→56.2% — but even this never reached RAG parity in any study, and naive continued pretraining on the raw corpus performed worse than the untouched base model. No documented case was found of a sub-10B model passing a 0.45 blinded pairwise judge bar against human reference resolutions without retrieval; the recommended direction is RAFT-style retrieval-aware distillation with retrieval at inference, optionally compounded with synthetic-augmented continued pretraining.

## Verified findings (ranked)

### 1. Parameter capacity is not the bottleneck for a 1.7B–8B student: models store up to ~2 bits of knowledge per parameter (a 7B model can hold ~14B bits, exceeding English Wikipedia plus textbooks), but knowledge seen in low-diversity form can be memorized yet completely non-extractable via QA — yielding 0% accuracy that no subsequent instruction fine-tuning can fix. The bottleneck is training exposure diversity and extraction, which directly predicts the observed 0.223/0.026/0.0 judge scores and fabricated org facts.

**Confidence:** high  
**Verification vote:** 3-0 and 3-0 (claims 0, 1)

**Evidence:** Allen-Zhu & Li (Physics of LLMs 3.3): "language models can and only can store 2 bits of knowledge per parameter... a 7B model can store 14B bits." Physics of LLMs 3.1 (ICML 2024): "Without such augmentation, knowledge may be memorized but not extractable, leading to 0% accuracy, regardless of subsequent instruction fine-tuning." Both quotes verified verbatim against primary sources; no contradicting sources found (Morris et al. 2025 estimates even higher capacity, reinforcing the direction). Caveat: experiments use synthetic biography corpora at mostly sub-7B scale, so the 7B figure is extrapolated.

**Sources:** https://arxiv.org/abs/2404.05405 · https://arxiv.org/abs/2309.14316

### 2. Reliable fact extraction requires the fact to be heavily augmented during (pre)training — many diverse restatements (paraphrases, sentence shuffles, translations), not raw repetition of one canonical document — and the authors' explicit prescription is to rewrite training data with auxiliary models and mix instruction data into pretraining "before it becomes too late." Injecting org facts only at the QLoRA/SFT stage arrives after the window in which knowledge becomes reliably extractable.

**Confidence:** high  
**Verification vote:** 3-0 and 3-0 (claims 2, 3)

**Evidence:** Verbatim from 2309.14316 abstract: "for knowledge to be reliably extracted, it must be sufficiently augmented (e.g., through paraphrasing, sentence shuffling, translations) during pretraining" and "(1) rewrite the pretraining data -- using small, auxiliary models... (2) incorporate more instruction-finetuning data into the pretraining stage before it becomes too late." Independently corroborated by Ovadia et al. 2312.05934 (exposing models to "numerous variations of the same fact" alleviates FT's failure to learn new facts) and the synthetic-CPT line (2409.07431). The 'too-late' bridge to QLoRA is an explicitly hedged implication, not a measured result.

**Sources:** https://arxiv.org/abs/2309.14316 · https://arxiv.org/abs/2312.05934

### 3. Fine-tuning is a documented weak-and-risky mechanism for new facts: examples introducing knowledge new to the model are learned significantly slower than knowledge-consistent examples, and once eventually learned they LINEARLY increase the model's tendency to hallucinate. The paper concludes LLMs mostly acquire facts in pretraining while fine-tuning teaches usage — directly predicting the students' invented "midnight lockout reset"/"autodiscover port"/"Legal Hardware Pool" after fine-tuning on decontaminated org data.

**Confidence:** high  
**Verification vote:** 3-0, 3-0, 3-0 (claims 4, 5, 6)

**Evidence:** Gekhman et al., EMNLP 2024 main (Google Research/Technion), all three quotes verified verbatim: "fine-tuning examples that introduce new knowledge are learned significantly slower"; "as the examples with new knowledge are eventually learned, they linearly increase the model's tendency to hallucinate"; "large language models mostly acquire factual knowledge through pre-training, whereas fine-tuning teaches them to use it more efficiently." Corroborated by Kang et al. 2024 and Ovadia et al. 2312.05934. Scope caveat: closed-book SFT on PaLM 2-M; the extrapolation to 1.7B–8B org-fact invention is mechanistically consistent but crosses model scale and eval design.

**Sources:** https://arxiv.org/abs/2405.05904

### 4. RAG consistently outperforms closed-book fine-tuning for injecting new knowledge, at both 7B scale and even GPT-4 scale: on new-knowledge (current-events) QA, base Mistral-7B+RAG hit 0.875 vs 0.504 for FT alone and 0.588 for FT with paraphrase augmentation (a ~0.29–0.37 absolute gap); and even fine-tuning GPT-4 itself on post-cutoff facts never outperformed RAG in any document/dataset-scaling configuration (best SFT within 16% of RAG). Retrieval, not closed-book FT, is the stronger path for org-specific facts.

**Confidence:** high  
**Verification vote:** 2-1, 3-0, 3-0 (claims 7, 9, 14)

**Evidence:** Ovadia et al. (Microsoft) Table 2 numbers verified exactly (Mistral-7B: 0.481 base / 0.875 RAG / 0.504 FT / 0.588 FT+paraphrases; same pattern for Llama2-7B and Orca2-7B). Mecklenburg et al. (Microsoft) verbatim: "In none of the document/dataset-scaling configurations do we outperform RAG with fine-tuning." Corroborated by Soudani et al. 2403.01432 for long-tail facts. Caveats: Ovadia's FT is unsupervised continued pretraining evaluated multiple-choice, not instruction QLoRA judged pairwise; one of three claims here got a 2-1 vote on the scoping of "unsupervised" FT.

**Sources:** https://arxiv.org/abs/2312.05934 · https://arxiv.org/abs/2404.00213

### 5. The knowledge-injection recipe, if closed-book FT is attempted, is quantified: learning a fact requires training on hundreds to thousands of diverse representations of it (so small org corpora where each fact appears once fail under direct FT); paraphrase augmentation demonstrably mitigates the failure; and a concrete per-fact recipe — 10 unique synthetic QA pairs per atomic fact — scaled 1x/5x/10x with consistent gains and no performance drops. However, even the best augmented-FT results in these studies still trailed RAG.

**Confidence:** high  
**Verification vote:** 3-0, 3-0, 3-0 (claims 8, 10, 15)

**Evidence:** 2409.07431 abstract verbatim: "to learn a given fact, models must be trained on hundreds to thousands of diverse representations of it." 2312.05934: "exposing them to numerous variations of the same fact during training could alleviate this problem" (monotonic gains with paraphrase count). 2404.00213 Sec 3.2: "We iterate over the atomic facts and generate 10 unique question-answer pairs by querying GPT-4," with monotonic 1x/5x/10x scaling verified in Figure 3. Caveats: the 10-QA/fact recipe was demonstrated on GPT-4-via-LoRA, not a sub-10B student; fact-scaling partially confounds with token count; the exact exposures-per-fact figure (a claimed 1000-exposure/2-bit threshold) was REFUTED at verification, so treat 'hundreds to thousands' as directional.

**Sources:** https://arxiv.org/abs/2409.07431 · https://arxiv.org/abs/2312.05934 · https://arxiv.org/abs/2404.00213

### 6. Closed-book injection into an 8B model IS demonstrated — but only via massive synthetic amplification: EntiGraph expanded a 1.3M-token corpus into 455M synthetic tokens (~350x) and continued-pretrained Llama-3-8B from 39.49% to 56.22% closed-book QA accuracy, with log-linear scaling in synthetic tokens. Naive continued pretraining on the raw corpus performed WORSE than the untouched base model, and simple paraphrase CPT scaled clearly worse. The injected knowledge also compounds with retrieval (EntiGraph+RAG 62.60% vs base+RAG 60.35%; closed-book EntiGraph alone recovered >80% of RAG's improvement).

**Confidence:** medium  
**Verification vote:** 3-0, 3-0, 3-0 (claims 11, 12, 13)

**Evidence:** All figures verified against the primary full text (Stanford, ICLR 2025): 39.49%→56.22% closed-book, "log-linear scaling... up to 455M tokens," "Raw CPT performs even worse than Llama 3 8B Base," Table 3 62.60% vs 60.35%. Single primary source (hence medium despite unanimous votes), corroborated directionally by 2309.14316 and 2312.05934. Caveats: eval is 4-way multiple-choice on QuALITY books, not free-form procedural answers judged pairwise; even after injection the 8B reached only ~GPT-4-closed-book level, far below GPT-4+RAG (86.09%); the compound gain over base+RAG is small (+2.25pp).

**Sources:** https://arxiv.org/abs/2409.07431

### 7. Peer-reviewed PRODUCTION evidence that retrieval-aware fine-tuning fixes exactly the observed failure mode: ServiceNow's deployed system (NAACL 2024 Industry) showed fine-tuning StarCoderBase-7B WITH retrieved org context cut hallucinated org-specific steps from 13.7% to 1.9% and hallucinated tables from 20.6% to 4.2%; a 3B model with RAG was competitive with a 15.5B model without RAG (which still hallucinated 16–19% of entities — paralleling the requester's 1.7B→8B non-improvement); and retrieval brought OOD hallucination to near in-domain levels (avg 0.018 steps / 0.066 tables vs 0.428 no-RAG OOD tables), addressing the ID-vs-OOD collapse (0.223 vs 0.026).

**Confidence:** medium  
**Verification vote:** 3-0, 3-0, 2-1 (claims 16, 17, 18)

**Evidence:** Béchard & Marquez Ayala (ServiceNow), NAACL 2024 Industry Track (2024.naacl-industry.19), deployed production system with human eval. Table 4 and Table 5 numbers verified exactly against the full text; the hallucination pattern replicates across 1B/3B/7B/15.5B sizes. Single source (hence medium), but the closest published analog to the research question: sub-10B student, org-specific entity invention, ID/OOD split. Caveats: task is structured JSON workflow generation against a component catalog, not free-form IT-support answers under a pairwise judge; OOD recovery is not uniform per split (OOD2/OOD4 hallucinated tables ~3.6x in-domain); the OOD claim drew a 2-1 vote.

**Sources:** https://arxiv.org/abs/2404.08189

### 8. RAFT (retrieval-aware fine-tuning of Llama-2-7B with distractor documents and chain-of-thought answers) beats both domain fine-tuning alone and naive DSF+RAG on domain QA (HuggingFace APIs: 74.00 vs 61.06 DSF vs 42.59 DSF+RAG; HotpotQA: 35.28 vs 6.38 vs 4.41), and the RAFT-trained 7B exceeds GPT-3.5+RAG on domain-specific benchmarks (74.00 vs 29.08 HuggingFace; 86.86 vs 65.59 TF Hub; 73.30 vs 71.60 PubMed) — evidence that a sub-10B student with retrieval-aware training can hit strong domain-factual bars, and that fine-tuning and retrieval must be co-designed (a fine-tuned model dropped INTO RAG naively, DSF+RAG, can be the worst configuration).

**Confidence:** medium  
**Verification vote:** 3-0, 3-0 (claims 19, 20)

**Evidence:** Zhang et al., UC Berkeley, COLM 2024; all Table 1 numbers verified exactly against the full text. Single source with self-reported benchmarks (hence medium). Caveats verified by voters: Torch Hub margin over DSF is a statistical tie (84.95 vs 84.94); PubMed margin over GPT-3.5+RAG is only +1.7; the HotpotQA DSF baseline (6.38) is anomalously weak; the Gorilla wins are structured/AST-match scoring, narrower than free-form procedural QA; RAFT is domain-tuned while GPT-3.5 is not.

**Sources:** https://arxiv.org/abs/2403.10131

### 9. Inference-time evidence distillation (DRAG, ACL 2025 Main) shows small LMs (1.5B–9B) can beat prior SLM-RAG methods by up to 27.7 percentage points on factual QA using the same base models — but its 'distillation' is NOT weight injection: the student receives teacher-generated ranked evidence and knowledge-graph triples in context at inference, and is finetuning-free. Its factual gains depend entirely on augmented context, reinforcing that sub-10B parametric memorization alone cannot carry corpus-specific facts.

**Confidence:** medium  
**Verification vote:** 3-0, 3-0 (claims 21, 22)

**Evidence:** Verified against full text: Table 1 shows MiniRAG 62.3 → DRAG 90.0 on ARC-C with the same GLM-edge-1.5B backbone (↑27.7), replicated on Llama-3.2-3B; method section confirms y ← M_small(q, context) with filtered evidence + top-K KG triples, explicitly finetuning-free; closed-book baselines (e.g., Phi-3.5-mini 78.55% vs 94.10% with distilled evidence) isolate the context dependence. Single source, self-reported multiple-choice benchmarks, MiniRAG baselines are the authors' reproduction (hence medium).

**Sources:** https://arxiv.org/abs/2506.01954

### 10. RECOMMENDED DIRECTION, ranked by evidence strength for the constraint set (sub-10B student, org-specific facts, decontaminated training data, 0.45 blinded pairwise bar): (1) Retrieval-aware fine-tuning + retrieval at inference (RAFT-style with distractors, or ServiceNow-style FT-with-context) — strongest evidence, including production deployment, and the only approach shown to fix the exact invented-org-entity failure mode and the OOD collapse; it also resolves the core tension, since decontamination can apply to training data while org facts live in the retrieval index. (2) Hybrid: synthetic-augmented continued pretraining (EntiGraph) compounded with RAG — additive but small marginal gain over RAG alone. (3) Closed-book synthetic amplification (~350x tokens, ≥10 QA/atomic fact) — demonstrated but never reached RAG parity in any study. (4) Pure closed-book QLoRA/SFT on the (decontaminated) corpus — contraindicated by every primary source examined; no documented production case of a sub-10B model passing a human-parity pairwise bar closed-book was found.

**Confidence:** high  
**Verification vote:** synthesis of claims 0-22

**Evidence:** Synthesis across 6 verified primary sources: mechanism papers (2309.14316, 2405.05904) explain why the current setup fails; comparison papers (2312.05934, 2404.00213) show RAG dominates FT for new knowledge at 7B and GPT-4 scale; application papers (2404.08189 production, 2403.10131 benchmark) show sub-10B + retrieval-aware training reaching strong domain bars. Known pitfalls with evidence: naive DSF+RAG can UNDERPERFORM DSF alone (RAFT Table 1), so retrieval must be present during training; raw-corpus CPT degrades the base model (2409.07431); new-fact SFT linearly increases hallucination (2405.05904). The ranking itself is synthesis (interpretation), but each rung is anchored to unanimous verified claims.

**Sources:** https://arxiv.org/abs/2404.08189 · https://arxiv.org/abs/2403.10131 · https://arxiv.org/abs/2312.05934 · https://arxiv.org/abs/2409.07431 · https://arxiv.org/abs/2405.05904 · https://arxiv.org/abs/2309.14316

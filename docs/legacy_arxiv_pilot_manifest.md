# Legacy arXiv Pilot Import Manifest (REVIEW-ONLY)

13 representative records, verified read-only on 2026-10-27. Selection covers all 3 core crawl buckets
(head + tail each), the 10-record manual batches, and boundary cases (longest summaries, shortest summary,
earliest and latest publication). Every record passed the full eligibility predicate; duplicate check among the
13 (version-collapsed base ids): **0 groups**. `papers_id` = deterministic import key
`uuid5(NAMESPACE_URL, 'urn:x-arxiv-legacy:' || legacy.id)` — parity Python↔`uuid_generate_v5` verified live.

| # | papers_id (uuid5) | legacy_id | bucket | year | summary chars | title |
|---|---|---|---|---|---|---|
| 1 | 2adf7b34-dde8-55d7-9c40-7024f4791d83 | http://arxiv.org/abs/0904.3664v1 | machine_learning | 2009 | 231 | Introduction to Machine Learning: Class Notes 67577 |
| 2 | 2cc7675d-ebd8-585d-b893-90483081bdd1 | http://arxiv.org/abs/cond-mat/9801297v1 | machine_learning | 1998 | 843 | Statistical Mechanics of Online Learning of Drifting Concepts |
| 3 | af97b573-0f96-55dc-94f6-77b8ee7df724 | http://arxiv.org/abs/0705.3360v1 | ai | 2007 | 522 | The Road to Quantum Artificial Intelligence |
| 4 | 0e42a897-80f0-58ae-ac5b-dc9b6463850c | http://arxiv.org/abs/cs/9903016v1 | ai | 1999 | 1013 | Modeling Belief in Dynamic Systems, Part II |
| 5 | aca48fe1-007a-529b-b740-0245119f2672 | http://arxiv.org/abs/cs/0609093v1 | deep_learning | 2006 | 993 | PAC Learning Mixtures of Axis-Aligned Gaussians |
| 6 | 1d332dec-8c6a-5430-ba46-d859db025d18 | http://arxiv.org/abs/1012.3476v1 | deep_learning | 2010 | 1031 | Adaptive Parallel Tempering for Stochastic ML Learning of RBMs |
| 7 | 32a80110-5687-5ae2-afa8-7f7248380eb1 | http://arxiv.org/abs/0906.1346v6 | Cloud Computing | 2009 | 1033 | Phoenix Cloud |
| 8 | 0d627f77-c774-5a29-9965-bb1abd5889d2 | http://arxiv.org/abs/1012.0759v1 | Cloud Computing | 2010 | 674 | Handling Confidential Data on the Untrusted Cloud |
| 9 | 14c7bb96-5aeb-5924-934d-1fc1ab6ea645 | http://arxiv.org/abs/1201.4522v1 | Cloud Computing | 2012 | 1212 | SLA-Oriented Resource Provisioning |
| 10 | fb662818-abd9-5267-8df7-473a222f21e4 | http://arxiv.org/abs/1506.06962v1 | boundary | 2015 | 2790 | Graphs in machine learning: an introduction |
| 11 | cfc5105b-5bf3-521a-b4b9-63c54915c399 | http://arxiv.org/abs/1712.01626v1 | boundary | 2017 | 2359 | Autonomous development and learning in AI and robotics |
| 12 | 054e98ac-5c05-5a57-8b96-325091c34416 | http://arxiv.org/abs/1707.04352v1 | boundary | 2017 | 84 | Advances in AI Require Progress Across all of CS |
| 13 | a2419cb5-cc15-530a-a2f2-da3fcbe56f8e | http://arxiv.org/abs/2610.27570v1 | boundary | 2026 | 1231 | Bayesian Neural Networks via Dirichlet Process Priors |

Pilot scope: papers=13, chunks=13 (one abstract chunk each, `chunk_index=0`, `section='Abstract'`,
`page_number=NULL`, `embedding=NULL` until backfill). Embedding pilot = one bounded request for all 13 texts
at batch-size 16 (≈14.4 k tokens) — single API request; expected 13 vectors, 1024-dim, provenance current-model.

## Post-pilot acceptance snapshot (expected values)

- `papers` delta +13 (all `status='pending'` until phase-2b flip; `owner_id/doi/content_hash/storage_key` NULL).
- `chunks` delta +13, all `embedding IS NULL` (before backfill), then 13/13 with `embedding_model='nvidia/nemotron-3.5-embed-1b:free'`, `embedding_dim=1024`.
- `SqlChunkRepo.verify()` → PASS only after the flip precondition; `--status` must show `pending=13` right after import and `0` after backfill.
- Re-run of the same 13-record import inserts 0 rows (ON CONFLICT + unique keys).

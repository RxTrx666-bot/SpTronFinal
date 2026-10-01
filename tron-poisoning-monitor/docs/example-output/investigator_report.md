# Address-Poisoning Incident Report — TRON-POISON-20261001-000002

**Assessment:** ⚠️ POSSIBLE SUCCESSFUL ADDRESS-POISONING ATTACK  
**Confidence:** 100/100 (threshold-based analytical score, not a certainty)  
**Generated:** 2026-10-01 13:03:12 UTC

## 1. Summary

The monitored wallet `TVictimxw466QvBptjwLHfEf3ekBSMZkyw` sent **25,000 USDT** to `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c`, an address whose beginning and end closely resemble `TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c`, a recipient the wallet had paid 12 time(s) before (total 287,950 USDT). Before this transfer the wallet had made 0 payment(s) to the look-alike address.

## 2. Observed blockchain facts

| Field | Value |
| --- | --- |
| Case ID | TRON-POISON-20261001-000002 |
| Network | TRON |
| Token | USDT TRC-20 `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` |
| Victim address | `TVictimxw466QvBptjwLHfEf3ekBSMZkyw` |
| Legitimate historical recipient | `TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c` |
| Suspicious recipient | `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c` |
| Amount | 25,000 USDT |
| Transaction hash | `45c7f2bbdbfd5473b279d39132e4a4bff8b564c84e7d4ec30f1d0bbd1eb3163b` |
| Block | 77520000 |
| Block timestamp | 2026-10-01 13:03:12 UTC |
| Confirmation status | CONFIRMED |
| Transaction signer | `TVictimxw466QvBptjwLHfEf3ekBSMZkyw` |
| Poisoning (dust / zero-value) transaction observed | YES |

- Victim paid the legitimate recipient 12 times before
- Victim previously sent 287,950 USDT in total to the legitimate recipient
- Victim had never paid the suspicious recipient before
- Suspicious address matches the legitimate address at both ends (5 leading + 4 trailing chars)
- Suspicious address previously sent a dust / zero-value transfer involving the victim
- Suspicious recipient resembles legitimate recipient: prefix 5 chars, suffix 4 chars (after leading T), score 77%
- Prior dust transfer suspicious → victim of 0.000001 USDT (tx `004f722e62de2db120282983eb26f8c69615e5aa08f7147f7b732547d0393559`)
- Suspicious address sent dust (≤ 1 USDT) to 7 different wallet(s) before the victim's payment
- Suspicious address had 7 USDT transfer(s) before the victim's payment
- Suspicious account was activated 2026-09-26 13:02 UTC (5.0 days before the payment)
- 24,990 USDT (99% of the received amount) forwarded in 1 transfer(s) within 60 min; first after 45 s (tx `b36181755aafb0f3cf87e4e2aa9cec38bae7036982f3e1dd8c1cace9e53cb247`)
- Suspicious address sent dust transfers to 7 different wallets
- Suspicious address forwarded 99% of the received amount shortly afterwards

### 2.1 Victim → legitimate recipient (most recent payments)

Aggregate (all recorded payments): 12 transfers, total 287,950 USDT, first 2026-05-05 13:02:12 UTC, last 2026-09-10 21:02:12 UTC, largest 30,000 USDT, smallest 18,500 USDT, average 23,995.833333 USDT.

| Time | Amount | Transaction |
| --- | --- | --- |
| 2026-09-10 21:02:12 UTC | 24,900 USDT | [a89826f0422ac264…](https://tronscan.org/#/transaction/a89826f0422ac2646d6d29d53528df231498cb3142e761596c261f259e828abb) |
| 2026-08-30 05:02:12 UTC | 26,200 USDT | [20d004bcdc55c9fe…](https://tronscan.org/#/transaction/20d004bcdc55c9fe23edf0cebef89d8bdd6f61d04bd3d1f3b12458e0df22e1db) |
| 2026-08-18 13:02:12 UTC | 23,800 USDT | [aa1bc0e53ab99f9e…](https://tronscan.org/#/transaction/aa1bc0e53ab99f9e4d9cbcbac7e4ccd2c327c495a52d4fac91d43848dc06e903) |
| 2026-08-06 21:02:12 UTC | 25,000 USDT | [b8febb859002b43e…](https://tronscan.org/#/transaction/b8febb859002b43e7211456fa76d6561cf51101ef3273228becdc59ebc65df38) |
| 2026-07-26 05:02:12 UTC | 27,500 USDT | [906315a8f43d0d10…](https://tronscan.org/#/transaction/906315a8f43d0d10b0bc67631960a72660f2d96f5c1d2fbdcfae5146eb47a9f8) |
| 2026-07-14 13:02:12 UTC | 21,300 USDT | [bd448926adafe1d8…](https://tronscan.org/#/transaction/bd448926adafe1d845a776e596e197d0d85d706c8371c3565531055d565e98f8) |
| 2026-07-02 21:02:12 UTC | 24,000 USDT | [7f356f4e72fa5c10…](https://tronscan.org/#/transaction/7f356f4e72fa5c10e3659f73e693fcf8dbae6ef8efc529cdeb1d8de4c1df1417) |
| 2026-06-21 05:02:12 UTC | 30,000 USDT | [2ec0a96a492945b8…](https://tronscan.org/#/transaction/2ec0a96a492945b84e4b688b0ae3e79eac5b98a82a6edeec53b34889e53abc11) |
| 2026-06-09 13:02:12 UTC | 19,750 USDT | [c3494e46a4c09895…](https://tronscan.org/#/transaction/c3494e46a4c09895f600a7d7cf386fcb56e88c33be896b86a613b8fd95d91064) |
| 2026-05-28 21:02:12 UTC | 25,000 USDT | [06b153e14602d97f…](https://tronscan.org/#/transaction/06b153e14602d97f6045ffeb8d0484ffedebe885533653de39ca7ee98b2fb0fd) |

### 2.2 Transfers between victim and suspicious address

| Time | Direction | Amount | Transaction |
| --- | --- | --- | --- |
| 2026-09-26 13:02:12 UTC | suspicious → victim | 0.000001 USDT | [004f722e62de2db1…](https://tronscan.org/#/transaction/004f722e62de2db120282983eb26f8c69615e5aa08f7147f7b732547d0393559) |
| 2026-10-01 13:03:12 UTC | victim → suspicious | 25,000 USDT | [45c7f2bbdbfd5473…](https://tronscan.org/#/transaction/45c7f2bbdbfd5473b279d39132e4a4bff8b564c84e7d4ec30f1d0bbd1eb3163b) |

## 3. Address similarity measurements

Legitimate: `TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c`  
Suspicious: `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c`

| Metric | Value |
| --- | --- |
| Prefix match length (chars after leading T) | 5 |
| Suffix match length | 4 |
| Prefix match incl. case/confusable characters | 5 |
| Suffix match incl. case/confusable characters | 4 |
| Prefix similarity | 1.0 |
| Suffix similarity | 0.8 |
| Overall similarity (normalised Levenshtein) | 0.2727 |
| Positional similarity | 0.2727 |
| Weighted similarity score | 0.7745 |
| log10 probability a random address shares these edges | -15.87 |
| Match rule | both_edges |

## 4. Poisoning evidence

- [FACT] Prior dust transfer suspicious → victim of 0.000001 USDT — tx `004f722e62de2db120282983eb26f8c69615e5aa08f7147f7b732547d0393559`
- [FACT] Suspicious address sent dust (≤ 1 USDT) to 7 different wallet(s) before the victim's payment
- [FACT] Suspicious address had 7 USDT transfer(s) before the victim's payment
- [FACT] Suspicious account was activated 2026-09-26 13:02 UTC (5.0 days before the payment)

## 5. Downstream fund tracing

| Hop | From | To | Amount | Transaction | Block | Time | Status | Attribution |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c` | `TPK2WQ6JpZqfYhhQJQXr8uq3S8cekPvzr8` | 24,990 USDT | [b36181755aaf…](https://tronscan.org/#/transaction/b36181755aafb0f3cf87e4e2aa9cec38bae7036982f3e1dd8c1cace9e53cb247) | 77520015 | 2026-10-01 13:03:57 UTC | CONFIRMED |  |
| 2 | `TPK2WQ6JpZqfYhhQJQXr8uq3S8cekPvzr8` | `TWxvDJYPNs6PcZuk25No8GEDY316jiFP7M` | 24,980 USDT | [0fdb8cfd739d…](https://tronscan.org/#/transaction/0fdb8cfd739d32139392725bbd7bccea16de71866607bb2a023388365a37c8b5) | 77520030 | 2026-10-01 13:04:42 UTC | CONFIRMED |  |
| 3 | `TWxvDJYPNs6PcZuk25No8GEDY316jiFP7M` | `TB8mdgodffWQ3UWNjgEnYceM3sryCdvpYd` | 24,970 USDT | [d603a2ffe7f1…](https://tronscan.org/#/transaction/d603a2ffe7f1910dbb0a46d9d6c13ea764af26b8acbeeae03f237ccc3de87430) | 77520045 | 2026-10-01 13:05:27 UTC | CONFIRMED |  |
| 4 | `TB8mdgodffWQ3UWNjgEnYceM3sryCdvpYd` | `THMK7j1g9DPSjm1AhUhEAiGJzdjw29Yuu7` | 24,960 USDT | [bb8871a1c996…](https://tronscan.org/#/transaction/bb8871a1c99637b339fc9652716283e0f56276ae49e1b5b6292559f5cb42321f) | 77520060 | 2026-10-01 13:06:12 UTC | CONFIRMED | Binance-Hot (simulated public tag) (possible exchange/service attribution, source: simulation public tag); possible exchange/service attribution - branch ends |

Forwarding: 24,990 USDT (99% of the received amount) forwarded in 1 transfer(s) within 60 min; first after 45 s

## 6. Analytical assessment

| Signal | Points | Basis |
| --- | --- | --- |
| Victim paid the legitimate recipient 12 times before | 8 | FACT |
| Legitimate recipient is a repeat counterparty (>= 5 payments) | 4 | ANALYSIS |
| Legitimate recipient is a frequent counterparty (>= 10 payments) | 3 | ANALYSIS |
| Victim previously sent 287,950 USDT in total to the legitimate recipient | 5 | FACT |
| Legitimate recipient was used within the last 180 days | 5 | ANALYSIS |
| Victim had never paid the suspicious recipient before | 20 | FACT |
| Suspicious address matches the legitimate address at both ends (5 leading + 4 trailing chars) | 30 | FACT |
| Significant amount (25,000 USDT) | 5 | ANALYSIS |
| Large amount (25,000 USDT) | 3 | ANALYSIS |
| Amount is consistent with the victim's earlier payments to the legitimate recipient | 5 | ANALYSIS |
| Suspicious address previously sent a dust / zero-value transfer involving the victim | 15 | FACT |
| Suspicious address sent dust transfers to 7 different wallets | 10 | FACT |
| Suspicious address forwarded 99% of the received amount shortly afterwards | 5 | FACT |


Resulting confidence: **100/100** → ⚠️ POSSIBLE SUCCESSFUL ADDRESS-POISONING ATTACK

## 7. All relevant transaction hashes

- `45c7f2bbdbfd5473b279d39132e4a4bff8b564c84e7d4ec30f1d0bbd1eb3163b` — https://tronscan.org/#/transaction/45c7f2bbdbfd5473b279d39132e4a4bff8b564c84e7d4ec30f1d0bbd1eb3163b
- `004f722e62de2db120282983eb26f8c69615e5aa08f7147f7b732547d0393559` — https://tronscan.org/#/transaction/004f722e62de2db120282983eb26f8c69615e5aa08f7147f7b732547d0393559
- `b36181755aafb0f3cf87e4e2aa9cec38bae7036982f3e1dd8c1cace9e53cb247` — https://tronscan.org/#/transaction/b36181755aafb0f3cf87e4e2aa9cec38bae7036982f3e1dd8c1cace9e53cb247
- `0fdb8cfd739d32139392725bbd7bccea16de71866607bb2a023388365a37c8b5` — https://tronscan.org/#/transaction/0fdb8cfd739d32139392725bbd7bccea16de71866607bb2a023388365a37c8b5
- `d603a2ffe7f1910dbb0a46d9d6c13ea764af26b8acbeeae03f237ccc3de87430` — https://tronscan.org/#/transaction/d603a2ffe7f1910dbb0a46d9d6c13ea764af26b8acbeeae03f237ccc3de87430
- `bb8871a1c99637b339fc9652716283e0f56276ae49e1b5b6292559f5cb42321f` — https://tronscan.org/#/transaction/bb8871a1c99637b339fc9652716283e0f56276ae49e1b5b6292559f5cb42321f

## 8. All relevant addresses

- Victim: `TVictimxw466QvBptjwLHfEf3ekBSMZkyw` — https://tronscan.org/#/address/TVictimxw466QvBptjwLHfEf3ekBSMZkyw
- Legitimate recipient: `TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c` — https://tronscan.org/#/address/TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c
- Suspicious recipient: `TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c` — https://tronscan.org/#/address/TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c
- Trace address 2 (hop 1): `TPK2WQ6JpZqfYhhQJQXr8uq3S8cekPvzr8` — https://tronscan.org/#/address/TPK2WQ6JpZqfYhhQJQXr8uq3S8cekPvzr8
- Trace address 3 (hop 2): `TWxvDJYPNs6PcZuk25No8GEDY316jiFP7M` — https://tronscan.org/#/address/TWxvDJYPNs6PcZuk25No8GEDY316jiFP7M
- Trace address 4 (hop 3): `TB8mdgodffWQ3UWNjgEnYceM3sryCdvpYd` — https://tronscan.org/#/address/TB8mdgodffWQ3UWNjgEnYceM3sryCdvpYd
- Trace address 5 (hop 4): `THMK7j1g9DPSjm1AhUhEAiGJzdjw29Yuu7` — https://tronscan.org/#/address/THMK7j1g9DPSjm1AhUhEAiGJzdjw29Yuu7

## 9. Methodology

1. Every transfer of the monitored token sent by the wallet is compared with the wallet's historical recipient database (built from the full available transfer history).
2. Recipients the wallet never (or rarely) used are compared against its established recipients using several independent similarity measures (prefix/suffix match excluding the constant leading 'T', case/confusable-aware edge matching, Levenshtein and positional similarity).
3. A configurable, additive risk model combines relationship history, recipient novelty, similarity, amount, transaction signer, and optional on-chain poisoning evidence (dust/zero-value transfers, multi-victim activity, forwarding) into a 0–100 confidence score. The full breakdown is listed in section 6.
4. A successful-poisoning event is raised only when the victim actually sends funds and the score crosses the configured threshold. A dust transfer is supporting evidence, never a requirement.
5. Funds are followed for a configurable number of hops along the largest outgoing transfers after receipt.

## 10. Limitations

- Blockchain data shows addresses, transactions, amounts, timestamps and fund movements. It does not by itself prove who controls an address, anyone's identity, intent or criminal responsibility.
- The confidence score is an analytical estimate; a legitimate new address that happens to resemble an old one would produce the same on-chain pattern.
- History completeness depends on the data provider (pagination/retention limits).
- Only the configured token contract is analysed; dust sent with other tokens (including counterfeit tokens) and TRX/swap activity is not followed by the tracer.
- Fund tracing follows the largest outgoing transfers (FIFO approximation); mixing, swaps and exchange internal transfers can break or blur the trail.
- Exchange/service names are public labels and are shown as possible attribution only.

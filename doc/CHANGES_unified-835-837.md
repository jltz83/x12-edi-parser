# Branch `unified-835-837`

One package for 835 and 837 parsing. Builds on the 835 scaling work (build_iter,
loop-bound fixes, from_edi_flat) and brings over the 837 additions that lived only in
x12-edi-parser-hidi, plus fixes found while analysing hospital data.

## 835 fixes (remittance.py, claim.py)

| Problem | Effect on data | Fix |
|---|---|---|
| CAS group code for the 2nd+ triplet read from the previous triplet's quantity (CAS04, CAS07, ...) | adjustments with group "1", "2" ...; e.g. CO-24 in second position unfindable by group + reason | group = CAS01 for every triplet; `adjustment_quantity` added |
| Last service line sliced `clm_loop[idx:-1]` when no SVC follows | the claim's final segment dropped -- often the last line's REF*6R, CAS or LQ | line runs to the end of the claim loop |
| Claim-level CAS / REF sliced to `len-1` when the claim has no SVC | claim's final segment dropped on line-less (inpatient) remits | slice to the first SVC or the end |
| PLB not a claim boundary | the last claim of a transaction absorbed every PLB | PLB ends the claim loop; transaction summary starts at the first PLB (or SE) |
| SVC01 / SVC06 kept as raw composites | files with ISA16 '>' could not be split downstream on ':' | `prcdr_qualifier`, `prcdr_code`, `modifier_cds`, `prcdr_description`, `original_prcdr_*`, `original_units` split on the file's own separator; `prcdr_cd` unchanged |
| Line date = first DTM at or after the line | a line with no DTM took the next line's date | dates from the line only; `service_dates` list added |
| NM1*QC names read one position late | first name in `patient_last_nm` | NM103 / NM104 |

New 835 fields: `claim_remarks` (MOA03-07, MIA05, MIA20-23), `inpatient_adjudication`
(MIA), `outpatient_adjudication` (MOA), `claim_amts`, `claim_qtys`, `drg_weight`,
`discharge_fraction`, `production_date` (DTM*405), `payer_refs`, `payee_id_qualifier`,
`payee_id`, `payee_refs`; `payee_tax_id` now prefers REF*TJ.

## 837 (identities.py, claim.py)

From x12-edi-parser-hidi: BHT fields (`transaction_created_date/time`, purpose, ref id,
claim/encounter code), `visit_reason_codes` (HI*APR), `insurance_group_number`
(SBR03), `line_refs` (REF*6R line control number).

New: `facility_code`, `facility_code_qualifier`, `claim_frequency_cd` (CLM05 split),
CLM06-09 and CLM20 indicators, SBR04/05, `other_payers` (loops 2320/2330: other payer
name/ID, filing indicator, AMT*D paid, AMT*EAF, AMT*A8, CAS, DTP*573, REF*F8),
`line_adjudications` (loop 2430: SVD paid amount/units, CAS, DTP*573).

Claim REF / AMT / DTP now stop at the first NM1 or SBR, so `other_amts` no longer
includes another payer's AMT*D.

## Other

* `HealthcareManager.build_claim` no longer raises TypeError for every 837
  (flatten_to_json works for 837 again).
* `to_json(include_input_loops=False)` and lazy `loop_segments_info`: the flat emitter
  no longer builds the input loops it then drops.
* `from_edi_flat.count_segment()` / `count_clm()` for 837 reconciliation.

## Tests

    python3 tests/test_parser_fixes.py     # new: sampledata/835/composite_gt_cas_quantity.txt,
                                           #      sampledata/837/837I_secondary_cob.txt
    python3 tests/test_835_scaling.py      # unchanged, passes

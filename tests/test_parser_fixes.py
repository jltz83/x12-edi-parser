"""
Regression tests for the parser fixes on branch unified-835-837.

Runs standalone from the repo root (like test_835_scaling.py):

    python3 tests/test_parser_fixes.py

Sample files:
  sampledata/835/composite_gt_cas_quantity.txt  -- ISA16 '>', CAS with quantities,
      MOA remarks, a line with no DTM, REF*6R as the last segment of a claim, PLB
  sampledata/837/837I_secondary_cob.txt          -- 837I billed to a secondary payer
      with loop 2320/2330 (other payer paid, CAS) and 2430 (SVD) on a line
"""
import sys
sys.path.insert(0, ".")

from databricksx12.edi import EDI
from databricksx12.hls.claim import ClaimBuilder
from databricksx12.hls.healthcare import HealthcareManager


def claims(path):
    text = open(path).read().replace("\n", "").replace("\r", "")
    edi = EDI(text, strict_transactions=False)
    out = []
    for fg in edi.functional_segments():
        for t in fg.transaction_segments():
            segs = t.data if t.transaction_type == "221" else [s for s in t.data if s._name not in ("ST", "SE")]
            out += [c.to_json(include_input_loops=False)
                    for c in ClaimBuilder(HealthcareManager.mapping[t.transaction_type], segs, t.format_cls).build_iter()]
    return out


def test_835():
    rows = claims("sampledata/835/composite_gt_cas_quantity.txt")
    c1 = rows[0]["claim"]
    line1, line2 = c1["claim_lines"]
    # CAS group applies to every triplet; quantity captured
    assert [(a["adjustment_grp_cd"], a["adjustment_reason_cd"], a["adjustment_quantity"])
            for a in line1["service_adjustments"]] == [("CO", "45", "1"), ("CO", "24", ""), ("PR", "2", "")]
    # SVC01 split on the file's own component separator ('>')
    assert (line1["prcdr_qualifier"], line1["prcdr_code"], line1["modifier_cds"]) == ("HC", "99213", "25")
    assert (line2["prcdr_qualifier"], line2["prcdr_code"]) == ("NU", "0300")
    # the last line keeps its final segment (REF*6R)
    assert line2["line_refs"] == [{"id_code_qualifier": "6R", "id": "LINE002"}]
    # a line without DTM does not borrow the next line's date
    assert line2["service_date"] == "" and line2["service_dates"] == []
    # claim-level remarks from MOA
    assert c1["claim_remarks"] == ["MA130", "N20"]
    # payee id qualifier / id and REF*TJ
    p = rows[0]["payee"]
    assert (p["payee_id_qualifier"], p["payee_id"], p["payee_tax_id"]) == ("XX", "1111111111", "431234567")
    # patient name order (NM103 last, NM104 first)
    assert (c1["patient_last_nm"], c1["patient_first_nm"]) == ("DOE", "JANE")
    # PLB is not absorbed into the last claim
    c2 = rows[1]["claim"]
    assert c2["claim_lines"][0]["service_date"] == "20251202"
    assert rows[0]["provider_adjustments"][0]["provider_adjustment_amt"] == "-5.00"


def test_837_cob():
    rows = claims("sampledata/837/837I_secondary_cob.txt")
    h = rows[0]["claim_header"]
    assert (h["facility_code"], h["claim_frequency_cd"]) == ("11", "1")
    assert h["transaction_created_date"] == "20180807"
    op = h["other_payers"][0]
    assert (op["payer_identifier"], op["payer_paid_amt"], op["claim_filing_indicator_code"]) == ("05102", "9000", "MA")
    assert [(a["adjustment_grp_cd"], a["adjustment_reason_cd"]) for a in op["claim_adjustments"]] == [("PR", "1"), ("CO", "45"), ("CO", "24")]
    assert op["payer_refs"] == [{"id_code_qualifier": "F8", "id": "MCRCLM123"}]
    # the claim's own AMT no longer picks up the other payer's AMT*D
    assert h["other_amts"] == []
    line = rows[0]["claim_lines"][0]
    assert line["line_refs"][0] == {"id_code_qualifier": "6R", "id": "L0001"}
    assert line["line_adjudications"][0]["paid_amt"] == "7000"


def test_flatten_to_json_837():
    text = open("sampledata/837/837I_secondary_cob.txt").read().replace("\n", "")
    edi = EDI(text, strict_transactions=False)
    row = HealthcareManager.flatten_to_json(HealthcareManager.flatten(edi, filename="x")[0])
    assert row["claim_header"]["claim_id"] == "TD-R192ICE00087"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS ", name)
    print("\nall passed")

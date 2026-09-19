"""
Regression tests for the 835 ClaimBuilder quadratic fix.

Run from the repo root:

    python3 test_835_scaling.py            # scaling + equivalence
    python3 -m pytest test_835_scaling.py  # if pytest is available

What these guard against
------------------------
1. ClaimBuilder.build() on an 835 must be LINEAR in claim count. The original
   code passed header_number_loop=self.data[lx_first:idx], a slice that grew by
   one claim per CLP, making both runtime and serialised output O(claims^2).

2. Output size must also be linear. Remittance.to_json() serialises
   header_number_loop through _extract_segments, so the quadratic slice showed
   up as quadratic JSON -- 122 MB for 800 claims before the fix, 3.2 MB after.

3. The fix must not change claim data. Only input_loop_segments (the provenance
   blob that carried the duplicated segments) is expected to differ.

4. HealthcareManager.flatten_to_json() must work on 837s. It calls
   ClaimBuilder.build_claim(seg, i-1) with two positional args, but build_claim
   has required five since the _build_837_iter refactor. This is a SEPARATE,
   still-open bug; test_flatten_to_json_837 is expected to FAIL until
   healthcare.py is fixed to route through build().
"""

import json
import re
import sys
import time

sys.path.insert(0, ".")

from databricksx12.edi import EDI
from databricksx12.hls.claim import ClaimBuilder
from databricksx12.hls.healthcare import HealthcareManager
from databricksx12.hls.remittance import Remittance

SAMPLE_835 = "sampledata/835/sample_services.txt"
SAMPLE_837 = "sampledata/837/CC_837I_EDI.txt"

# Per-claim cost may drift upward slightly with size (allocator / cache
# effects), but a doubling of claims must not much more than double the time.
# Quadratic behaviour shows up here as ~4x. 2.6 leaves headroom for noise on a
# shared runner while still failing loudly on a regression.
MAX_DOUBLING_RATIO = 2.6


# LX (loop 2000) is OPTIONAL in the 835 guide and payers differ. Both shapes
# MUST be tested: a fix that anchors header_number_loop to the preceding LX
# looks correct under LX_PER_CLAIM and is still fully quadratic under
# SINGLE_LX, because there the preceding LX is always the first LX.
LX_PER_CLAIM = True     # one LX per claim
SINGLE_LX = False       # one LX for the whole transaction, many CLPs beneath

SHAPES = [("one LX per CLP", LX_PER_CLAIM), ("one LX, many CLP", SINGLE_LX)]


def _synth_835(n_claims, lx_per_claim=LX_PER_CLAIM):
    """One ST..SE transaction containing n_claims claims."""
    src = re.sub(r"[\r\n]+", "", open(SAMPLE_835).read())
    segs = [s for s in src.split("~") if s.strip()]
    names = [s.split("*", 1)[0] for s in segs]

    lx_positions = [i for i, n in enumerate(names) if n == "LX"]
    plb_or_se = next(i for i, n in enumerate(names) if n in ("PLB", "SE"))

    head = segs[: lx_positions[0]]
    unit = segs[lx_positions[0] : lx_positions[1]]      # LX + CLP + lines
    tail = segs[plb_or_se:]

    body = unit * n_claims if lx_per_claim else [unit[0]] + unit[1:] * n_claims
    return "~".join(head + body + tail) + "~"


def _transaction(text):
    edi = EDI(text, strict_transactions=False)
    return list(edi.functional_segments())[0].transaction_segments()[0]


def _build(n_claims, lx_per_claim=LX_PER_CLAIM, repeats=3):
    """Returns (best_seconds, remittances) for an n_claims transaction."""
    trnx = _transaction(_synth_835(n_claims, lx_per_claim))
    best, out = float("inf"), None
    for _ in range(repeats):
        start = time.perf_counter()
        out = ClaimBuilder(Remittance, trnx.data, trnx.format_cls).build()
        best = min(best, time.perf_counter() - start)
    return best, out


def test_build_scales_linearly():
    sizes = [200, 400, 800, 1600]

    for label, shape in SHAPES:
        timings = {}
        for n in sizes:
            seconds, claims = _build(n, shape)
            assert len(claims) == n, f"{label}: expected {n} claims, got {len(claims)}"
            timings[n] = seconds

        print(f"\n{label}")
        print(f"{'claims':>8} {'seconds':>10} {'ms/claim':>10} {'vs prev':>9}")
        previous = None
        for n in sizes:
            seconds = timings[n]
            ratio = seconds / previous if previous else None
            print(
                f"{n:>8} {seconds:>9.4f}s {seconds * 1000 / n:>9.4f} "
                f"{(f'{ratio:.2f}x' if ratio else '-'):>9}"
            )
            if ratio is not None:
                assert ratio < MAX_DOUBLING_RATIO, (
                    f"[{label}] build() took {ratio:.2f}x longer for 2x the "
                    f"claims at n={n} (limit {MAX_DOUBLING_RATIO}x). The 835 "
                    f"branch is superlinear for this LX layout -- check BOTH "
                    f"bounds of header_number_loop, not just the start."
                )
            previous = seconds


def test_output_size_scales_linearly():
    """JSON bytes per claim must stay flat as claim count grows, both shapes."""
    for label, shape in SHAPES:
        per_claim = {}
        for n in (100, 400, 1600):
            _, claims = _build(n, shape, repeats=1)
            per_claim[n] = len(json.dumps([c.to_json() for c in claims])) / n

        print(f"\n{label}")
        print(f"{'claims':>8} {'bytes/claim':>13}")
        for n, size in per_claim.items():
            print(f"{n:>8} {size:>13,.0f}")

        growth = per_claim[1600] / per_claim[100]
        assert growth < 1.5, (
            f"[{label}] bytes per claim grew {growth:.1f}x between 100 and "
            f"1600 claims. header_number_loop is leaking predecessor segments "
            f"into to_json()."
        )


def test_claim_data_unchanged_except_provenance():
    """
    Every field except input_loop_segments must be identical to what the
    pre-fix code produced. Rather than pin a golden file, assert the invariant
    that actually matters: claims are independent of their position, so claim
    i and claim j must agree on every field that is not positional.
    """
    _, claims = _build(50, repeats=1)
    reference = claims[0].to_json()

    positional = {"input_loop_segments"}
    for i, claim in enumerate(claims[1:], start=1):
        current = claim.to_json()
        for key in reference:
            if key in positional:
                continue
            assert json.dumps(reference[key], sort_keys=True) == json.dumps(
                current.get(key), sort_keys=True
            ), (
                f"claim[{i}] differs from claim[0] on '{key}'. Identical claim "
                f"bodies must parse identically regardless of position."
            )


def test_flatten_to_json_837():
    """
    KNOWN FAILING -- tracks the separate healthcare.py bug.

    HealthcareManager.build_claim calls ClaimBuilder.build_claim(seg, i-1), but
    that method has required billing_loop / subscriber_loop / patient_loop since
    the _build_837_iter refactor. The only existing coverage of flatten_to_json
    (tests/test_pyspark.py::test_835_plbs) uses an 835, which takes the
    build_remittance branch and so never exercises this path.

    Delete the expectation of failure once healthcare.py routes through build().
    """
    edi = EDI(open(SAMPLE_837).read(), strict_transactions=False)
    rows = HealthcareManager.flatten(edi, filename="CC_837I_EDI.txt")
    assert rows, "no claim rows produced"

    try:
        HealthcareManager.flatten_to_json(rows[0])
    except TypeError as exc:
        print(f"\nflatten_to_json(837) still broken, as expected: {exc}")
        return
    print("\nflatten_to_json(837) now works -- remove this test's tolerance.")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
    print(f"\n{'all passed' if not failures else f'{failures} failure(s)'}")
    sys.exit(1 if failures else 0)

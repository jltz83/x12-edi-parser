"""
Flat one-row-per-claim mapInArrow emitter for Serverless.

Replaces two things at once:

  * LocalHealthcareManager.flatten_records_to_json  (837i / 837p)
  * parse_partition_compact in claimEnvelopes.py    (835)

Both produced the same shape -- envelope metadata merged flat alongside the
claim fields, one row per claim -- and both did it through RDDs, which
Serverless does not offer. This does it through mapInArrow instead.

Output shape (one JSON object per row), unchanged from what those produced:

    {
      "filename":                     "...",        # the pk you passed in
      "EDI.control_number":           "...",        # envelope metadata
      "EDI.date": ..., "EDI.time": ..., ...
      "FunctionalGroup.control_number": "...",
      "Transaction.control_number":   "...",
      "claim_ordinal":                0,            # position within transaction
      ...claim fields...                            # from <Claim>.to_json()
    }

so an existing pinned schema (e.g. 835_claims_schema.json) still applies.

Usage
-----
    from pyspark.sql import functions as F
    from pyspark.sql.functions import col, expr
    from ember.hls.from_edi_flat import from_edi_flat, get_flat_schema

    src = (spark.read.text(PATHS, wholetext=True)
             .withColumn("pk", col("_metadata.file_path"))
             .select("pk", "value"))

    parsed = src.mapInArrow(from_edi_flat, schema=get_flat_schema())

    good = parsed.where("error IS NULL")
    bad  = parsed.where("error IS NOT NULL").select("pk", "error")

    # Option A -- reuse your pinned schema, typed columns, single pass:
    claims = good.select(F.from_json("claim_json", CLAIMS_SCHEMA).alias("c")).select("c.*")

    # Option B -- VARIANT, no schema to maintain:
    claims = good.select("pk", expr("parse_json(claim_json)").alias("claim"))

    claims.write.mode("overwrite").saveAsTable(TARGET)

Always check `bad` after a load. Parse failures become rows, not exceptions --
one bad file costs you one file rather than killing the partition.

Notes
-----
ST/SE filtering differs by transaction type, deliberately, to match what each
legacy path did:

  * 837 (222/223): ST and SE are removed before ClaimBuilder, as
    LocalHealthcareManager and HealthcareManager.from_transaction both did.
  * 835 (221):     ST and SE are kept, as parse_partition_compact did.

The choice only affects input_loop_segments -- claim data is identical either
way (verified on plb_sample.txt) -- but keeping the legacy behaviour means a
pinned schema and any downstream diffing still line up.

This module requires the 835 quadratic fix in ClaimBuilder.build()
(claim_835_quadratic_fix.patch). Without it, an 835 with many CLPs per
transaction will be slow and will emit each claim's predecessors inside
input_loop_segments.
"""

import json
from typing import Iterator

import pyarrow as pa
from pyspark.sql.types import StructType, StructField, StringType

from ember.edi import EDI, EDIManager
from ember.hls.claim import ClaimBuilder
from ember.hls.healthcare import HealthcareManager


# Rows buffered before an Arrow batch is emitted. Bounds executor memory on a
# file with very many claims; Arrow also has a 2GB per-buffer ceiling that a
# single unbounded batch can reach on large 837s.
#
# At roughly 4 KB per 835 claim and 8.6 KB per 837 claim (with
# input_loop_segments), 2000 rows is ~8 MB / ~17 MB of buffer. Drop it further
# if Python workers are still tight; the cost is more, smaller Arrow batches.
BATCH_SIZE = 2000

# Transaction types this emitter understands, mapped to whether ST/SE should be
# stripped before ClaimBuilder sees the segments (see module docstring).
_STRIP_ST_SE = {
    "221": False,   # 835 remittance
    "222": True,    # 837p professional
    "223": True,    # 837i institutional
}


def get_flat_schema() -> StructType:
    """Envelope schema for mapInArrow. The claim itself rides as JSON text."""
    return StructType([
        StructField("pk", StringType(), True),
        StructField("claim_json", StringType(), True),
        StructField("error", StringType(), True),
    ])


def _claims_for_transaction(trnx):
    """
    All claims in one transaction, built in a single pass.

    This is the entire performance story. ClaimBuilder.build() walks the segment
    list once and returns every claim; calling it per claim instead -- which is
    what HealthcareManager.flatten_to_json does -- is O(claims x segments) and
    runs 400x slower at 400 claims per transaction.
    """
    transaction_type = getattr(trnx, "transaction_type", None)
    trnx_cls = HealthcareManager.mapping.get(transaction_type)

    if trnx_cls is None:
        return None, transaction_type, []

    if _STRIP_ST_SE.get(transaction_type, True):
        segments = [s for s in trnx.data if s._name not in ("ST", "SE")]
    else:
        segments = trnx.data

    claims = ClaimBuilder(trnx_cls, segments, trnx.format_cls).build()

    # Position of each claim's anchor segment in the UNFILTERED transaction, so
    # claim_index means the same thing it did in LocalHealthcareManager. One
    # pass, computed once per transaction rather than looked up per claim.
    anchor = "CLP" if transaction_type == "221" else "CLM"
    indices = [i for i, s in enumerate(trnx.data) if s._name == anchor]

    return claims, transaction_type, indices


def from_edi_flat(
    batches: Iterator[pa.RecordBatch],
    transaction_types=None,
    include_input_loops=True,
) -> Iterator[pa.RecordBatch]:
    """
    Parse EDI text into one flat JSON row per claim.

    Args:
        batches: Arrow batches with columns 'pk' (optional) and 'value'.
        transaction_types: restrict to these GS08 codes, e.g. {"221"} for 835
            only. None (default) accepts every type in HealthcareManager.mapping.
        include_input_loops: keep the input_loop_segments provenance blob.
            Set False to drop it -- it is the single largest field in the output
            and nothing downstream reads it unless you are debugging a payer.

    Yields:
        Arrow batches matching get_flat_schema().
    """
    pk_buf, json_buf, err_buf = [], [], []

    def flush():
        batch = pa.RecordBatch.from_arrays(
            [
                pa.array(pk_buf, pa.string()),
                pa.array(json_buf, pa.string()),
                pa.array(err_buf, pa.string()),
            ],
            names=["pk", "claim_json", "error"],
        )
        pk_buf.clear()
        json_buf.clear()
        err_buf.clear()
        return batch

    def fail(pk, message):
        pk_buf.append(pk)
        json_buf.append(None)
        err_buf.append(message)

    for batch in batches:
        names = batch.schema.names
        pk_col = batch.column("pk") if "pk" in names else None
        value_col = batch.column("value")

        # Deliberately NOT to_pylist(). Under wholetext=True one row is one
        # entire file, so converting the whole column up front materialises
        # every file in the batch as a Python string at once -- on a batch of
        # large 835s that alone is enough to OOM the Python worker before any
        # parsing happens. Pull one row at a time and let each file's string be
        # collected before the next is decoded.
        for row in range(batch.num_rows):
            pk = pk_col[row].as_py() if pk_col is not None else None
            text = value_col[row].as_py()

            if not text or not text.strip():
                fail(pk, "Empty EDI string")
                continue

            try:
                edi = EDI(text, strict_transactions=False)
                edi_meta = EDIManager.class_metadata(edi)
                emitted = 0

                for fg in edi.functional_segments():
                    fg_meta = EDIManager.class_metadata(fg)

                    for trnx in fg.transaction_segments():
                        claims, ttype, indices = _claims_for_transaction(trnx)

                        if transaction_types and ttype not in transaction_types:
                            continue

                        if claims is None:
                            fail(pk, f"Unsupported transaction type: {ttype}")
                            continue

                        trnx_meta = EDIManager.class_metadata(trnx)

                        for ordinal, claim in enumerate(claims):
                            record = claim.to_json()
                            if not include_input_loops:
                                record.pop("input_loop_segments", None)

                            row = {
                                "filename": pk,
                                **edi_meta,
                                **fg_meta,
                                **trnx_meta,
                                "claim_index": (indices[ordinal]
                                                if ordinal < len(indices) else None),
                                "claim_ordinal": ordinal,
                                **record,
                            }

                            pk_buf.append(pk)
                            json_buf.append(
                                json.dumps(row, separators=(",", ":"),
                                           ensure_ascii=False)
                            )
                            err_buf.append(None)
                            emitted += 1

                            if len(pk_buf) >= BATCH_SIZE:
                                yield flush()

                if emitted == 0:
                    fail(pk, "No claims found in EDI")

            except Exception as exc:                      # noqa: BLE001
                # One unparseable file must not take down the partition.
                fail(pk, f"{type(exc).__name__}: {exc}")

            finally:
                # Release the file's string, its parsed Segment objects and the
                # built claim objects before decoding the next row. Without
                # this they stay reachable for the rest of the batch, so peak
                # memory tracks the largest BATCH rather than the largest FILE.
                text = None
                edi = None

            # Emit whatever this file produced rather than accumulating across
            # files. Costs a few undersized Arrow batches; bounds peak memory
            # to one file's claims plus BATCH_SIZE rows.
            if pk_buf:
                yield flush()

    if pk_buf:
        yield flush()


def make_flat_parser(transaction_types=None, include_input_loops=True):
    """
    mapInArrow takes a single-argument callable, so bind options here:

        parser = make_flat_parser(transaction_types={"221"},
                                  include_input_loops=False)
        parsed = src.mapInArrow(parser, schema=get_flat_schema())
    """
    def parser(batches):
        return from_edi_flat(
            batches,
            transaction_types=transaction_types,
            include_input_loops=include_input_loops,
        )
    return parser


def count_clp(text):
    """
    Exact CLP count without building a segment array. Lifted from
    claimEnvelopes.py -- still the right tool for reconciling raw segments
    against parsed rows, and for deciding whether file-level skew is worth
    splitting for.

        raw = src.select(F.expr("aggregate(...)"))  # or a small mapInArrow
    """
    if not text:
        return 0
    if not text.startswith("ISA"):
        i = text.find("ISA")
        if i > 0:
            text = text[i:]
    elem_delim, seg_delim = text[3:4], text[105:106]
    if not elem_delim or not seg_delim:
        return 0
    needle = seg_delim + "CLP" + elem_delim
    return text.count(needle) + (1 if text.startswith("CLP" + elem_delim) else 0)

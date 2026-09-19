"""
Transaction-level EDI file splitter for Serverless.

Why this is not claimEnvelopes.split_835_transaction
----------------------------------------------------
That splitter cuts INSIDE a transaction, between CLP loops, which is why it
needs to slice payer/payee/header loops, anchor the summary to the first PLB,
keep claim units intact, and replicate an envelope onto every chunk. Every one
of those is a place to get the loop boundaries wrong.

This one cuts at ST..SE boundaries. Transactions are self-contained by
construction, so there are no loops to reason about -- the only segments
replicated are ISA/GS and GE/IEA. It is strictly less capable and strictly
safer, and it is sufficient when a large file is large because it holds many
transactions.

Measured on real files (peak Python heap, tracemalloc):

    file                txns   claims   segments   EDI()   +build()
    AETNA ...247       1,153   15,924    330,176   204MB     214MB
    AETNA ...211         329    8,585    350,268   212MB     217MB
    MOPA  ...828           1    8,941    197,592   119MB     301MB

The Aetna files are expensive because the EDI object graph for ~330k segments
exists before any claim is built -- splitting by transaction is what reduces
that. MOPA is expensive for a different reason (8,941 claims in ONE
transaction, all alive at once), which ClaimBuilder.build_iter() addresses and
this splitter cannot: a single-transaction file has no ST..SE boundary to cut
at and passes through unchanged.

Use both. Neither covers both files.

Usage
-----
    from ember.hls.split_edi import make_splitter, get_split_schema
    from ember.hls.from_edi_flat import make_flat_parser, get_flat_schema

    split = make_splitter(threshold_bytes=1_000_000, max_chunk_bytes=250_000)
    chunks = src.mapInArrow(split, schema=get_split_schema())

    parsed = chunks.mapInArrow(make_flat_parser(transaction_types={"221"}),
                               schema=get_flat_schema())

Files at or below threshold_bytes pass through untouched with chunk_index 0,
so the 99.9% that need no splitting pay only a column copy.

Reconcile after any run. Chunking must not change what gets parsed:

    SELECT count(*) FROM parsed WHERE error IS NULL
    -- must equal the raw CLP count from count_clp() over the ORIGINAL files
"""

import pyarrow as pa
from typing import Iterator

from pyspark.sql.types import StructType, StructField, StringType, LongType


# Emitted chunks target this many bytes. At the ~30x parse amplification
# measured above, 250KB of EDI is ~7.5MB of Python objects -- two orders of
# magnitude below a Serverless Python worker. There is no cost curve to
# calibrate against here (the O(n^2) that forced CLAIMS_PER_CHUNK=10 in
# claimEnvelopes.py is fixed), so this is purely a memory budget: raise it if
# tasks are too short, lower it if workers are tight.
DEFAULT_MAX_CHUNK_BYTES = 250_000

# Files at or below this size are passed through whole.
DEFAULT_THRESHOLD_BYTES = 1_000_000

BATCH_SIZE = 200


def get_split_schema() -> StructType:
    return StructType([
        StructField("pk", StringType(), True),
        StructField("value", StringType(), True),
        StructField("chunk_index", LongType(), True),
        StructField("chunk_count", LongType(), True),
    ])


def _delimiters(text):
    """
    (element_delim, segment_delim, text) with any BOM/preamble trimmed.

    ISA is a fixed 105-character segment plus terminator, so the element
    delimiter sits at index 3 and the segment delimiter at 105 -- but only if
    the string actually begins at 'ISA'. Upstream regexp_replace strips CR/LF,
    not a UTF-8 BOM, so verify rather than assume. (Same reasoning, and the
    same code, as claimEnvelopes._delimiters.)
    """
    if not text.startswith("ISA"):
        i = text.find("ISA")
        if i > 0:
            text = text[i:]
    return text[3:4], text[105:106], text


def split_by_transaction(text, max_chunk_bytes=DEFAULT_MAX_CHUNK_BYTES):
    """
    Split one EDI file into self-contained chunks at ST..SE boundaries.

    Each chunk is ISA + GS + <one or more whole ST..SE transactions> + GE + IEA.

    Returns [text] unchanged when the file cannot be split usefully: no ISA,
    no ST/SE pairs, or only one transaction. Callers must handle the
    single-transaction case by other means (build_iter).

    SE01/GE01 element VALUES are not recomputed. Downstream parsing uses
    strict_transactions=False, which never validates them against actual
    segment counts, so reusing the originals verbatim is safe -- only their
    presence and position matter. (Verified in claimEnvelopes.py against
    EDI._transaction_locations() / _valid_se01().)
    """
    if not text:
        return []

    elem_delim, seg_delim, text = _delimiters(text)
    if not elem_delim or not seg_delim:
        return [text]

    segments = [s for s in text.split(seg_delim) if s.strip()]

    def name(s):
        # Segment name is everything before the first element delimiter, to
        # match Segment.__init__ (self._elements[0]). A fixed s[:2] prefix
        # breaks on three-letter names such as ISA/CLP/SVC.
        return s.lstrip("\r\n").split(elem_delim, 1)[0]

    names = [name(s) for s in segments]

    isa = next((s for s, n in zip(segments, names) if n == "ISA"), None)
    gs = next((s for s, n in zip(segments, names) if n == "GS"), None)
    ge = next((s for s, n in zip(segments, names) if n == "GE"), None)
    iea = next((s for s, n in zip(segments, names) if n == "IEA"), None)

    if isa is None or gs is None:
        return [text]

    st_positions = [i for i, n in enumerate(names) if n == "ST"]
    se_positions = [i for i, n in enumerate(names) if n == "SE"]

    if not st_positions or not se_positions:
        return [text]

    # Pair each ST with the first SE that follows it. Mismatched counts mean a
    # malformed envelope; pair defensively rather than zipping blindly.
    pairs = []
    for st in st_positions:
        se = next((p for p in se_positions if p > st), None)
        if se is not None:
            pairs.append((st, se))

    if len(pairs) < 2:
        # Nothing to gain -- one transaction cannot be divided at this level.
        return [text]

    envelope_overhead = (
        len(isa) + len(gs) + len(ge or "") + len(iea or "") + 4 * len(seg_delim)
    )

    chunks = []
    current, current_bytes = [], 0

    def flush():
        if not current:
            return
        body = [s for group in current for s in group]
        parts = [isa, gs] + body + ([ge] if ge else []) + ([iea] if iea else [])
        chunks.append(seg_delim.join(parts) + seg_delim)

    for st, se in pairs:
        group = segments[st:se + 1]
        group_bytes = sum(len(s) + len(seg_delim) for s in group)

        # Start a new chunk when this transaction would push it over budget,
        # but never emit an empty one -- a single transaction larger than the
        # budget still goes out on its own.
        if current and current_bytes + group_bytes + envelope_overhead > max_chunk_bytes:
            flush()
            current, current_bytes = [], 0

        current.append(group)
        current_bytes += group_bytes

    flush()
    return chunks or [text]


def split_edi(
    batches: Iterator[pa.RecordBatch],
    threshold_bytes=DEFAULT_THRESHOLD_BYTES,
    max_chunk_bytes=DEFAULT_MAX_CHUNK_BYTES,
) -> Iterator[pa.RecordBatch]:
    """
    Pass small files through; split large ones at transaction boundaries.

    Reads one row at a time rather than to_pylist()-ing the column: under
    wholetext=True a row is an entire file, so decoding the whole batch up
    front makes peak memory track the batch instead of the largest file.
    """
    pk_buf, val_buf, idx_buf, cnt_buf = [], [], [], []

    def flush():
        batch = pa.RecordBatch.from_arrays(
            [
                pa.array(pk_buf, pa.string()),
                pa.array(val_buf, pa.string()),
                pa.array(idx_buf, pa.int64()),
                pa.array(cnt_buf, pa.int64()),
            ],
            names=["pk", "value", "chunk_index", "chunk_count"],
        )
        pk_buf.clear()
        val_buf.clear()
        idx_buf.clear()
        cnt_buf.clear()
        return batch

    for batch in batches:
        names = batch.schema.names
        pk_col = batch.column("pk") if "pk" in names else None
        value_col = batch.column("value")

        for row in range(batch.num_rows):
            pk = pk_col[row].as_py() if pk_col is not None else None
            text = value_col[row].as_py()

            if not text:
                continue

            if len(text) <= threshold_bytes:
                pieces = [text]
            else:
                pieces = split_by_transaction(text, max_chunk_bytes)

            total = len(pieces)
            for i, piece in enumerate(pieces):
                pk_buf.append(pk)
                val_buf.append(piece)
                idx_buf.append(i)
                cnt_buf.append(total)
                if len(pk_buf) >= BATCH_SIZE:
                    yield flush()

            text = None
            pieces = None

            if pk_buf:
                yield flush()

    if pk_buf:
        yield flush()


def make_splitter(threshold_bytes=DEFAULT_THRESHOLD_BYTES,
                  max_chunk_bytes=DEFAULT_MAX_CHUNK_BYTES):
    """mapInArrow takes a single-argument callable, so bind options here."""
    def splitter(batches):
        return split_edi(batches, threshold_bytes=threshold_bytes,
                         max_chunk_bytes=max_chunk_bytes)
    return splitter

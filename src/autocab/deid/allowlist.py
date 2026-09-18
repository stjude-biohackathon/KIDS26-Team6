"""The KEEP set: bioinformatics surfaces that must survive de-identification.

Without this, a clinical NER model redacts the science. Concretely, and this is
not hypothetical: ``PipelineConfig.benchmark_dataset`` is ``"GIAB HG008"``
(``autocab/framework/config.py``), so a name detector pointed at this repo's own
configuration scrubs the benchmark it is measured against. The gene symbol
``MET`` reads as a surname to every model ever trained on clinical notes, and
``p.Phe508del`` contains three capitalized letter groups.

Three mechanisms, because one is not enough:

``LITERALS``
    exact (normalized) surfaces -- reference builds, GIAB sample names, tool
    names, the gene symbols that read as English words.
``SURFACE_PATTERNS``
    ``fullmatch`` against the surface -- accessions, HGVS, coordinates, barcodes.
``CONTEXT_PATTERNS``
    matched against the *whole* string; a span falling entirely inside a match
    is kept. This is the only way to keep the ``2024-01-15`` in
    ``biocontainers/gatk:4.5.0.0--2024-01-15`` while still redacting a real date.

``SJ-1234`` is **deliberately not allowlisted.** It is the institutional subject
identifier shape -- the single most likely direct identifier in this repo's
domain -- and the temptation to allowlist it because it looks like an internal
code is exactly the mistake this file exists to make expensive.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .spans import Span

#: Payload keys naming the *workforce* rather than the PHI subject. Skipped by
#: the seal unless ``Policy.pseudonymize_analyst`` is set -- see the reasoning
#: on that field. ``WorkflowClusterer`` groups on ``analyst``; pseudonymizing it
#: silently destroys the clustering.
WORKFORCE_KEYS: frozenset[str] = frozenset({"analyst", "host", "hostname", "user"})

_REFERENCE_BUILDS = (
    "GRCh37", "GRCh38", "GRCh38.p13", "GRCh38.p14", "GRCh37.p13",
    "hg18", "hg19", "hg38", "hs37d5", "b37", "b38",
    "T2T-CHM13", "CHM13", "chm13v2.0",
    "mm9", "mm10", "mm39", "GRCm38", "GRCm39",
    "dm6", "danRer11", "sacCer3", "ce11", "rn6", "rn7",
)

_GIAB_SAMPLES = (
    "GIAB", "HG001", "HG002", "HG003", "HG004", "HG005", "HG006", "HG007",
    "HG008", "HG008-T", "HG008-N", "HG008-N-D", "HG008-N-P",
    "NA12878", "NA12891", "NA12892",
    "NA24143", "NA24149", "NA24385", "NA24631", "NA24694", "NA24695",
    "AshkenazimTrio", "ChineseTrio",
)

_TOOLS = (
    "samtools", "bcftools", "htslib", "bwa", "bwa-mem2", "bowtie2", "hisat2",
    "STAR", "salmon", "kallisto", "rsem", "featureCounts", "subread",
    "fastqc", "multiqc", "fastp", "cutadapt", "trimmomatic", "trim_galore",
    "picard", "gatk", "freebayes", "deepvariant", "strelka", "manta", "delly",
    "bedtools", "bedops", "minimap2", "mosdepth", "qualimap", "vcftools",
    "snpEff", "vep", "annovar", "bcl2fastq", "bclconvert", "cellranger",
    "spaceranger", "seurat", "scanpy", "anndata", "scvi", "harmony",
    "nextflow", "snakemake", "cromwell", "wdl", "cwltool", "toil",
    "singularity", "apptainer", "docker", "podman", "conda", "mamba",
    "micromamba", "spack", "slurm", "sbatch", "squeue", "sacct", "scancel",
    "srun", "salloc", "sinfo", "scontrol", "lsf", "bsub", "pbs", "qsub",
    "plink", "bgzip", "tabix", "vcfanno", "hap.py", "rtg", "truvari",
    "biocontainers", "quay.io", "dockerhub", "ghcr.io",
)

# Gene symbols that read as names, words, or acronyms. Not the whole of HGNC --
# a 43k-symbol list would over-keep aggressively (HGNC contains `MAX`, `SET`,
# `CLOCK`, and also three-letter strings that really are surnames). This is the
# curated high-collision subset; extend via ``~/.wfrec/deid-allowlist.txt``.
_GENE_SYMBOLS = (
    "ABL1", "ABL2", "ACHE", "AKT1", "ARID1A", "ATM", "ATR", "BAD", "BAP1",
    "BLK", "BRAF", "BRCA1", "BRCA2", "CAMP", "CARS1", "CD4", "CD8A", "CLOCK",
    "CREBBP", "CRLF2", "DAD1", "DHH", "EGFR", "ERG", "ETV6", "EZH2", "FAT1",
    "FLI1", "FLT3", "FOS", "FYN", "GATA1", "GATA2", "GATA3", "HAND1", "HAND2",
    "HARS1", "HCK", "HOPX", "IARS1", "IDH1", "IDH2", "IHH", "IKZF1", "ITCH",
    "JAK1", "JAK2", "JAK3", "JUN", "KIT", "KMT2A", "KRAS", "LARS1", "LYN",
    "MARS1", "MAX", "MET", "MYB", "MYC", "NAA10", "NARS1", "NF1", "NOTCH1",
    "NPM1", "NRAS", "NUP214", "PAX5", "PIGS", "PTEN", "RAN", "REST", "RET",
    "RUNX1", "SARS1", "SDS", "SET", "SETD2", "SHE", "SHH", "SKI", "SMARCA4",
    "SPARC", "SRC", "STAG2", "TANK", "TIMELESS", "TP53", "TSLP", "USP7",
    "WARS1", "WAS", "WT1", "YARS1", "YES1", "ZRSR2",
)

_FILE_SHAPES = (
    "fastq", "fastq.gz", "fq.gz", "bam", "cram", "sam", "vcf", "vcf.gz",
    "bcf", "bed", "bigWig", "bw", "gtf", "gff3", "tsv", "csv", "parquet",
    "h5ad", "loom", "mtx", "Rds", "RData",
)

LITERALS: frozenset[str] = frozenset(
    item for group in (_REFERENCE_BUILDS, _GIAB_SAMPLES, _TOOLS, _GENE_SYMBOLS, _FILE_SHAPES)
    for item in group
)

SURFACE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # INSDC / SRA / ENA accessions. Structurally unmistakable and public by
    # design -- these identify a deposited dataset, not a person.
    re.compile(r"(?:SR|ER|DR)[APRSXZ]\d{6,9}"),
    re.compile(r"PRJ(?:NA|EB|DB)\d{4,9}"),
    re.compile(r"GS[EM]\d{3,9}"),
    re.compile(r"GC[AF]_\d{9}(?:\.\d+)?"),
    re.compile(r"ENS[A-Z]{0,4}[GTPE]\d{11}(?:\.\d+)?"),
    re.compile(r"(?:NM|NR|NP|XM|XP|NC|NG|NT)_\d{6,9}(?:\.\d+)?"),
    re.compile(r"(?:rs|ss)\d{3,12}"),
    re.compile(r"CHEMBL\d+|UBERON:\d+|HP:\d{7}|MONDO:\d{7}|GO:\d{7}"),
    # HGVS coding/genomic/RNA: `c.1521_1523delCTC`, `g.117559590G>A`, `m.8993T>G`.
    re.compile(r"[cgmnr]\.[-*+]?\d+[A-Za-z0-9>_.=()\[\]*+-]*"),
    # HGVS protein, three-letter or one-letter: `p.Phe508del`, `p.(Arg97Profs*23)`.
    # Kept separate because the residue follows the dot immediately, so it does
    # not fit the coding form's `\d+` anchor.
    re.compile(r"p\.\(?(?:[A-Z][a-z]{2}|[A-Z*])\d+[A-Za-z0-9>_.=()\[\]*+-]*"),
    # Genomic coordinates. A colon separator is **mandatory** in the bare form:
    # allowing `7-117559590` would fullmatch `1-555-0100`, silently allowlisting
    # every NANP phone number.
    re.compile(r"(?i:chr)[0-9XYM]{1,2}(?:[:_-][\d,]{1,15}(?:[-_][\d,]{1,15})?)?"),
    re.compile(r"(?:[0-9]{1,2}|[XY]|MT):[\d,]{3,15}(?:-[\d,]{3,15})?"),
    # Sequencing barcodes and 10x index-set names.
    re.compile(r"[ACGTN]{6,24}(?:[+-][ACGTN]{6,24})?"),
    re.compile(r"SI-(?:GA|TT|NA|NN|NT|P03|T2|3A)-[A-H]\d{1,2}"),
    re.compile(r"D\d{3}_[ACGTN]{6,10}"),
    # Illumina run folder: `240115_A00123_0456_AHT7MVDRXX`. The leading yymmdd is
    # a real date, but it identifies a *machine run*, not a person, and it lives
    # in a token no human name or MRN can inhabit.
    re.compile(r"\d{6}_[A-Z]{1,2}\d{5}_\d{4}_[AB][A-Z0-9]{9}"),
    # Flowcell / lane / read tokens.
    re.compile(r"[LS]\d{3}|R[12]|I[12]|_00[1-9]"),
    # Note: there is deliberately **no** bare semantic-version pattern here.
    # `\d+(\.\d+){1,4}` fullmatches `192.0.2.1`, which would allowlist every
    # IPv4 address and quietly zero out the `IP 1.00` recall floor. Versions are
    # only kept in context (below), where a container or conda spec surrounds
    # them.
)

CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Container references with a repository path:
    # `biocontainers/gatk:4.5.0.0--2024-01-15`,
    # `quay.io/biocontainers/samtools:1.19--h50ea8bc_0`. The date-shaped build
    # suffix is a package build stamp, not a clinical date.
    # Case-insensitive for robustness: the OCI spec requires lowercase
    # repository names, but a human transcribing one into a note writes
    # `biocontainers/STAR:...`, and the build-stamp date inside it is still not
    # a clinical date. Safe to relax because this alternative requires a `/` in
    # the repository path, which `MRN:123456` does not have.
    re.compile(
        r"(?i)(?:[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?/)?"
        r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+"
        r":[a-z0-9][a-z0-9._-]*"
    ),
    # Single-component image ref, but only with a conda-style `--build` suffix:
    # `samtools:1.19--h50ea8bc_0`. Requiring the `--` is what stops this rule
    # matching `mrn:123456`.
    re.compile(r"[a-z0-9][a-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*--[A-Za-z0-9._-]+"),
    # Conda/spack specs: `samtools=1.19`, `bwa@0.7.17`. Two guards, both load-
    # bearing. The version must contain a dot, or `mrn=123456` reads as a
    # package pin. And it is capped at three numeric components by
    # `(?![.\d])`, because without that cap `analyst@198.51.100.180` -- an
    # `ssh` target -- parsed as `analyst` pinned to version `198.51.100.180`
    # and allowlisted a reserved-range IP address, dropping measured IP recall
    # to 0.67 while the IP rule itself was working perfectly.
    re.compile(r"[a-z0-9_-][a-z0-9_.-]*(?:==?|@)\d+\.\d+(?:\.\d+)?(?![.\d])(?:[-+][A-Za-z0-9._]+)?"),
    # A version number introduced by a version word: `version 4.5.0.0`,
    # `v1.19.2`, `release 2.1`. Needed because a dotted quad like `4.5.0.0` is a
    # structurally valid IPv4 address and the IP rule -- which has a 1.00 recall
    # floor -- cannot afford a "does this look like a version" exception of its
    # own. No real IP address is preceded by the word "version".
    re.compile(
        r"(?i)(?:version|ver|rel(?:ease)?|tag|build|rev(?:ision)?)\s*[:=]?\s*"
        r"v?\d+(?:\.\d+){1,4}(?:[-+][A-Za-z0-9._]+)?"
    ),
    # Reference build with a patch or date suffix: `GRCh38.p14`.
    re.compile(r"(?:GRC[hm]\d{2}|hg\d{2}|T2T-CHM13)(?:[._][A-Za-z0-9.]+)?"),
    # An INSDC accession inside a longer token: `SRR12345678_1.fastq.gz`.
    re.compile(r"(?:SR|ER|DR)[APRSXZ]\d{6,9}[A-Za-z0-9._-]*"),
)

#: Where a site extends the KEEP set. One surface per line, ``#`` comments.
USER_ALLOWLIST_FILENAME = "deid-allowlist.txt"


def normalize_surface(surface: str) -> str:
    """The form ``LITERALS`` is keyed on: NFKC, casefold, whitespace collapsed.

    NFKC here (unlike offsets, which are NFC) because this is a *lookup* key and
    nothing is rewritten from it -- a full-width ``ｈｇ３８`` should hit the same
    entry as ``hg38``.
    """

    folded = unicodedata.normalize("NFKC", surface).casefold()
    return " ".join(folded.split())


@dataclass
class Allowlist:
    """The KEEP predicate.

    Construct once per pass; ``keeps`` is called for every candidate span, so the
    literal lookup is a set hit and the pattern lists are pre-compiled.
    """

    literals: frozenset[str]
    surface_patterns: tuple[re.Pattern[str], ...] = SURFACE_PATTERNS
    context_patterns: tuple[re.Pattern[str], ...] = CONTEXT_PATTERNS
    sources: tuple[str, ...] = ()

    @classmethod
    def default(
        cls,
        *,
        user_file: Path | None = None,
        extra: Iterable[str] = (),
    ) -> "Allowlist":
        """Built-in KEEP set, plus ``~/.wfrec/deid-allowlist.txt`` if present.

        A missing or unreadable user file is not an error -- it is the normal
        case -- but the resolved sources are recorded so ``seal.json`` can say
        which allowlist produced a given result.
        """

        literals = {normalize_surface(item) for item in LITERALS}
        sources = ["builtin"]
        if user_file is None:
            user_file = _default_user_file()
        lines: list[str] = []
        try:
            present = user_file is not None and user_file.is_file()
        except Exception:  # pragma: no cover - unstattable path
            present = False
        if present:
            try:
                lines = user_file.read_text(encoding="utf-8").splitlines()
            except Exception:
                # Broad on purpose. This file is an optional convenience and the
                # allowlist sits on the *redaction* path, which must never fail
                # open because a user's extension file is unreadable, on a
                # stale NFS mount, or -- as a test discovered -- behind a
                # monkeypatched `Path.stat`. Losing the extra KEEP entries costs
                # precision; raising here would cost the redaction entirely.
                lines = []
            else:
                sources.append(str(user_file))
            for line in lines:
                entry = line.split("#", 1)[0].strip()
                if entry:
                    literals.add(normalize_surface(entry))
        for item in extra:
            entry = item.strip()
            if entry:
                literals.add(normalize_surface(entry))
        if extra:
            sources.append("cli")
        return cls(literals=frozenset(literals), sources=tuple(sources))

    def keeps(self, text: str, span: Span) -> bool:
        """Whether this span is a bioinformatics false positive.

        Signature matches ``spans.resolve``'s ``keeps`` parameter.
        """

        surface = span.surface(text)
        if not surface:
            return False
        if normalize_surface(surface) in self.literals:
            return True
        for pattern in self.surface_patterns:
            if pattern.fullmatch(surface):
                return True
        for pattern in self.context_patterns:
            for match in pattern.finditer(text):
                # An exact span adds nothing beyond the surface patterns.
                if (
                    match.start() <= span.start
                    and span.end <= match.end()
                    and match.end() - match.start() > span.length
                ):
                    return True
        return False

    def to_dict(self) -> dict[str, object]:
        return {"entries": len(self.literals), "sources": list(self.sources)}


def _default_user_file() -> Path | None:
    """``~/.wfrec/deid-allowlist.txt``, resolved through ``wfrec.paths``.

    Imported lazily and defensively: ``autocab.deid`` must stay importable with
    stdlib alone so ``SensitiveDataRedactor`` can delegate to it without making
    the recorder a hard dependency of the pipeline.
    """

    try:
        from wfrec import paths
    except Exception:  # pragma: no cover - autocab-only installs
        return None
    try:
        return paths.home() / USER_ALLOWLIST_FILENAME
    except Exception:  # pragma: no cover - unwritable HOME
        return None


_SHARED: Allowlist | None = None


def shared() -> Allowlist:
    """Process-wide allowlist. Mirrors ``wfrec.redaction.shared()``; reset it in
    tests the same way (``allowlist._SHARED = None``)."""

    global _SHARED
    if _SHARED is None:
        _SHARED = Allowlist.default()
    return _SHARED

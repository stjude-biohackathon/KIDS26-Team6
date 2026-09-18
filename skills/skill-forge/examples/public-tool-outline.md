# Public-tool-only outline

Goal: prepare a coordinate-sorted, indexed BAM and a mapping summary before
downstream analysis.

Observed examples:

```bash
samtools sort -o sample.sorted.bam sample.bam
samtools index sample.sorted.bam
samtools flagstat sample.sorted.bam > sample.flagstat.txt
```

Important behavior:

- input and output filenames differ between projects;
- `samtools` is installed from Bioconda;
- no custom scripts or private pipeline are involved;
- output is complete when the sorted BAM, index, and non-empty flagstat report
  exist;
- genome build is not needed for these three operations.

This fixture should produce an STD skill even when CBD is the intake default.

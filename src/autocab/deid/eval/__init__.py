"""Corpus, scorer and report for the de-identification eval harness.

The order of the modules is the order of the build: the **corpus comes before
the detector** and the **scorer before the threshold**. Measuring a detector
against a corpus written afterwards measures the author's memory of the
patterns, not the detector.
"""

from __future__ import annotations

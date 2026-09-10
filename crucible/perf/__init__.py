"""Crucible's performance-engineering layer: profiles, collection, load.

Three modules, in the order the campaign uses them:

``profile``    the :class:`TargetProfile` — read from a file, never hardcoded
``collector``  raw provider metrics -> the snapshot the model is allowed to see
``runner``     the load generator, plus the mid-run gauge sampler

The split matters. ``profile`` grants authority, ``collector`` establishes
evidence, and ``runner`` produces it. Nothing in ``collector`` decides what the
agent may change, and nothing in ``profile`` decides what was measured.
"""

from .collector import COLLECTOR_VERSION

__all__ = ["COLLECTOR_VERSION"]

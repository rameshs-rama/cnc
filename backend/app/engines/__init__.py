"""Deterministic engineering engines.

Nothing in this package consults a language model. Geometry, cutting rules,
toolpaths, stock, kinematics, collision, postprocessing and validation are
computed from explicit inputs so the same inputs always produce the same result
(PRD 1.3, "Deterministic engineering").
"""

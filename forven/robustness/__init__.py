"""Robustness domain layer.

``models`` holds the typed request bodies; ``engine`` holds the validation maths,
the composite scorer and the persistence runners. Deliberately empty of imports:
the engine is imported by spawn-pool children, so this package must stay free of
import-time work.
"""

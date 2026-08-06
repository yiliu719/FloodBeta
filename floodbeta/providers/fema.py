"""
FEMA Flood Map Service Center provider implementation.

Queries the FEMA Flood Map Service Center API (free, no key required) for
a given lat/lon and normalizes FEMA's categorical flood zone labels
(AE, A, AO, AH, A1-30, X shaded, X unshaded, etc.) into the normalized
RiskPoint schema defined in base.py.
"""

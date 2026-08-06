"""
FloodBeta score aggregation.

Aggregates a list of normalized RiskPoints (from any provider) into a
single FloodBeta score (0.0-1.0), weighted equally per facility. Provider-
agnostic: must never contain provider-specific logic such as zone names or
depth values.
"""

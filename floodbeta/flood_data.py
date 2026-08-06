"""
Flood zone lookup router.

Routes geocoded facility coordinates to the currently configured
FloodDataProvider (see providers/) and returns a list of normalized
RiskPoints. Contains no provider-specific logic itself.
"""

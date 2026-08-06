"""
Address -> lat/lon geocoding using geopy's Nominatim geocoder.

Converts facility addresses extracted from 10-K filings into coordinates
for flood zone lookup. Respects Nominatim rate limits with delays between
batch requests.
"""

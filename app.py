"""
Main Streamlit entry point for FloodBeta.

Drives the end-to-end pipeline: takes a ticker input from the user, calls
edgar.py to extract facility locations from the latest 10-K, geocodes them
via geocoder.py, looks up flood risk via flood_data.py, aggregates a
FloodBeta score via scorer.py, and renders the score, map, and per-facility
breakdown in the UI.
"""

"""Ingestion: arbitrary tabular input -> canonical Invoice and Counterparty records.

parsers -> mapper -> normalizer -> matcher. Never contacts anyone; never guesses a date format.
"""

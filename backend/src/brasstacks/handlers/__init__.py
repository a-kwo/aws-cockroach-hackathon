"""Lambda entrypoints.

Deliberately thin. Each handler hydrates secrets, builds providers from
settings, and calls the same agent function the local harness calls — so what
runs at 4 AM in us-east-1 is the code the offline suite covers, not a parallel
implementation of it.
"""

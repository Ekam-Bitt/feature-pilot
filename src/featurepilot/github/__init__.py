"""GitHub ingestion and publishing.

The pipeline's two ends live here: turning a public issue URL into a local
clone plus issue text, and turning a finished run's diff into a fork, a
branch, and a pull request. Everything shells out through `client`, the one
subprocess seam, so tests fake exactly one module.
"""

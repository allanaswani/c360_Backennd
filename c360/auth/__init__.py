"""Cross-system authentication for Customer 360.

Customer 360 is a *resource server*: it trusts JWTs minted by the HF portfolio
platform (the estate's identity provider) and builds the acting user from the
token's claims, so a user who signed in on the portfolio lands here already
authenticated — no second account, no shared user database. See ``claims``.
"""

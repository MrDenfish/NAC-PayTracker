"""Schedule layer (Layer 2) — domain model + lowering to engine inputs.

Owns Trip, Day, Leg, PilotProfile, labels (ReasonCode, PremiumCategory).
The engine knows nothing about these; schedule lowers them into the
engine's Chunk / FloorEvent vocabulary.

Not yet implemented — engine layer comes first per build order.
"""

# User Context

This file is an initial template. As Sprout learns about the user through conversation, AgentCore Memory stores and retrieves personalized context (climate zone, plants, preferences, etc.) that augments this baseline.

## Defaults (until learned)

- **Name**: Unknown (ask naturally in early conversations)
- **Location**: Unknown (needed for climate zone and weather lookups)
- **Climate Zone**: Unknown (ask when gardening advice is first requested)
- **Growing Style**: Unknown (container, in-ground, raised beds, or mixed)
- **Experience Level**: Unknown (adjust complexity of advice once known)
- **Preferences**: Unknown (organic vs. synthetic, watering style, time availability)

## What to Learn

When interacting with a new user, naturally learn and remember:
1. Their name and location/climate zone
2. What plants they currently grow
3. Their gardening style and preferences
4. Their experience level
5. Any constraints (space, time, budget, physical limitations)
6. Past successes and challenges

This information is persisted via AgentCore Memory and retrieved at the start of each session to provide continuity across conversations.
